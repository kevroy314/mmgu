"""Read-only bridges to other modules. Each returns None when the other module is off or missing.

The overlay never imports another module at import time, so it works with any mix of modules.
Expected (optional) functions in other modules:

- ``mmgu.modules.watch.services.timer_states(session) -> list[TimerState]`` (sorted, soonest first;
  ``TimerState.json()`` has id, creature, zone, window_start, window_end, status)
- ``mmgu.modules.events.services.next_events(session, limit) -> list[dict]``
  dict keys: id, title, starts_at, zone, going
- ``mmgu.modules.board.services.overlay_line(session, kind, id) -> str | None`` (a one-line summary)
"""

from __future__ import annotations

import importlib
import logging
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from mmgu.core.hall import hall

log = logging.getLogger(__name__)


def iso(value: Any) -> str | None:
    """Datetimes (naive UTC) become ISO strings with a Z so the browser reads them as UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            from datetime import UTC

            value = value.astimezone(UTC).replace(tzinfo=None)
        return value.isoformat(timespec="seconds") + "Z"
    return str(value)


def _function(module_id: str, name: str):
    if not hall.is_enabled(module_id):
        return None
    try:
        services = importlib.import_module(f"mmgu.modules.{module_id}.services")
        return getattr(services, name)
    except (ImportError, AttributeError):
        return None


async def _call(module_id: str, name: str, *args: Any) -> Any:
    fn = _function(module_id, name)
    if fn is None:
        return None
    try:
        return await fn(*args)
    except Exception:  # noqa: BLE001 - another module's bug must not blank the overlay
        log.exception("overlay: %s.services.%s failed", module_id, name)
        return None


async def _watch_rows(session: AsyncSession) -> list[dict] | None:
    """Open and upcoming spawn windows, soonest first, from ``watch.services.timer_states``."""
    states = await _call("watch", "timer_states", session)
    if states is None:
        return None
    try:
        return [s.json() for s in states if s.status in ("open", "waiting")]
    except Exception:  # noqa: BLE001
        log.exception("overlay: could not read watch timer states")
        return None


async def upcoming_windows(session: AsyncSession, limit: int) -> list[dict] | None:
    rows = await _watch_rows(session)
    if rows is None:
        return None
    out = []
    for r in list(rows)[:limit]:
        r = dict(r)
        out.append(
            {
                "id": r.get("id"),
                "creature": str(r.get("creature") or "?"),
                "zone": r.get("zone") or "",
                "window_start": iso(r.get("window_start")),
                "window_end": iso(r.get("window_end")),
                "status": r.get("status") or "",
            }
        )
    return out


async def next_events(session: AsyncSession, limit: int) -> list[dict] | None:
    rows = await _call("events", "next_events", session, limit)
    if rows is None:
        return None
    out = []
    for r in list(rows)[:limit]:
        r = dict(r)
        out.append(
            {
                "id": r.get("id"),
                "title": str(r.get("title") or "Event"),
                "starts_at": iso(r.get("starts_at")),
                "zone": r.get("zone") or "",
                "going": int(r.get("going") or 0),
            }
        )
    return out


async def timer(session: AsyncSession, timer_id: Any) -> dict | None:
    for t in await upcoming_windows(session, 50) or []:
        if str(t["id"]) == str(timer_id):
            return t
    return None


async def event(session: AsyncSession, event_id: Any) -> dict | None:
    for e in await next_events(session, 20) or []:
        if str(e["id"]) == str(event_id):
            return e
    return None


async def board_line(session: AsyncSession, kind: str, item_id: Any) -> str | None:
    line = await _call("board", "overlay_line", session, kind, item_id)
    return str(line) if line else None
