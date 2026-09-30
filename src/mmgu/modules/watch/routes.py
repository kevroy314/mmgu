from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from mmgu.core.auth import Viewer, require
from mmgu.core.hall import hall
from mmgu.core.web import redirect, render
from mmgu.db import get_session
from mmgu.modules.watch import services
from mmgu.modules.watch.models import Camp, DeathReport, Timer

router = APIRouter()


def _when(form) -> dict:
    at = (form.get("at") or "").strip()
    ago = (form.get("minutes_ago") or "").strip()
    when = form.get("when") or ("at" if at else "ago" if ago else "now")
    return {"when": when, "minutes_ago": ago, "at": at, "tz_offset": form.get("tz_offset")}


async def _board_ctx(session: AsyncSession, viewer: Viewer) -> dict:
    states = await services.timer_states(session, viewer.id)
    camps = await services.active_camps(session)
    from mmgu.modules.roster.services import characters_of

    return {
        "states": states,
        "camps": camps,
        "camp_expires": services.camp_expires,
        "camp_hours": services.camp_hours(),
        "my_chars": await characters_of(session, viewer.id) if viewer.id else [],
        "zones": hall.game.zones,
    }


@router.get("/watch", response_class=HTMLResponse)
async def board(
    request: Request, viewer: Viewer = Depends(require("watch.view")), session: AsyncSession = Depends(get_session)
):
    ctx = await _board_ctx(session, viewer)
    inactive = []
    if viewer.can("watch.manage"):
        inactive = [s for s in await services.timer_states(session, include_inactive=True) if not s.timer.active]
    return render(request, "watch/index.html", inactive=inactive, **ctx)


async def _timer(session: AsyncSession, timer_id: int) -> Timer:
    t = await session.get(Timer, timer_id)
    if t is None:
        raise HTTPException(404, "That timer isn't tracked any more.")
    return t


@router.get("/watch/timers/{timer_id}", response_class=HTMLResponse)
async def timer_page(
    request: Request,
    timer_id: int,
    viewer: Viewer = Depends(require("watch.view")),
    session: AsyncSession = Depends(get_session),
):
    t = await _timer(session, timer_id)
    state = await services.timer_state(session, t, viewer.id)
    hist = await services.history(session, t.id)
    return render(request, "watch/timer.html", s=state, t=t, history=hist)


@router.post("/watch/timers")
async def create_timer(
    request: Request, viewer: Viewer = Depends(require("watch.manage")), session: AsyncSession = Depends(get_session)
):
    form = await request.form()
    try:
        t = await services.save_timer(session, viewer, dict(form))
    except services.WatchError as e:
        raise HTTPException(422, str(e)) from e
    await session.commit()
    return redirect(request, f"/watch#timer-{t.id}", f"Now tracking {t.creature}. Report a time of death to start it.")


@router.post("/watch/timers/{timer_id}")
async def edit_timer(
    request: Request,
    timer_id: int,
    viewer: Viewer = Depends(require("watch.manage")),
    session: AsyncSession = Depends(get_session),
):
    t = await _timer(session, timer_id)
    form = dict(await request.form())
    form.setdefault("active", "")
    try:
        await services.save_timer(session, viewer, form, timer=t)
    except services.WatchError as e:
        raise HTTPException(422, str(e)) from e
    await session.commit()
    return redirect(request, f"/watch/timers/{t.id}", f"Saved {t.creature}.")


@router.post("/watch/timers/{timer_id}/delete")
async def delete_timer(
    request: Request,
    timer_id: int,
    viewer: Viewer = Depends(require("watch.manage")),
    session: AsyncSession = Depends(get_session),
):
    t = await _timer(session, timer_id)
    name = t.creature
    await services.delete_timer(session, viewer, t)
    await session.commit()
    return redirect(request, "/watch", f"Stopped tracking {name}.")


@router.post("/watch/timers/{timer_id}/tod")
async def report_tod(
    request: Request,
    timer_id: int,
    viewer: Viewer = Depends(require("watch.report")),
    session: AsyncSession = Depends(get_session),
):
    t = await _timer(session, timer_id)
    form = await request.form()
    try:
        died = services.parse_when(**_when(form))
        await services.report_death(session, viewer, t, died, note=form.get("note"))
    except services.WatchError as e:
        raise HTTPException(422, str(e)) from e
    await session.commit()
    back = form.get("next") or f"/watch#timer-{t.id}"
    if not back.startswith("/"):
        back = "/watch"
    return redirect(request, back, f"Time of death recorded for {t.creature}.")


