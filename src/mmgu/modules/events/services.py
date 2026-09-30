"""Events, sign-ups, attendance and loot: logic shared by web pages, the Discord bot, the job and the API."""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mmgu.core.audit import record, snapshot
from mmgu.core.auth import Viewer
from mmgu.core.hall import hall
from mmgu.core.models import Character, Member
from mmgu.core.notify import notify
from mmgu.core.whoparse import parse_who
from mmgu.db import utcnow
from mmgu.modules.events.models import Attendance, CalendarToken, Event, LootAward, Signup
from mmgu.modules.events.when import from_utc, to_utc, zone

KINDS = ["Raid", "Group", "Crafting", "Social", "Other"]
STATUSES = ["scheduled", "active", "done", "cancelled"]
SIGNUP_STATUSES = [("going", "Going"), ("maybe", "Maybe"), ("late", "Late"), ("cant", "Can't")]
SIGNUP_LABEL = dict(SIGNUP_STATUSES)
LOOT_METHODS = ["Roll", "Council", "Points", "Free", "Other"]
MAX_REPEAT_WEEKS = 12


class EventError(ValueError):
    pass


# ----- small helpers ----------------------------------------------------------------------------
def role_options() -> list[dict]:
    """Every sign-up role from the game pack, e.g. tank/healer/dps/support/any."""
    return hall.game.event_roles


def desired_roles() -> list[dict]:
    """Roles a leader can ask for a number of (everything except the 'flexible' catch-all)."""
    return [r for r in role_options() if r["key"] != "any"]


def role_label(key: str | None) -> str:
    for r in role_options():
        if r["key"] == key:
            return r["label"]
    return (key or "Flexible").title()


def member_tz(member: Member | None) -> tzinfo | None:
    return zone(member.timezone) if member else None


def can_manage(viewer: Viewer, event: Event) -> bool:
    """Officers/raid leaders manage every event; the named leader manages their own."""
    return viewer.can("events.manage") or (viewer.id is not None and event.leader_id == viewer.id)


def _int(v: Any, lo: int = 0, hi: int = 10_000) -> int | None:
    if v is None or str(v).strip() == "":
        return None
    try:
        n = int(float(str(v).strip()))
    except ValueError as e:
        raise EventError(f"'{v}' isn't a whole number.") from e
    if not lo <= n <= hi:
        raise EventError(f"{n} is out of range ({lo}–{hi}).")
    return n


def parse_local_input(value: str | None, *, tz_name: str | None, tz_offset: str | None, fallback: tzinfo | None):
    """A browser ``datetime-local`` value → naive UTC.

    The event form sends the browser's IANA zone (``tz_name``) and its UTC offset for that date
    (``tz_offset``, minutes as JS ``getTimezoneOffset`` returns them). Without JavaScript we fall back
    to the member's profile zone, then UTC.
    """
    value = (value or "").strip()
    if not value:
        return None
    try:
        local = datetime.fromisoformat(value.replace(" ", "T"))
    except ValueError as e:
        raise EventError(f"'{value}' isn't a date and time.") from e
    local = local.replace(tzinfo=None, second=0, microsecond=0)
    tz = zone(tz_name)
    if tz is not None:
        return to_utc(local, tz)
    if tz_offset not in (None, "") and str(tz_offset).lstrip("-").isdigit():
        return local + timedelta(minutes=int(tz_offset))
    return to_utc(local, fallback)


def clean_event_fields(data: dict[str, Any]) -> dict[str, Any]:
    title = " ".join(str(data.get("title") or "").split())
    if not title or len(title) > 120:
        raise EventError("Give the event a title (up to 120 characters).")
    kind = str(data.get("kind") or "Raid").strip().title()
    if kind not in KINDS:
        kind = "Other"
    starts_at = data.get("starts_at")
    if not isinstance(starts_at, datetime):
        raise EventError("Pick when the event starts.")
    ends_at = data.get("ends_at")
    if ends_at is not None and ends_at <= starts_at:
        raise EventError("The end time must be after the start time.")
    cap = hall.game.level_cap or 100
    min_level = _int(data.get("min_level"), 1, cap + 20)
    max_level = _int(data.get("max_level"), 1, cap + 20)
    if min_level and max_level and min_level > max_level:
        raise EventError("The lowest level can't be above the highest level.")
    roles: dict[str, int] = {}
    for r in desired_roles():
        n = _int((data.get("roles") or {}).get(r["key"]), 0, 500)
        if n:
            roles[r["key"]] = n
    return {
        "title": title,
        "kind": kind,
        "starts_at": starts_at.replace(second=0, microsecond=0),
        "ends_at": ends_at.replace(second=0, microsecond=0) if ends_at else None,
        "zone": " ".join(str(data.get("zone") or "").split())[:120] or None,
        "description": (str(data.get("description") or "").strip() or None),
        "leader_id": _int(data.get("leader_id"), 1, 10**9),
        "capacity": _int(data.get("capacity"), 1, 1000),
        "min_level": min_level,
        "max_level": max_level,
        "roles": roles,
    }


