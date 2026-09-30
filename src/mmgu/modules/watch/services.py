"""The Watch's logic shared by the web pages, the Discord bot, the JSON API, jobs and add-ons.

Times: everything is stored as naive UTC. The web form sends explicit times as the browser's local
time plus a hidden ``tz_offset`` (minutes, from JS ``new Date().getTimezoneOffset()``); without it the
time is read as UTC. Discord and the API only take "N minutes ago", which needs no timezone at all.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mmgu.core.audit import record, snapshot
from mmgu.core.auth import Viewer
from mmgu.core.hall import hall
from mmgu.core.models import Character, Member
from mmgu.db import session_scope, utcnow
from mmgu.modules.watch.models import Camp, DeathReport, Timer, Watcher

log = logging.getLogger(__name__)

MAX_AGO_MINUTES = 7 * 24 * 60
DUPLICATE_WITHIN = timedelta(minutes=2)
STATUS_LABEL = {
    "open": "Window open",
    "waiting": "Waiting",
    "overdue": "Past window",
    "unknown": "No time of death",
}


class WatchError(ValueError):
    pass


def name_key(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower().replace("’", "'"))


def _int(v: Any, what: str, lo: int, hi: int, default: int | None = None) -> int:
    if v is None or str(v).strip() == "":
        if default is not None:
            return default
        raise WatchError(f"{what} is required.")
    try:
        n = int(float(str(v).strip()))
    except ValueError as e:
        raise WatchError(f"{what} should be a whole number.") from e
    if not lo <= n <= hi:
        raise WatchError(f"{what} should be between {lo} and {hi}.")
    return n


def _hours(v: Any, default: float) -> float:
    try:
        return max(0.25, float(v))
    except (TypeError, ValueError):
        return default


def alert_minutes() -> int:
    try:
        return max(0, int(hall.setting("watch", "alert_minutes_before") or 0))
    except (TypeError, ValueError):
        return 10


def camp_hours() -> float:
    return _hours(hall.setting("watch", "camp_hours"), 4.0)


# ----- turning "when did it die?" into a UTC time ---------------------------------------------
def parse_when(
    when: str | None = "now",
    minutes_ago: Any = None,
    at: str | None = None,
    tz_offset: Any = None,
    now: datetime | None = None,
) -> datetime:
    """``when`` is now | ago | at. ``at`` is a browser datetime-local value (local time) and
    ``tz_offset`` the browser's getTimezoneOffset() in minutes (UTC = local + offset)."""
    now = now or utcnow()
    when = (when or "now").strip().lower()
    if when == "ago" or (when == "now" and minutes_ago not in (None, "", "0", 0)):
        return now - timedelta(minutes=_int(minutes_ago, "Minutes ago", 0, MAX_AGO_MINUTES))
    if when == "at":
        raw = (at or "").strip()
        if not raw:
            raise WatchError("Pick the date and time it died.")
        try:
            local = datetime.fromisoformat(raw.replace("Z", ""))
        except ValueError as e:
            raise WatchError("That time isn't readable. Use the date/time picker.") from e
        offset = 0
        if tz_offset not in (None, ""):
            offset = _int(tz_offset, "Time zone offset", -24 * 60, 24 * 60)
        died = local.replace(tzinfo=None) + timedelta(minutes=offset)
        if died > now + timedelta(minutes=5):
            raise WatchError("That time is in the future. Check the date, and the time zone on your device.")
        if died < now - timedelta(minutes=MAX_AGO_MINUTES):
            raise WatchError("That's more than a week ago; report a more recent death.")
        return died
    return now


# ----- timers ---------------------------------------------------------------------------------
@dataclass
class TimerState:
    timer: Timer
    death: DeathReport | None
    reporter: Member | None
    window_start: datetime | None
    window_end: datetime | None
    status: str
    watching: bool = False
    watchers: int = 0

    @property
    def label(self) -> str:
        return STATUS_LABEL[self.status]

    @property
    def target(self) -> datetime | None:
        """What the big countdown counts toward."""
        if self.status == "waiting":
            return self.window_start
        if self.status == "open":
            return self.window_end
        return None

    def json(self) -> dict[str, Any]:
        def iso(d):
            return d.isoformat(timespec="seconds") + "Z" if d else None

        return {
            "id": self.timer.id,
            "creature": self.timer.creature,
            "zone": self.timer.zone,
            "window_start": iso(self.window_start),
            "window_end": iso(self.window_end),
            "status": self.status,
            "last_death": iso(self.death.died_at if self.death else None),
            "respawn_minutes": self.timer.respawn_minutes,
            "variance_minutes": self.timer.variance_minutes,
        }


