from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from markupsafe import escape
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmgu.core.auth import Viewer, build_viewer, require
from mmgu.core.hall import hall
from mmgu.core.models import Member
from mmgu.core.web import redirect, render
from mmgu.db import get_session, utcnow
from mmgu.modules.events import services
from mmgu.modules.events.models import Event, LootAward, Signup
from mmgu.modules.events.when import from_utc, zone

router = APIRouter()
TABS = ("roster", "attendance", "loot")


def _e422(e: Exception) -> HTTPException:
    return HTTPException(422, str(e))


async def _event_or_404(session: AsyncSession, event_id: int) -> Event:
    ev = await session.get(Event, event_id)
    if ev is None:
        raise HTTPException(404, "That event doesn't exist (it may have been removed).")
    return ev


def _manage_or_403(viewer: Viewer, ev: Event) -> None:
    if not services.can_manage(viewer, ev):
        raise HTTPException(403, "Only officers, raid leaders and this event's leader can do that.")


async def _active_members(session: AsyncSession) -> list[Member]:
    return list(
        (await session.execute(select(Member).where(Member.status == "active").order_by(Member.display_name)))
        .scalars()
        .all()
    )


# ----- lists ------------------------------------------------------------------------------------
@router.get("/events", response_class=HTMLResponse)
async def index(
    request: Request,
    show: str = "upcoming",
    kind: str = "",
    viewer: Viewer = Depends(require("events.view")),
    session: AsyncSession = Depends(get_session),
):
    kind = kind if kind in services.KINDS else ""
    events = await (services.past if show == "past" else services.upcoming)(session, kind=kind, limit=100)
    counts = await services.signup_counts(session, [e.id for e in events])
    mine = {}
    if viewer.id and events:
        mine = {
            s.event_id: s
            for s in (
                await session.execute(
                    select(Signup).where(Signup.member_id == viewer.id, Signup.event_id.in_([e.id for e in events]))
                )
            ).scalars()
        }
    leaders = {m.id: m for m in await _active_members(session)}
    token = await services.calendar_token(session, viewer.id) if viewer.id else None
    return render(
        request,
        "events/index.html",
        events=events,
        counts=counts,
        mine=mine,
        leaders=leaders,
        show="past" if show == "past" else "upcoming",
        kind=kind,
        kinds=services.KINDS,
        labels=services.SIGNUP_LABEL,
        feed_url=hall.settings.base_url.rstrip("/") + f"/events.ics?token={token}" if token else None,
    )


# ----- create / edit ------------------------------------------------------------------------------
def _form_data(form, viewer: Viewer) -> dict:
    fallback = services.member_tz(viewer.member)
    tz_name, tz_offset = form.get("tz_name"), form.get("tz_offset")
    return {
        "title": form.get("title"),
        "kind": form.get("kind"),
        "starts_at": services.parse_local_input(
            form.get("starts_at"), tz_name=tz_name, tz_offset=tz_offset, fallback=fallback
        ),
        "ends_at": services.parse_local_input(
            form.get("ends_at"), tz_name=tz_name, tz_offset=tz_offset, fallback=fallback
        ),
        "zone": form.get("zone"),
        "description": form.get("description"),
        "leader_id": form.get("leader_id"),
        "capacity": form.get("capacity"),
        "min_level": form.get("min_level"),
        "max_level": form.get("max_level"),
        "roles": {r["key"]: form.get(f"role_{r['key']}") for r in services.desired_roles()},
    }