# ----- events -------------------------------------------------------------------------------------
async def create_event(
    session: AsyncSession,
    viewer: Viewer,
    data: dict[str, Any],
    *,
    repeat_weeks: int = 0,
    tz: tzinfo | None = None,
    via: str = "web",
) -> list[Event]:
    """Create an event, plus ``repeat_weeks`` more copies a week apart (same local wall-clock time in ``tz``)."""
    fields = clean_event_fields(data)
    if fields["leader_id"] is None:
        fields["leader_id"] = viewer.id
    elif await session.get(Member, fields["leader_id"]) is None:
        raise EventError("That leader isn't a member of the hall.")
    repeat_weeks = max(0, min(int(repeat_weeks or 0), MAX_REPEAT_WEEKS))
    series = uuid.uuid4().hex[:16] if repeat_weeks else None
    local_start = from_utc(fields["starts_at"], tz)
    length = (fields["ends_at"] - fields["starts_at"]) if fields["ends_at"] else None
    events: list[Event] = []
    for week in range(repeat_weeks + 1):
        start = to_utc(local_start + timedelta(weeks=week), tz)
        ev = Event(
            **{**fields, "starts_at": start, "ends_at": start + length if length else None},
            series_id=series,
            created_by=viewer.id,
            discord_channel_id=data.get("discord_channel_id"),
        )
        session.add(ev)
        events.append(ev)
    await session.flush()
    for ev in events:
        await record(session, "events.created", actor_id=viewer.id, entity=ev, via=via, summary=f"Scheduled {ev.title}")
    for ev in events:
        await hall.bus.emit("events.created", actor_id=viewer.id, event_id=ev.id)
    return events


async def update_event(
    session: AsyncSession, viewer: Viewer, event: Event, data: dict[str, Any], *, via: str = "web"
) -> Event:
    fields = clean_event_fields(data)
    if fields["leader_id"] is None:
        fields["leader_id"] = event.leader_id
    before = snapshot(event)
    moved = fields["starts_at"] != event.starts_at
    for k, v in fields.items():
        setattr(event, k, v)
    if moved and event.starts_at > utcnow():
        event.reminded = False
        event.start_announced = False
        if event.status == "active":
            event.status = "scheduled"
    await session.flush()
    await record(
        session,
        "events.updated",
        actor_id=viewer.id,
        entity=event,
        before=before,
        via=via,
        summary=f"Edited {event.title}",
    )
    await hall.bus.emit("events.updated", actor_id=viewer.id, event_id=event.id)
    if moved:
        for s in await going_signups(session, event.id, include_maybe=True):
            await notify(session, s.member_id, f"{event.title} moved. Check the new time.", f"/events/{event.id}")
    return event


async def set_status(session: AsyncSession, viewer: Viewer | None, event: Event, status: str, *, via: str = "web"):
    if status not in STATUSES:
        raise EventError("Unknown status.")
    if status == event.status:
        return event
    before = snapshot(event)
    old = event.status
    event.status = status
    if status == "active":
        event.start_announced = True if event.starts_at <= utcnow() else event.start_announced
    await session.flush()
    actor = viewer.id if viewer else None
    await record(
        session,
        "events.status",
        actor_id=actor,
        entity=event,
        before=before,
        via=via,
        summary=f"{event.title}: {old} → {status}",
    )
    await hall.bus.emit("events.updated", actor_id=actor, event_id=event.id)
    if status == "cancelled":
        for s in await going_signups(session, event.id, include_maybe=True):
            if s.member_id != actor:
                await notify(session, s.member_id, f"{event.title} was cancelled.", f"/events/{event.id}")
    return event


async def get_event(session: AsyncSession, event_id: int) -> Event | None:
    return await session.get(Event, event_id)