def window(timer: Timer, died_at: datetime) -> tuple[datetime, datetime]:
    spawn = died_at + timedelta(minutes=timer.respawn_minutes)
    var = timedelta(minutes=timer.variance_minutes or 0)
    return spawn - var, spawn + var


def status_of(ws: datetime | None, we: datetime | None, now: datetime) -> str:
    if ws is None or we is None:
        return "unknown"
    if now < ws:
        return "waiting"
    if now <= we:
        return "open"
    return "overdue"


def _sort_key(s: TimerState, now: datetime):
    order = {"open": 0, "waiting": 1, "overdue": 2, "unknown": 3}[s.status]
    if s.status == "open":
        k = (s.window_end - now).total_seconds()
    elif s.status == "waiting":
        k = (s.window_start - now).total_seconds()
    elif s.status == "overdue":
        k = (now - s.window_end).total_seconds()
    else:
        k = 0
    return (order, k, s.timer.creature.lower())


async def latest_deaths(session: AsyncSession, timer_ids: list[int]) -> dict[int, DeathReport]:
    if not timer_ids:
        return {}
    sub = (
        select(DeathReport.timer_id, func.max(DeathReport.died_at).label("m"))
        .where(DeathReport.timer_id.in_(timer_ids))
        .group_by(DeathReport.timer_id)
        .subquery()
    )
    rows = (
        (
            await session.execute(
                select(DeathReport)
                .join(sub, (sub.c.timer_id == DeathReport.timer_id) & (sub.c.m == DeathReport.died_at))
                .order_by(DeathReport.id)
            )
        )
        .scalars()
        .all()
    )
    return {d.timer_id: d for d in rows}


async def timer_states(
    session: AsyncSession,
    viewer_id: int | None = None,
    *,
    include_inactive: bool = False,
    timer_ids: list[int] | None = None,
    now: datetime | None = None,
) -> list[TimerState]:
    now = now or utcnow()
    stmt = select(Timer)
    if not include_inactive:
        stmt = stmt.where(Timer.active.is_(True))
    if timer_ids is not None:
        stmt = stmt.where(Timer.id.in_(timer_ids))
    timers = list((await session.execute(stmt)).scalars().all())
    ids = [t.id for t in timers]
    deaths = await latest_deaths(session, ids)
    reporter_ids = {d.reported_by for d in deaths.values() if d.reported_by}
    reporters = {}
    if reporter_ids:
        reporters = {
            m.id: m for m in (await session.execute(select(Member).where(Member.id.in_(reporter_ids)))).scalars()
        }
    counts: dict[int, int] = {}
    mine: set[int] = set()
    if ids:
        counts = dict(
            (
                await session.execute(
                    select(Watcher.timer_id, func.count()).where(Watcher.timer_id.in_(ids)).group_by(Watcher.timer_id)
                )
            ).all()
        )
        if viewer_id:
            mine = set(
                (
                    await session.execute(
                        select(Watcher.timer_id).where(Watcher.timer_id.in_(ids), Watcher.member_id == viewer_id)
                    )
                ).scalars()
            )
    out = []
    for t in timers:
        d = deaths.get(t.id)
        ws, we = window(t, d.died_at) if d else (None, None)
        out.append(
            TimerState(
                timer=t,
                death=d,
                reporter=reporters.get(d.reported_by) if d and d.reported_by else None,
                window_start=ws,
                window_end=we,
                status=status_of(ws, we, now),
                watching=t.id in mine,
                watchers=counts.get(t.id, 0),
            )
        )
    out.sort(key=lambda s: _sort_key(s, now))
    return out


async def timer_state(session: AsyncSession, timer: Timer, viewer_id: int | None = None) -> TimerState:
    return (await timer_states(session, viewer_id, include_inactive=True, timer_ids=[timer.id]))[0]


async def find_timer(session: AsyncSession, name: str) -> Timer | None:
    return (await session.execute(select(Timer).where(Timer.creature_key == name_key(name)))).scalar_one_or_none()


async def suggest_timers(session: AsyncSession, q: str, limit: int = 25) -> list[Timer]:
    stmt = select(Timer).where(Timer.active.is_(True))
    if q.strip():
        stmt = stmt.where(Timer.creature.ilike(f"%{q.strip()}%"))
    return list((await session.execute(stmt.order_by(Timer.creature).limit(limit))).scalars().all())