def _form_page(request: Request, viewer: Viewer, members, ev: Event | None = None):
    tz = services.member_tz(viewer.member)

    def local(v: datetime | None) -> str:
        return from_utc(v, tz).strftime("%Y-%m-%dT%H:%M") if v else ""

    if ev is None:
        start = (utcnow() + timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
        data = {"kind": "Raid", "starts_at": start, "leader_id": viewer.id, "roles": {}}
    else:
        data = {k: getattr(ev, k) for k in ("title", "kind", "starts_at", "ends_at", "zone", "description")}
        data |= {k: getattr(ev, k) for k in ("leader_id", "capacity", "min_level", "max_level", "roles")}
    return render(
        request,
        "events/form.html",
        ev=ev,
        data=data,
        local_start=local(data.get("starts_at")),
        local_end=local(data.get("ends_at")),
        members=members,
        kinds=services.KINDS,
        roles=services.desired_roles(),
        zones=hall.game.zones,
        profile_tz=viewer.member.timezone if viewer.member else None,
        max_weeks=services.MAX_REPEAT_WEEKS,
    )


@router.get("/events/new", response_class=HTMLResponse)
async def new_form(
    request: Request, viewer: Viewer = Depends(require("events.manage")), session: AsyncSession = Depends(get_session)
):
    return _form_page(request, viewer, await _active_members(session))


@router.post("/events/new")
async def create(
    request: Request, viewer: Viewer = Depends(require("events.manage")), session: AsyncSession = Depends(get_session)
):
    form = await request.form()
    try:
        data = _form_data(form, viewer)
        weeks = int(form.get("repeat_weeks") or 0) if form.get("repeat") else 0
        tz = zone(form.get("tz_name"))
        events = await services.create_event(
            session, viewer, data, repeat_weeks=weeks, tz=tz or services.member_tz(viewer.member)
        )
    except (services.EventError, ValueError) as e:
        raise _e422(e) from e
    await session.commit()
    first = events[0]
    msg = f"{first.title} is on the War Table."
    if len(events) > 1:
        msg = f"{first.title} is scheduled for the next {len(events)} weeks."
    return redirect(request, f"/events/{first.id}", msg)


@router.get("/events/{event_id:int}/edit", response_class=HTMLResponse)
async def edit_form(
    request: Request,
    event_id: int,
    viewer: Viewer = Depends(require("events.view")),
    session: AsyncSession = Depends(get_session),
):
    ev = await _event_or_404(session, event_id)
    _manage_or_403(viewer, ev)
    return _form_page(request, viewer, await _active_members(session), ev)


@router.post("/events/{event_id:int}/edit")
async def edit(
    request: Request,
    event_id: int,
    viewer: Viewer = Depends(require("events.view")),
    session: AsyncSession = Depends(get_session),
):
    ev = await _event_or_404(session, event_id)
    _manage_or_403(viewer, ev)
    form = await request.form()
    try:
        data = _form_data(form, viewer)
        if not viewer.can("events.manage"):
            data["leader_id"] = ev.leader_id  # a named leader can't hand the event to someone else
        await services.update_event(session, viewer, ev, data)
    except (services.EventError, ValueError) as e:
        raise _e422(e) from e
    await session.commit()
    return redirect(request, f"/events/{ev.id}", "Saved.")


@router.post("/events/{event_id:int}/status")
async def change_status(
    request: Request,
    event_id: int,
    viewer: Viewer = Depends(require("events.view")),
    session: AsyncSession = Depends(get_session),
):
    ev = await _event_or_404(session, event_id)
    _manage_or_403(viewer, ev)
    form = await request.form()
    status = str(form.get("status") or "")
    try:
        await services.set_status(session, viewer, ev, status)
    except services.EventError as e:
        raise _e422(e) from e
    await session.commit()
    msg = {
        "active": "Started. People can check in now.",
        "done": "Marked done.",
        "cancelled": "Cancelled. Everyone signed up was told.",
        "scheduled": "Back on the schedule.",
    }[status]
    return redirect(request, f"/events/{ev.id}", msg)


# ----- event page ---------------------------------------------------------------------------------
@router.get("/events/{event_id:int}", response_class=HTMLResponse)
async def event_page(
    request: Request,
    event_id: int,
    tab: str = "roster",
    viewer: Viewer = Depends(require("events.view")),
    session: AsyncSession = Depends(get_session),
):
    ev = await _event_or_404(session, event_id)
    tab = tab if tab in TABS else "roster"
    ros = await services.roster(session, ev)
    mine = await services.my_signup(session, ev.id, viewer.id)
    chars = await services.characters_for(session, viewer.id) if viewer.id else []
    leader = await session.get(Member, ev.leader_id) if ev.leader_id else None
    # Preselect the role only when the member picked one that differs from their class's usual role.
    explicit_role = None
    if mine:
        mine_char = next((c for c in chars if c.id == mine.character_id), None)
        if mine_char is None or mine.role != hall.game.class_role(mine_char.class_name):
            explicit_role = mine.role
    attendance = await services.attendance_rows(session, ev.id)
    loot = await services.loot_history(session, event_id=ev.id) if viewer.can("loot.view") else []
    manage = services.can_manage(viewer, ev)
    # Attendance checklist: everyone signed up (any answer) plus anyone already marked.
    present = {a.member_id for a, _, _ in attendance if a.member_id}
    checklist: dict[int, dict] = {}
    for group in ros.groups:
        for r in group.rows:
            checklist[r.member.id] = {"member": r.member, "character": r.character, "status": r.signup.status}
    for r in ros.maybe + ros.cant:
        checklist.setdefault(r.member.id, {"member": r.member, "character": r.character, "status": r.signup.status})
    for _a, m, c in attendance:
        if m is not None:
            checklist.setdefault(m.id, {"member": m, "character": c, "status": None})
    all_members = await _active_members(session) if manage else []
    series = []
    if ev.series_id:
        series = list(
            (
                await session.execute(
                    select(Event).where(Event.series_id == ev.series_id, Event.id != ev.id).order_by(Event.starts_at)
                )
            )
            .scalars()
            .all()
        )
    return render(
        request,
        "events/event.html",
        ev=ev,
        tab=tab,
        ros=ros,
        mine=mine,
        chars=chars,
        leader=leader,
        attendance=attendance,
        present=present,
        checklist=sorted(checklist.values(), key=lambda r: r["member"].display_name.lower()),
        loot=loot,
        manage=manage,
        all_members=all_members,
        series=series,
        statuses=services.SIGNUP_STATUSES,
        labels=services.SIGNUP_LABEL,
        role_options=services.role_options(),
        role_label=services.role_label,
        methods=services.LOOT_METHODS,
        checked_in=viewer.id in present,
        explicit_role=explicit_role,
        who_result=request.session.pop("events_who", None) if "session" in request.scope else None,
    )


@router.post("/events/{event_id:int}/signup")
async def do_signup(
    request: Request,
    event_id: int,
    viewer: Viewer = Depends(require("events.signup")),
    session: AsyncSession = Depends(get_session),
):
    ev = await _event_or_404(session, event_id)
    form = await request.form()
    char = form.get("character_id")
    try:
        s = await services.signup(
            session,
            viewer,
            ev,
            status=str(form.get("status") or "going"),
            character_id=int(char) if char and str(char).isdigit() else None,
            role=form.get("role") or None,
            note=form.get("note"),
        )
    except services.EventError as e:
        raise _e422(e) from e
    await session.commit()
    back = form.get("next") or f"/events/{ev.id}"
    if not str(back).startswith("/"):
        back = f"/events/{ev.id}"
    return redirect(request, back, f"You're marked {services.SIGNUP_LABEL[s.status]} for {ev.title}.")


# ----- attendance ---------------------------------------------------------------------------------
@router.post("/events/{event_id:int}/attendance")
async def attendance(
    request: Request,
    event_id: int,
    viewer: Viewer = Depends(require("events.view")),
    session: AsyncSession = Depends(get_session),
):
    ev = await _event_or_404(session, event_id)
    _manage_or_403(viewer, ev)
    form = await request.form()
    ids = {int(v) for v in form.getlist("present") if str(v).isdigit()}
    extra = form.get("add_member")
    if extra and str(extra).isdigit():
        ids.add(int(extra))
    res = await services.set_attendance(session, viewer, ev, ids)
    await session.commit()
    return redirect(
        request,
        f"/events/{ev.id}?tab=attendance",
        f"Attendance saved: {len(ids)} present ({res['added']} added, {res['removed']} removed).",
    )


@router.post("/events/{event_id:int}/checkin")
async def checkin(
    request: Request,
    event_id: int,
    viewer: Viewer = Depends(require("events.signup")),
    session: AsyncSession = Depends(get_session),
):
    ev = await _event_or_404(session, event_id)
    try:
        added = await services.check_in(session, viewer, ev)
    except services.EventError as e:
        raise _e422(e) from e
    await session.commit()
    return redirect(request, f"/events/{ev.id}", "Checked in. Have fun!" if added else "You were already checked in.")


@router.post("/events/{event_id:int}/who")
async def who(
    request: Request,
    event_id: int,
    viewer: Viewer = Depends(require("events.view")),
    session: AsyncSession = Depends(get_session),
):
    ev = await _event_or_404(session, event_id)
    _manage_or_403(viewer, ev)
    form = await request.form()
    text = str(form.get("text") or "")
    if not text.strip():
        raise HTTPException(422, "Paste the /who output first.")
    res = await services.attendance_from_who(session, viewer, ev, text)
    await session.commit()
    request.session["events_who"] = res
    kind = "ok" if res["marked"] or res["already"] else "warn"
    return redirect(
        request,
        f"/events/{ev.id}?tab=attendance",
        f"From /who: {len(res['marked'])} marked present, {len(res['unknown'])} not on the roster.",
        kind,
    )


@router.post("/events/{event_id:int}/attendance/{attendance_id:int}/delete")
async def remove_attendee(
    request: Request,
    event_id: int,
    attendance_id: int,
    viewer: Viewer = Depends(require("events.view")),
    session: AsyncSession = Depends(get_session),
):
    ev = await _event_or_404(session, event_id)
    _manage_or_403(viewer, ev)
    await services.delete_attendance(session, viewer, ev, attendance_id)
    await session.commit()
    return redirect(request, f"/events/{ev.id}?tab=attendance", "Removed.")


@router.get("/events/attendance", response_class=HTMLResponse)
async def attendance_summary(
    request: Request,
    kind: str = "",
    viewer: Viewer = Depends(require("events.view")),
    session: AsyncSession = Depends(get_session),
):
    kind = kind if kind in services.KINDS else ""
    windows = (30, 60, 90)
    rates = {d: await services.attendance_rates(session, d, kind=kind) for d in windows}
    members = {m.id: m for m in await _active_members(session)}
    rows = []
    for mid, m in members.items():
        r = {d: rates[d].get(mid) for d in windows}
        if any(r.values()):
            rows.append({"member": m, "r": r})

    def sort_key(row):
        v = row["r"][30] or row["r"][60] or row["r"][90] or (0, 1)
        return (-(v[0] / v[1] if v[1] else 0), row["member"].display_name.lower())

    rows.sort(key=sort_key)
    counted = {d: max((v[1] for v in rates[d].values()), default=0) for d in windows}
    return render(
        request,
        "events/attendance.html",
        rows=rows,
        windows=windows,
        counted=counted,
        kind=kind,
        kinds=services.KINDS,
    )


# ----- loot ---------------------------------------------------------------------------------------
def _date(v: str) -> datetime | None:
    try:
        return datetime.fromisoformat(v) if v else None
    except ValueError:
        return None


@router.get("/loot", response_class=HTMLResponse)
async def loot_page(
    request: Request,
    member: str = "",
    item: str = "",
    event: str = "",
    character: str = "",
    since: str = "",
    until: str = "",
    viewer: Viewer = Depends(require("loot.view")),
    session: AsyncSession = Depends(get_session),
):
    until_dt = _date(until)
    rows = await services.loot_history(
        session,
        member_id=int(member) if member.isdigit() else None,
        item=item,
        event_id=int(event) if event.isdigit() else None,
        character=character,
        since=_date(since),
        until=until_dt + timedelta(days=1) if until_dt else None,
        limit=300,
    )
    from mmgu.modules.roster.services import list_characters

    members = await _active_members(session)
    recent_events = await services.past(session, limit=40)
    characters = await list_characters(session, mules=False, limit=1000) if viewer.can("loot.award") else []
    return render(
        request,
        "events/loot.html",
        rows=rows,
        f={"member": member, "item": item, "event": event, "character": character, "since": since, "until": until},
        members=members,
        recent_events=(await services.upcoming(session, limit=10)) + recent_events,
        methods=services.LOOT_METHODS,
        characters=characters,
        total_points=sum(a.points or 0 for a, _, _ in rows),
    )


@router.get("/loot/suggest", response_class=HTMLResponse)
async def loot_suggest(
    q: str = "",
    item: str = "",
    viewer: Viewer = Depends(require("loot.view")),
    session: AsyncSession = Depends(get_session),
):
    names = await services.item_names(session, q or item)
    opts = "".join(f'<option value="{escape(n)}"></option>' for n in names)
    return HTMLResponse(f'<datalist id="loot-items">{opts}</datalist>')


@router.post("/loot")
async def award(
    request: Request, viewer: Viewer = Depends(require("loot.award")), session: AsyncSession = Depends(get_session)
):
    form = await request.form()
    event_id = form.get("event_id")
    try:
        a = await services.award_loot(
            session,
            viewer,
            item=str(form.get("item") or ""),
            character=str(form.get("character") or ""),
            event_id=int(event_id) if event_id and str(event_id).isdigit() else None,
            method=str(form.get("method") or "Roll"),
            points=form.get("points"),
            note=form.get("note"),
        )
    except services.EventError as e:
        raise _e422(e) from e
    await session.commit()
    back = f"/events/{a.event_id}?tab=loot" if a.event_id and form.get("from_event") else "/loot"
    return redirect(request, back, f"{a.item_name} → {a.character_name}. Recorded.")


@router.post("/loot/{award_id:int}/delete")
async def delete_award(
    request: Request,
    award_id: int,
    viewer: Viewer = Depends(require("loot.award")),
    session: AsyncSession = Depends(get_session),
):
    a = await session.get(LootAward, award_id)
    if a is None:
        raise HTTPException(404, "That loot record is already gone.")
    event_id = a.event_id
    await services.delete_award(session, viewer, a)
    await session.commit()
    form = await request.form()
    back = f"/events/{event_id}?tab=loot" if event_id and form.get("from_event") else "/loot"
    return redirect(request, back, "Loot record removed.")


# ----- calendar feed ------------------------------------------------------------------------------
@router.post("/events/calendar/regenerate")
async def regenerate_feed(
    request: Request, viewer: Viewer = Depends(require("events.view")), session: AsyncSession = Depends(get_session)
):
    await services.calendar_token(session, viewer.id, regenerate=True)
    await session.commit()
    return redirect(request, "/events#calendar", "New calendar link made. The old one stops working.")


@router.get("/events.ics")
async def ics(token: str = "", session: AsyncSession = Depends(get_session)):
    member = await services.member_for_token(session, token)
    if member is None:
        raise HTTPException(404, "That calendar link isn't valid. Copy a fresh one from the War Table.")
    viewer = await build_viewer(session, member, "token")
    if not viewer.can("events.view"):
        raise HTTPException(403, "This member can't see events any more.")
    body = await services.feed_for(session, member)
    return Response(
        body,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'inline; filename="events.ics"', "Cache-Control": "private, max-age=300"},
    )


# ----- JSON API -----------------------------------------------------------------------------------
@router.get("/api/events")
async def api_events(
    upcoming: str = "1",
    kind: str = "",
    viewer: Viewer = Depends(require("events.view")),
    session: AsyncSession = Depends(get_session),
):
    kind = kind if kind in services.KINDS else ""
    if upcoming in ("0", "false", "no"):
        events = await services.past(session, kind=kind, limit=50)
    else:
        events = await services.upcoming(session, kind=kind, limit=50)
    counts = await services.signup_counts(session, [e.id for e in events])
    return {
        "events": [services.event_json(e, counts[e.id].get("going", 0) + counts[e.id].get("late", 0)) for e in events]
    }