@router.post("/watch/tod")
async def report_tod_form(
    request: Request, viewer: Viewer = Depends(require("watch.report")), session: AsyncSession = Depends(get_session)
):
    """The report form on the board: pick the creature by id (select) or name."""
    form = await request.form()
    t = None
    if (form.get("timer_id") or "").isdigit():
        t = await session.get(Timer, int(form["timer_id"]))
    elif form.get("creature"):
        t = await services.find_timer(session, form["creature"])
    if t is None:
        raise HTTPException(422, "Pick one of the tracked creatures. Officers can add new ones.")
    try:
        died = services.parse_when(**_when(form))
        await services.report_death(session, viewer, t, died, note=form.get("note"))
    except services.WatchError as e:
        raise HTTPException(422, str(e)) from e
    await session.commit()
    return redirect(request, f"/watch#timer-{t.id}", f"Time of death recorded for {t.creature}.")


@router.post("/watch/deaths/{death_id}/delete")
async def delete_death(
    request: Request,
    death_id: int,
    viewer: Viewer = Depends(require("watch.report")),
    session: AsyncSession = Depends(get_session),
):
    d = await session.get(DeathReport, death_id)
    if d is None:
        raise HTTPException(404, "That report was already removed.")
    tid = d.timer_id
    try:
        await services.delete_death(session, viewer, d)
    except services.WatchError as e:
        raise HTTPException(403, str(e)) from e
    await session.commit()
    return redirect(request, f"/watch/timers/{tid}", "Time of death removed. The window now uses the previous report.")


@router.post("/watch/timers/{timer_id}/watch")
async def toggle_watch(
    request: Request,
    timer_id: int,
    viewer: Viewer = Depends(require("watch.view")),
    session: AsyncSession = Depends(get_session),
):
    t = await _timer(session, timer_id)
    watching = await services.toggle_watch(session, viewer, t)
    await session.commit()
    msg = (
        f"Watching {t.creature}. You'll get a notice (and a Discord DM if linked) when the window opens."
        if watching
        else f"Stopped watching {t.creature}."
    )
    form = await request.form()
    back = form.get("next") or f"/watch#timer-{t.id}"
    return redirect(request, back if back.startswith("/") else "/watch", msg)


@router.post("/watch/camps")
async def start_camp(
    request: Request, viewer: Viewer = Depends(require("watch.report")), session: AsyncSession = Depends(get_session)
):
    form = await request.form()
    try:
        c = await services.start_camp(
            session, viewer, form.get("zone") or "", form.get("camp") or "", form.get("character"), form.get("note")
        )
    except services.WatchError as e:
        raise HTTPException(422, str(e)) from e
    await session.commit()
    return redirect(request, "/watch#camps", f"Checked in at {c.camp} ({c.zone}). Mark it done when you leave.")


@router.post("/watch/camps/{camp_id}/end")
async def end_camp(
    request: Request,
    camp_id: int,
    viewer: Viewer = Depends(require("watch.report")),
    session: AsyncSession = Depends(get_session),
):
    c = await session.get(Camp, camp_id)
    if c is None:
        raise HTTPException(404)
    try:
        await services.end_camp(session, viewer, c)
    except services.WatchError as e:
        raise HTTPException(403, str(e)) from e
    await session.commit()
    return redirect(request, "/watch#camps", f"{c.camp} is free.")


# ----- JSON API -----------------------------------------------------------------------------------
@router.get("/api/watch/timers")
async def api_timers(viewer: Viewer = Depends(require("watch.view")), session: AsyncSession = Depends(get_session)):
    return {"timers": [s.json() for s in await services.timer_states(session, viewer.id)]}


@router.post("/api/watch/tod")
async def api_tod(
    request: Request, viewer: Viewer = Depends(require("watch.report")), session: AsyncSession = Depends(get_session)
):
    body = await request.json()
    t = await services.find_timer(session, str(body.get("creature") or ""))
    if t is None or not t.active:
        raise HTTPException(404, "No tracked creature by that name.")
    try:
        died = services.parse_when("ago", body.get("minutes_ago") or 0)
        d = await services.report_death(session, viewer, t, died, source="api", via="api")
    except services.WatchError as e:
        raise HTTPException(422, str(e)) from e
    ws, we = services.window(t, d.died_at)
    await session.commit()
    return {
        "timer_id": t.id,
        "window_start": ws.isoformat(timespec="seconds") + "Z",
        "window_end": we.isoformat(timespec="seconds") + "Z",
    }