async def save_timer(
    session: AsyncSession, viewer: Viewer, data: dict[str, Any], *, timer: Timer | None = None, via: str = "web"
) -> Timer:
    if not viewer.can("watch.manage"):
        raise WatchError("Only officers and raid leaders can add or change tracked creatures.")
    creature = re.sub(r"\s+", " ", str(data.get("creature") or "").strip())[:120]
    if not creature:
        raise WatchError("Name the creature, e.g. 'Lord Grimjaw'.")
    respawn = _int(data.get("respawn_minutes"), "Respawn time (minutes)", 1, 60 * 24 * 14)
    variance = _int(data.get("variance_minutes"), "Variance (minutes)", 0, respawn, default=0)
    clash = await find_timer(session, creature)
    if clash is not None and (timer is None or clash.id != timer.id):
        raise WatchError(f"{clash.creature} is already tracked. Edit that timer instead.")
    before = snapshot(timer) if timer else None
    if timer is None:
        timer = Timer(creature=creature, creature_key=name_key(creature), respawn_minutes=respawn, created_by=viewer.id)
        session.add(timer)
    timer.creature = creature
    timer.creature_key = name_key(creature)
    timer.zone = re.sub(r"\s+", " ", str(data.get("zone") or "").strip())[:120] or None
    timer.respawn_minutes = respawn
    timer.variance_minutes = variance
    timer.notes = str(data.get("notes") or "").strip()[:2000] or None
    if "active" in data:
        timer.active = bool(data.get("active")) and str(data.get("active")).lower() not in ("0", "false", "")
    await session.flush()
    await record(
        session,
        "watch.timer_saved",
        actor_id=viewer.id,
        entity=timer,
        before=before,
        via=via,
        summary=f"Timer: {creature} ({respawn}m ± {variance}m)",
    )
    await hall.bus.emit("watch.timer_saved", actor_id=viewer.id, timer_id=timer.id)
    return timer


async def delete_timer(session: AsyncSession, viewer: Viewer, timer: Timer, *, via: str = "web") -> None:
    if not viewer.can("watch.manage"):
        raise WatchError("Only officers and raid leaders can remove tracked creatures.")
    await record(
        session,
        "watch.timer_deleted",
        actor_id=viewer.id,
        entity=timer,
        before=snapshot(timer),
        after={},
        via=via,
        summary=f"Stopped tracking {timer.creature}",
    )
    await session.delete(timer)
    await session.flush()


# ----- deaths ---------------------------------------------------------------------------------
async def report_death(
    session: AsyncSession,
    viewer: Viewer | None,
    timer: Timer,
    died_at: datetime,
    *,
    source: str = "manual",
    note: str | None = None,
    via: str = "web",
) -> DeathReport:
    """Record a time of death. A report within two minutes of an existing one is treated as the same kill."""
    if viewer is not None and not viewer.can("watch.report"):
        raise WatchError("Your rank can't report times of death yet.")
    dupe = (
        await session.execute(
            select(DeathReport).where(
                DeathReport.timer_id == timer.id,
                DeathReport.died_at >= died_at - DUPLICATE_WITHIN,
                DeathReport.died_at <= died_at + DUPLICATE_WITHIN,
            )
        )
    ).scalar_one_or_none()
    if dupe is not None:
        return dupe
    death = DeathReport(
        timer_id=timer.id,
        died_at=died_at,
        reported_by=viewer.id if viewer else None,
        source=(source or "manual")[:20],
        note=(note or "").strip()[:200] or None,
    )
    ws, _ = window(timer, died_at)
    if ws <= utcnow():
        # reported so late the window has already opened: don't alert for it
        death.soon_sent = death.open_sent = True
    session.add(death)
    await session.flush()
    await record(
        session,
        "watch.tod_reported",
        actor_id=viewer.id if viewer else None,
        entity=death,
        via=via,
        summary=f"{timer.creature} died at {died_at:%Y-%m-%d %H:%M} UTC ({death.source})",
    )
    await hall.bus.emit(
        "watch.tod_reported", actor_id=viewer.id if viewer else None, timer_id=timer.id, death_id=death.id
    )
    return death


async def report_death_by_name(
    session: AsyncSession, viewer: Viewer | None, name: str, died_at: datetime, source: str = "manual"
) -> DeathReport | None:
    """For add-ons (the ledger): report a death by creature name; None when no timer tracks it."""
    timer = await find_timer(session, name)
    if timer is None or not timer.active:
        return None
    if died_at.tzinfo is not None:
        died_at = died_at.astimezone(UTC).replace(tzinfo=None)
    via = source if source in ("discord", "api", "ledger") else "web"
    return await report_death(session, viewer, timer, died_at, source=source, via=via)