async def upcoming(session: AsyncSession, *, limit: int = 50, kind: str = "") -> list[Event]:
    now = utcnow()
    stmt = select(Event).where(
        Event.status.in_(("scheduled", "active")),
        or_(Event.starts_at >= now - timedelta(hours=12), Event.status == "active"),
    )
    if kind:
        stmt = stmt.where(Event.kind == kind)
    return list((await session.execute(stmt.order_by(Event.starts_at).limit(limit))).scalars().all())


async def past(session: AsyncSession, *, limit: int = 50, kind: str = "") -> list[Event]:
    now = utcnow()
    stmt = select(Event).where(
        or_(
            Event.status.in_(("done", "cancelled")),
            (Event.starts_at < now - timedelta(hours=12)) & (Event.status == "scheduled"),
        )
    )
    if kind:
        stmt = stmt.where(Event.kind == kind)
    return list((await session.execute(stmt.order_by(Event.starts_at.desc()).limit(limit))).scalars().all())


async def search_events(session: AsyncSession, q: str, limit: int = 8) -> list[Event]:
    stmt = select(Event).where(Event.title.ilike(f"%{q.strip()}%")).order_by(Event.starts_at.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


# ----- sign-ups -----------------------------------------------------------------------------------
async def characters_for(session: AsyncSession, member_id: int) -> list[Character]:
    from mmgu.modules.roster.services import characters_of

    return [c for c in await characters_of(session, member_id) if not c.is_bank_mule] or await characters_of(
        session, member_id
    )


async def default_character(session: AsyncSession, member_id: int) -> Character | None:
    chars = await characters_for(session, member_id)
    return chars[0] if chars else None  # characters_of puts the main first


async def my_signup(session: AsyncSession, event_id: int, member_id: int | None) -> Signup | None:
    if not member_id:
        return None
    return (
        await session.execute(select(Signup).where(Signup.event_id == event_id, Signup.member_id == member_id))
    ).scalar_one_or_none()


async def signup(
    session: AsyncSession,
    viewer: Viewer,
    event: Event,
    *,
    status: str,
    character_id: int | None = None,
    role: str | None = None,
    note: str | None = None,
    via: str = "web",
) -> Signup:
    if status not in SIGNUP_LABEL:
        raise EventError("Pick Going, Maybe, Late or Can't.")
    if event.status in ("done", "cancelled"):
        raise EventError(f"{event.title} is {'over' if event.status == 'done' else 'cancelled'}; sign-ups are closed.")
    chars = await characters_for(session, viewer.id)
    char = None
    if character_id:
        char = next((c for c in chars if c.id == character_id), None)
        if char is None:
            raise EventError("Pick one of your own characters.")
    existing = await my_signup(session, event.id, viewer.id)
    if char is None:
        char = next((c for c in chars if existing and c.id == existing.character_id), None) or (
            chars[0] if chars else None
        )
    if char is None and status != "cant":
        raise EventError("Add a character to the Muster Roll first (Roster → Add a character, or /char add).")
    keys = {r["key"] for r in role_options()}
    if not role or role not in keys:
        same_char = existing is not None and char is not None and existing.character_id == char.id
        role = (existing.role if same_char else None) or hall.game.class_role(char.class_name if char else None)
        if role not in keys:
            role = "any"
    before = snapshot(existing) if existing else None
    s = existing or Signup(event_id=event.id, member_id=viewer.id)
    s.status = status
    s.character_id = char.id if char else None
    s.role = role
    if note is not None:
        s.note = note.strip()[:200] or None
    if existing is None:
        session.add(s)
    await session.flush()
    await record(
        session,
        "events.signup",
        actor_id=viewer.id,
        entity=s,
        before=before,
        via=via,
        summary=f"{SIGNUP_LABEL[status]} to {event.title}" + (f" as {char.name}" if char else ""),
    )
    await hall.bus.emit("events.signup", actor_id=viewer.id, event_id=event.id, signup_id=s.id)
    return s


async def going_signups(session: AsyncSession, event_id: int, *, include_maybe: bool = False) -> list[Signup]:
    statuses = ("going", "late", "maybe") if include_maybe else ("going", "late")
    stmt = select(Signup).where(Signup.event_id == event_id, Signup.status.in_(statuses))
    return list((await session.execute(stmt.order_by(Signup.created_at))).scalars().all())


@dataclass
class RosterRow:
    signup: Signup
    member: Member
    character: Character | None
    bench: bool = False


@dataclass
class RoleGroup:
    key: str
    label: str
    rows: list[RosterRow] = field(default_factory=list)
    want: int = 0

    @property
    def have(self) -> int:
        return len([r for r in self.rows if not r.bench])

    @property
    def short(self) -> int:
        return max(0, self.want - self.have)


@dataclass
class EventRoster:
    groups: list[RoleGroup]
    maybe: list[RosterRow]
    cant: list[RosterRow]
    bench: list[RosterRow]
    going: int
    capacity: int | None

    @property
    def visible_groups(self) -> list[RoleGroup]:
        return [g for g in self.groups if g.rows or g.want]

    @property
    def short_roles(self) -> list[RoleGroup]:
        return [g for g in self.groups if g.short]

    @property
    def counts_text(self) -> str:
        parts = []
        for g in self.groups:
            if g.want or g.have:
                parts.append(f"{g.label} {g.have}" + (f"/{g.want}" if g.want else ""))
        return " · ".join(parts)


async def roster(session: AsyncSession, event: Event) -> EventRoster:
    rows = (
        await session.execute(
            select(Signup, Member, Character)
            .join(Member, Member.id == Signup.member_id)
            .join(Character, Character.id == Signup.character_id, isouter=True)
            .where(Signup.event_id == event.id)
            .order_by(Signup.created_at, Signup.id)
        )
    ).all()
    groups = {
        r["key"]: RoleGroup(r["key"], r["label"], want=(event.roles or {}).get(r["key"], 0)) for r in role_options()
    }
    maybe, cant, bench = [], [], []
    seated = 0
    for s, m, c in rows:
        row = RosterRow(s, m, c)
        if s.status in ("going", "late"):
            if event.capacity and seated >= event.capacity:
                row.bench = True
                bench.append(row)
            else:
                seated += 1
            groups.setdefault(s.role, RoleGroup(s.role, role_label(s.role))).rows.append(row)
        elif s.status == "maybe":
            maybe.append(row)
        else:
            cant.append(row)
    return EventRoster(list(groups.values()), maybe, cant, bench, seated, event.capacity)


async def signup_counts(session: AsyncSession, event_ids: list[int]) -> dict[int, dict[str, int]]:
    out: dict[int, dict[str, int]] = {i: {} for i in event_ids}
    if not event_ids:
        return out
    rows = await session.execute(
        select(Signup.event_id, Signup.status, func.count())
        .where(Signup.event_id.in_(event_ids))
        .group_by(Signup.event_id, Signup.status)
    )
    for eid, st, n in rows.all():
        out[eid][st] = n
    return out


# ----- attendance ---------------------------------------------------------------------------------
async def attendance_rows(
    session: AsyncSession, event_id: int
) -> list[tuple[Attendance, Member | None, Character | None]]:
    return [
        (a, m, c)
        for a, m, c in (
            await session.execute(
                select(Attendance, Member, Character)
                .join(Member, Member.id == Attendance.member_id, isouter=True)
                .join(Character, Character.id == Attendance.character_id, isouter=True)
                .where(Attendance.event_id == event_id)
                .order_by(Attendance.created_at)
            )
        ).all()
    ]


async def _add_attendance(
    session: AsyncSession, event: Event, *, member_id: int | None, character_id: int | None, source: str, by: int | None
) -> bool:
    """Add one attendee unless they're already marked. Returns True when a row was added."""
    stmt = select(Attendance).where(Attendance.event_id == event.id)
    if member_id:
        stmt = stmt.where(Attendance.member_id == member_id)
    else:
        stmt = stmt.where(Attendance.member_id.is_(None), Attendance.character_id == character_id)
    if (await session.execute(stmt)).scalars().first() is not None:
        return False
    session.add(
        Attendance(event_id=event.id, member_id=member_id, character_id=character_id, source=source, recorded_by=by)
    )
    return True


async def set_attendance(session: AsyncSession, viewer: Viewer, event: Event, member_ids: set[int]) -> dict[str, int]:
    """The checkbox list: exactly these members were there (people added from /who without a member stay)."""
    existing = (
        (
            await session.execute(
                select(Attendance).where(Attendance.event_id == event.id, Attendance.member_id.is_not(None))
            )
        )
        .scalars()
        .all()
    )
    have = {a.member_id for a in existing}
    removed = 0
    for a in existing:
        if a.member_id not in member_ids:
            await session.delete(a)
            removed += 1
    signups = {
        s.member_id: s.character_id
        for s in (await session.execute(select(Signup).where(Signup.event_id == event.id))).scalars()
    }
    added = 0
    for mid in member_ids - have:
        if await session.get(Member, mid) is None:
            continue
        session.add(
            Attendance(
                event_id=event.id, member_id=mid, character_id=signups.get(mid), source="manual", recorded_by=viewer.id
            )
        )
        added += 1
    await session.flush()
    await record(
        session,
        "events.attendance",
        actor_id=viewer.id,
        entity_type="Event",
        entity_id=event.id,
        summary=f"Attendance for {event.title}: +{added} −{removed}",
        after={"members": sorted(member_ids)},
    )
    await hall.bus.emit("events.attendance", actor_id=viewer.id, event_id=event.id)
    return {"added": added, "removed": removed}


async def check_in(session: AsyncSession, viewer: Viewer, event: Event, *, via: str = "web") -> bool:
    if event.status != "active":
        raise EventError("Check-in opens when the event starts.")
    s = await my_signup(session, event.id, viewer.id)
    char_id = s.character_id if s and s.character_id else None
    if char_id is None:
        ch = await default_character(session, viewer.id)
        char_id = ch.id if ch else None
    added = await _add_attendance(
        session, event, member_id=viewer.id, character_id=char_id, source="checkin", by=viewer.id
    )
    await session.flush()
    if added:
        await record(
            session,
            "events.checkin",
            actor_id=viewer.id,
            entity_type="Event",
            entity_id=event.id,
            via=via,
            summary=f"Checked in to {event.title}",
        )
        await hall.bus.emit("events.attendance", actor_id=viewer.id, event_id=event.id)
    return added


async def attendance_from_who(session: AsyncSession, viewer: Viewer, event: Event, text: str) -> dict[str, list[str]]:
    """Mark everyone in pasted /who output whose character is on the roster. Returns matched/unknown/already names."""
    from mmgu.modules.roster.services import find_character

    lines, _skipped = parse_who(text, hall.game)
    out: dict[str, list[str]] = {"marked": [], "already": [], "unknown": []}
    for line in lines:
        ch = await find_character(session, line.name)
        if ch is None:
            out["unknown"].append(line.name)
            continue
        added = await _add_attendance(
            session, event, member_id=ch.member_id, character_id=ch.id, source="who", by=viewer.id
        )
        await session.flush()
        (out["marked"] if added else out["already"]).append(ch.name)
    await record(
        session,
        "events.attendance_who",
        actor_id=viewer.id,
        entity_type="Event",
        entity_id=event.id,
        summary=f"/who attendance for {event.title}: {len(out['marked'])} marked, {len(out['unknown'])} unknown",
        after=out,
    )
    await hall.bus.emit("events.attendance", actor_id=viewer.id, event_id=event.id)
    return out


async def attendance_rates(
    session: AsyncSession, days: int, *, member_id: int | None = None, kind: str = "", now: datetime | None = None
) -> dict[int, tuple[int, int]]:
    """{member_id: (attended, counted)} over the last ``days`` days.

    Only events that had attendance taken count, and only those after the member joined the hall,
    so a new member isn't punished for raids before their time.
    """
    now = now or utcnow()
    since = now - timedelta(days=days)
    taken = select(Attendance.event_id).distinct()
    stmt = select(Event.id, Event.starts_at).where(
        Event.starts_at >= since,
        or_(Event.starts_at <= now, Event.status.in_(("active", "done"))),
        Event.status != "cancelled",
        Event.id.in_(taken),
    )
    if kind:
        stmt = stmt.where(Event.kind == kind)
    events = {eid: at for eid, at in (await session.execute(stmt)).all()}
    if not events:
        return {}
    mstmt = select(Member.id, Member.created_at).where(Member.status == "active")
    if member_id:
        mstmt = select(Member.id, Member.created_at).where(Member.id == member_id)
    members = {mid: joined for mid, joined in (await session.execute(mstmt)).all()}
    att_rows = await session.execute(
        select(Attendance.member_id, Attendance.event_id).where(
            Attendance.event_id.in_(list(events)), Attendance.member_id.in_(list(members))
        )
    )
    attended: dict[int, set[int]] = {}
    for mid, eid in att_rows.all():
        attended.setdefault(mid, set()).add(eid)
    out: dict[int, tuple[int, int]] = {}
    for mid, joined in members.items():
        eligible = [eid for eid, at in events.items() if joined is None or at >= joined - timedelta(days=1)]
        mine = attended.get(mid, set())
        went, counted = len(mine), len(set(eligible) | mine)
        if counted:
            out[mid] = (went, counted)
    return out


# ----- loot ---------------------------------------------------------------------------------------
async def award_loot(
    session: AsyncSession,
    viewer: Viewer,
    *,
    item: str,
    character: str,
    event_id: int | None = None,
    method: str = "Roll",
    points: Any = None,
    note: str | None = None,
    via: str = "web",
) -> LootAward:
    from mmgu.modules.archive.services import find_by_name
    from mmgu.modules.roster.services import find_character

    item = " ".join((item or "").split())
    if not item or len(item) > 120:
        raise EventError("Name the item (up to 120 characters).")
    character = (character or "").strip()
    if not character:
        raise EventError("Say which character received it.")
    method = next((m for m in LOOT_METHODS if m.lower() == (method or "").strip().lower()), "Other")
    pts = None
    if points not in (None, ""):
        try:
            pts = float(str(points).strip())
        except ValueError as e:
            raise EventError(f"'{points}' isn't a number of points.") from e
    ev = None
    if event_id:
        ev = await session.get(Event, int(event_id))
        if ev is None:
            raise EventError("That event doesn't exist.")
    arch = await find_by_name(session, item)
    ch = await find_character(session, character)
    award = LootAward(
        event_id=ev.id if ev else None,
        item_id=arch.id if arch else None,
        item_name=arch.name if arch else item,
        character_id=ch.id if ch else None,
        character_name=ch.name if ch else character[:1].upper() + character[1:64],
        member_id=ch.member_id if ch else None,
        method=method,
        points=pts,
        note=(note or "").strip()[:300] or None,
        awarded_by=viewer.id,
    )
    session.add(award)
    await session.flush()
    await record(
        session,
        "loot.awarded",
        actor_id=viewer.id,
        entity=award,
        via=via,
        summary=f"{award.item_name} → {award.character_name} ({method})" + (f" at {ev.title}" if ev else ""),
    )
    await hall.bus.emit("loot.awarded", actor_id=viewer.id, award_id=award.id, item_id=award.item_id)
    if award.member_id and award.member_id != viewer.id:
        await notify(
            session,
            award.member_id,
            f"{award.character_name} received {award.item_name}. Grats!",
            f"/events/{ev.id}?tab=loot" if ev else "/loot",
            dm=False,
        )
    return award


async def delete_award(session: AsyncSession, viewer: Viewer, award: LootAward, *, via: str = "web") -> None:
    before = snapshot(award)
    await record(
        session,
        "loot.removed",
        actor_id=viewer.id,
        entity_type="LootAward",
        entity_id=award.id,
        before=before,
        via=via,
        summary=f"Removed loot record: {award.item_name} → {award.character_name}",
    )
    await session.delete(award)
    await session.flush()
    await hall.bus.emit("loot.removed", actor_id=viewer.id, award_id=before["id"], item_id=before["item_id"])


async def loot_history(
    session: AsyncSession,
    *,
    member_id: int | None = None,
    item: str = "",
    item_id: int | None = None,
    event_id: int | None = None,
    character: str = "",
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 200,
) -> list[tuple[LootAward, Event | None, Member | None]]:
    stmt = (
        select(LootAward, Event, Member)
        .join(Event, Event.id == LootAward.event_id, isouter=True)
        .join(Member, Member.id == LootAward.member_id, isouter=True)
    )
    if member_id:
        stmt = stmt.where(LootAward.member_id == member_id)
    if item_id:
        stmt = stmt.where(LootAward.item_id == item_id)
    if item:
        stmt = stmt.where(LootAward.item_name.ilike(f"%{item.strip()}%"))
    if character:
        stmt = stmt.where(LootAward.character_name.ilike(f"%{character.strip()}%"))
    if event_id:
        stmt = stmt.where(LootAward.event_id == event_id)
    if since:
        stmt = stmt.where(LootAward.awarded_at >= since)
    if until:
        stmt = stmt.where(LootAward.awarded_at < until)
    stmt = stmt.order_by(LootAward.awarded_at.desc(), LootAward.id.desc()).limit(limit)
    return [(a, e, m) for a, e, m in (await session.execute(stmt)).all()]


async def item_names(session: AsyncSession, q: str, limit: int = 12) -> list[str]:
    """Archive names plus free-text names already used in loot records."""
    from mmgu.modules.archive.services import suggest_names

    names = [n for _, n in await suggest_names(session, q, limit)]
    if q.strip():
        extra = (
            await session.execute(
                select(LootAward.item_name).where(LootAward.item_name.ilike(f"%{q.strip()}%")).distinct().limit(limit)
            )
        ).scalars()
        names += [n for n in extra if n not in names]
    return names[:limit]


# ----- calendar feed ------------------------------------------------------------------------------
async def calendar_token(session: AsyncSession, member_id: int, *, regenerate: bool = False) -> str:
    row = await session.get(CalendarToken, member_id)
    if row is not None and not regenerate:
        return row.token
    tok = secrets.token_urlsafe(24)
    if row is None:
        session.add(CalendarToken(member_id=member_id, token=tok))
    else:
        row.token, row.created_at = tok, utcnow()
    await session.flush()
    if regenerate:
        await record(
            session,
            "events.calendar_token",
            actor_id=member_id,
            entity_type="CalendarToken",
            entity_id=member_id,
            summary="Regenerated calendar feed link",
        )
    return tok


async def member_for_token(session: AsyncSession, token: str) -> Member | None:
    if not token:
        return None
    row = (await session.execute(select(CalendarToken).where(CalendarToken.token == token))).scalar_one_or_none()
    return await session.get(Member, row.member_id) if row else None


def _ics_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n")
    )