async def delete_death(session: AsyncSession, viewer: Viewer, death: DeathReport, *, via: str = "web") -> None:
    if not (viewer.can("watch.manage") or (death.reported_by and death.reported_by == viewer.id)):
        raise WatchError("You can only remove times of death you reported.")
    await record(
        session,
        "watch.tod_removed",
        actor_id=viewer.id,
        entity=death,
        before=snapshot(death),
        after={},
        via=via,
        summary="Removed a time of death",
    )
    await session.delete(death)
    await session.flush()
    await hall.bus.emit("watch.tod_removed", actor_id=viewer.id, timer_id=death.timer_id, death_id=death.id)


async def history(session: AsyncSession, timer_id: int, limit: int = 30) -> list[tuple[DeathReport, Member | None]]:
    return [
        (d, m)
        for d, m in (
            await session.execute(
                select(DeathReport, Member)
                .join(Member, Member.id == DeathReport.reported_by, isouter=True)
                .where(DeathReport.timer_id == timer_id)
                .order_by(DeathReport.died_at.desc())
                .limit(limit)
            )
        ).all()
    ]


# ----- watchers -------------------------------------------------------------------------------
async def toggle_watch(session: AsyncSession, viewer: Viewer, timer: Timer, *, via: str = "web") -> bool:
    """Watch or stop watching a timer; returns True when now watching."""
    if not viewer.can("watch.view") or not viewer.id:
        raise WatchError("Log in to watch timers.")
    row = (
        await session.execute(select(Watcher).where(Watcher.timer_id == timer.id, Watcher.member_id == viewer.id))
    ).scalar_one_or_none()
    if row is not None:
        await session.delete(row)
        watching = False
    else:
        session.add(Watcher(timer_id=timer.id, member_id=viewer.id))
        watching = True
    await session.flush()
    await record(
        session,
        "watch.watching" if watching else "watch.unwatched",
        actor_id=viewer.id,
        entity=timer,
        via=via,
        summary=f"{'Watching' if watching else 'Stopped watching'} {timer.creature}",
    )
    await hall.bus.emit("watch.watch_toggled", actor_id=viewer.id, timer_id=timer.id, watching=watching)
    return watching


async def watcher_ids(session: AsyncSession, timer_id: int) -> list[int]:
    return list((await session.execute(select(Watcher.member_id).where(Watcher.timer_id == timer_id))).scalars())


# ----- camps ----------------------------------------------------------------------------------
def _camp_cutoff(now: datetime | None = None) -> datetime:
    return (now or utcnow()) - timedelta(hours=camp_hours())


async def active_camps(session: AsyncSession) -> list[tuple[Camp, Member | None]]:
    rows = (
        await session.execute(
            select(Camp, Member)
            .join(Member, Member.id == Camp.member_id, isouter=True)
            .where(Camp.ended_at.is_(None), Camp.started_at >= _camp_cutoff())
            .order_by(Camp.zone, Camp.camp)
        )
    ).all()
    return [(c, m) for c, m in rows]


def camp_expires(camp: Camp) -> datetime:
    return camp.started_at + timedelta(hours=camp_hours())


async def start_camp(
    session: AsyncSession,
    viewer: Viewer,
    zone: str,
    camp: str,
    character: str | None = None,
    note: str | None = None,
    *,
    via: str = "web",
) -> Camp:
    if not viewer.can("watch.report"):
        raise WatchError("Your rank can't check in at camps yet.")
    zone = re.sub(r"\s+", " ", (zone or "").strip())[:120]
    camp = re.sub(r"\s+", " ", (camp or "").strip())[:120]
    if not zone or not camp:
        raise WatchError("Give both the zone and the camp, e.g. 'Scarwood' and 'Ogre hill'.")
    ch = None
    cname = (character or "").strip()[:64] or None
    if cname:
        ch = (
            await session.execute(
                select(Character).where(Character.member_id == viewer.id, func.lower(Character.name) == cname.lower())
            )
        ).scalar_one_or_none()
    else:
        ch = (
            (
                await session.execute(
                    select(Character)
                    .where(Character.member_id == viewer.id, Character.status == "active")
                    .order_by(Character.is_main.desc(), Character.id)
                )
            )
            .scalars()
            .first()
        )
    for old in await my_camps(session, viewer):
        await _end(session, viewer, old, "replaced", via)
    row = Camp(
        zone=zone,
        camp=camp,
        member_id=viewer.id,
        character_id=ch.id if ch else None,
        character_name=ch.name if ch else cname,
        note=(note or "").strip()[:200] or None,
    )
    session.add(row)
    await session.flush()
    await record(
        session,
        "watch.camp_started",
        actor_id=viewer.id,
        entity=row,
        via=via,
        summary=f"{row.character_name or viewer.name} is holding {camp} in {zone}",
    )
    await hall.bus.emit("watch.camp_started", actor_id=viewer.id, camp_id=row.id)
    return row