def _ics_fold(line: str) -> str:
    """Fold at 75 octets as RFC 5545 requires, never splitting a UTF-8 character."""
    out, cur, size = [], "", 0
    for ch in line:
        n = len(ch.encode())
        if size + n > (75 if not out else 74):
            out.append(cur)
            cur, size = "", 0
        cur += ch
        size += n
    out.append(cur)
    return "\r\n ".join(out)


def _ics_time(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%SZ")


def ics_feed(events: list[Event], statuses: dict[int, str], *, base_url: str, calname: str) -> str:
    host = base_url.split("://", 1)[-1].split("/", 1)[0] or "hallkeeper"
    stamp = _ics_time(utcnow())
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Hallkeeper//Events//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_ics_escape(calname)}",
        "X-PUBLISHED-TTL:PT1H",
    ]
    for ev in events:
        end = ev.ends_at or ev.starts_at + timedelta(hours=2)
        mine = statuses.get(ev.id)
        summary = (f"[{SIGNUP_LABEL[mine]}] " if mine else "") + f"{ev.kind}: {ev.title}"
        url = f"{base_url.rstrip('/')}/events/{ev.id}"
        desc = (ev.description or "").strip()
        desc = (desc + "\n\n" if desc else "") + f"Sign up: {url}"
        lines += [
            "BEGIN:VEVENT",
            f"UID:event-{ev.id}@{host}",
            f"DTSTAMP:{stamp}",
            f"LAST-MODIFIED:{_ics_time(ev.updated_at or ev.created_at or utcnow())}",
            f"DTSTART:{_ics_time(ev.starts_at)}",
            f"DTEND:{_ics_time(end)}",
            f"SUMMARY:{_ics_escape(summary)}",
            f"DESCRIPTION:{_ics_escape(desc)}",
            f"URL:{url}",
            "STATUS:" + ("CANCELLED" if ev.status == "cancelled" else "CONFIRMED"),
        ]
        if ev.zone:
            lines.append(f"LOCATION:{_ics_escape(ev.zone)}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_ics_fold(line) for line in lines) + "\r\n"


async def feed_for(session: AsyncSession, member: Member) -> str:
    now = utcnow()
    events = list(
        (
            await session.execute(
                select(Event)
                .where(Event.starts_at >= now - timedelta(days=30), Event.starts_at <= now + timedelta(days=180))
                .order_by(Event.starts_at)
            )
        )
        .scalars()
        .all()
    )
    mine = {
        s.event_id: s.status
        for s in (
            await session.execute(
                select(Signup).where(Signup.member_id == member.id, Signup.event_id.in_([e.id for e in events]))
            )
        ).scalars()
    }
    return ics_feed(events, mine, base_url=hall.settings.base_url, calname=f"{hall.guild_name} events")


# ----- the minute job -----------------------------------------------------------------------------
def _setting_int(key: str, default: int) -> int:
    try:
        return int(hall.setting("events", key) or default)
    except (TypeError, ValueError):
        return default


async def tick(session: AsyncSession, now: datetime | None = None) -> dict[str, list[int]]:
    """Send reminders, start events, close old ones. Safe to run as often as you like."""
    now = now or datetime.now(UTC).replace(tzinfo=None)
    remind = timedelta(minutes=_setting_int("reminder_minutes", 30))
    close_after = timedelta(hours=_setting_int("auto_close_hours", 6))
    done: dict[str, list[int]] = {"reminded": [], "started": [], "closed": []}
    live = (
        (
            await session.execute(
                select(Event).where(Event.status.in_(("scheduled", "active")), Event.starts_at <= now + remind)
            )
        )
        .scalars()
        .all()
    )
    for ev in live:
        if not ev.reminded and now < ev.starts_at:
            ev.reminded = True
            mins = max(1, round((ev.starts_at - now).total_seconds() / 60))
            for s in await going_signups(session, ev.id, include_maybe=True):
                await notify(session, s.member_id, f"{ev.title} starts in {mins} minutes.", f"/events/{ev.id}")
            await hall.bus.emit("events.reminder", event_id=ev.id, minutes=mins)
            done["reminded"].append(ev.id)
        if ev.starts_at <= now and not ev.start_announced:
            ev.start_announced = True
            ev.reminded = True
            if ev.status == "scheduled":
                ev.status = "active"
            if now - ev.starts_at < timedelta(minutes=30):
                await hall.bus.emit("events.starting", event_id=ev.id)
            await hall.bus.emit("events.updated", event_id=ev.id)
            await record(
                session, "events.started", actor_id=None, entity=ev, via="system", summary=f"{ev.title} started"
            )
            done["started"].append(ev.id)
        if (ev.ends_at or ev.starts_at) + close_after <= now:
            ev.status = "done"
            await hall.bus.emit("events.updated", event_id=ev.id)
            await record(session, "events.closed", actor_id=None, entity=ev, via="system", summary=f"{ev.title} closed")
            done["closed"].append(ev.id)
    await session.flush()
    return done


async def to_announce(session: AsyncSession, now: datetime | None = None, days: int = 7) -> list[Event]:
    """Events not yet posted to Discord that start within ``days`` days."""
    now = now or utcnow()
    return list(
        (
            await session.execute(
                select(Event).where(
                    Event.status == "scheduled",
                    Event.discord_channel_id.is_(None),
                    Event.starts_at > now,
                    Event.starts_at <= now + timedelta(days=days),
                )
            )
        )
        .scalars()
        .all()
    )


# ----- JSON ---------------------------------------------------------------------------------------
def event_json(ev: Event, signups: int = 0) -> dict[str, Any]:
    return {
        "id": ev.id,
        "title": ev.title,
        "kind": ev.kind,
        "starts_at": ev.starts_at.isoformat() + "Z",
        "ends_at": ev.ends_at.isoformat() + "Z" if ev.ends_at else None,
        "zone": ev.zone,
        "status": ev.status,
        "signups": signups,
        "url": hall.settings.base_url.rstrip("/") + f"/events/{ev.id}",
    }


async def delete_attendance(session: AsyncSession, viewer: Viewer, event: Event, attendance_id: int) -> None:
    a = await session.get(Attendance, attendance_id)
    if a is None or a.event_id != event.id:
        return
    await record(
        session,
        "events.attendance_removed",
        actor_id=viewer.id,
        entity=a,
        summary=f"Removed an attendee from {event.title}",
    )
    await session.execute(delete(Attendance).where(Attendance.id == attendance_id))
    await hall.bus.emit("events.attendance", actor_id=viewer.id, event_id=event.id)


async def next_events(session: AsyncSession, limit: int = 3) -> list[dict[str, Any]]:
    """Upcoming (and running) events for other modules, e.g. the stream overlay.

    Each dict: id, title, starts_at (naive UTC datetime), zone, going (count of Going/Late sign-ups).
    """
    events = await upcoming(session, limit=limit)
    counts = await signup_counts(session, [e.id for e in events])
    return [
        {
            "id": e.id,
            "title": e.title,
            "starts_at": e.starts_at,
            "zone": e.zone,
            "going": counts[e.id].get("going", 0) + counts[e.id].get("late", 0),
        }
        for e in events
    ]