async def my_camps(session: AsyncSession, viewer: Viewer) -> list[Camp]:
    return list(
        (
            await session.execute(
                select(Camp).where(
                    Camp.member_id == viewer.id, Camp.ended_at.is_(None), Camp.started_at >= _camp_cutoff()
                )
            )
        ).scalars()
    )


async def _end(session: AsyncSession, viewer: Viewer | None, camp: Camp, reason: str, via: str) -> None:
    camp.ended_at = utcnow()
    camp.end_reason = reason
    await record(
        session,
        "watch.camp_ended",
        actor_id=viewer.id if viewer else None,
        entity=camp,
        via=via,
        summary=f"{camp.camp} in {camp.zone}: {reason}",
    )
    await hall.bus.emit("watch.camp_ended", actor_id=viewer.id if viewer else None, camp_id=camp.id)


async def end_camp(session: AsyncSession, viewer: Viewer, camp: Camp, *, via: str = "web") -> None:
    if camp.ended_at is not None:
        return
    if camp.member_id != viewer.id and not viewer.can("watch.manage"):
        raise WatchError("Only the person holding the camp, or an officer, can end it.")
    await _end(session, viewer, camp, "done", via)
    await session.flush()


async def end_my_camps(session: AsyncSession, viewer: Viewer, *, via: str = "web") -> list[Camp]:
    camps = await my_camps(session, viewer)
    for c in camps:
        await _end(session, viewer, c, "done", via)
    await session.flush()
    return camps


async def expire_camps(session: AsyncSession, now: datetime | None = None) -> int:
    rows = (
        (await session.execute(select(Camp).where(Camp.ended_at.is_(None), Camp.started_at < _camp_cutoff(now))))
        .scalars()
        .all()
    )
    for c in rows:
        c.ended_at = camp_expires(c)
        c.end_reason = "expired"
        await record(
            session,
            "watch.camp_ended",
            actor_id=None,
            entity=c,
            via="system",
            summary=f"{c.camp} in {c.zone}: expired after {camp_hours():g}h",
        )
    return len(rows)


# ----- the 30-second job -----------------------------------------------------------------------
async def check_windows(session: AsyncSession, now: datetime | None = None) -> list[int]:
    """Send "soon" and "open" alerts for the latest death of every active timer. Returns timer ids
    whose window just opened (``watch.window_open`` has been emitted for each, exactly once)."""
    now = now or utcnow()
    pending = (
        (
            await session.execute(
                select(DeathReport).where((DeathReport.open_sent.is_(False)) | (DeathReport.soon_sent.is_(False)))
            )
        )
        .scalars()
        .all()
    )
    if not pending:
        return []
    timer_ids = list({d.timer_id for d in pending})
    latest = await latest_deaths(session, timer_ids)
    timers = {t.id: t for t in (await session.execute(select(Timer).where(Timer.id.in_(timer_ids)))).scalars()}
    lead = alert_minutes()
    opened: list[int] = []
    from mmgu.core.notify import notify

    for d in pending:
        t = timers.get(d.timer_id)
        if t is None or latest.get(d.timer_id) is not d or not t.active:
            d.soon_sent = d.open_sent = True  # superseded by a newer report, or timer switched off
            continue
        ws, we = window(t, d.died_at)
        url = f"/watch#timer-{t.id}"
        where = f" in {t.zone}" if t.zone else ""
        if not d.open_sent and now >= ws:
            d.soon_sent = d.open_sent = True
            if now > we + timedelta(minutes=5):
                continue  # far too late to be useful
            closes = f", closes {we:%H:%M} UTC" if we > ws else ""
            for mid in await watcher_ids(session, t.id):
                await notify(session, mid, f"{t.creature}{where}: spawn window is open now{closes}.", url)
            await hall.bus.emit("watch.window_open", timer_id=t.id, death_id=d.id)
            opened.append(t.id)
        elif not d.soon_sent and lead and now >= ws - timedelta(minutes=lead):
            d.soon_sent = True
            mins = max(1, round((ws - now).total_seconds() / 60))
            for mid in await watcher_ids(session, t.id):
                await notify(session, mid, f"{t.creature}{where}: spawn window opens in about {mins} min.", url)
            await hall.bus.emit("watch.window_soon", timer_id=t.id, death_id=d.id, minutes=mins)
    await session.flush()
    return opened


async def run_tick() -> None:
    async with session_scope() as session:
        await check_windows(session)
        await expire_camps(session)
