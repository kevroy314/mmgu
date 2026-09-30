"""How the Beacon listens to the rest of the hall."""

from __future__ import annotations

import logging

from mmgu.core.bus import HallEvent
from mmgu.db import session_scope

log = logging.getLogger(__name__)

# Events that become something on stream.
SHOWN = ("overlay.push", "archive.item_created", "watch.window_open", "events.starting", "board.posted")
# Events that only mean "the timers/next-event panels may be out of date".
REFRESH = (
    "watch.tod_reported",
    "watch.window_open",
    "events.created",
    "events.updated",
    "events.starting",
)


def register(hall) -> None:
    from mmgu.modules.overlay import services
    from mmgu.modules.overlay.hub import hub

    async def show(e: HallEvent) -> None:
        if not hall.is_enabled("overlay"):
            return
        async with session_scope() as session:
            out = await services.message_for(session, e)
        if out is not None:
            msg, scene_id = out
            hub.publish(msg, scene_id=scene_id)

    async def refresh(e: HallEvent) -> None:
        if hall.is_enabled("overlay") and hub.count():
            hub.publish({"kind": "refresh"})

    for name in SHOWN:
        hall.bus.subscribe(name, show)
    for name in REFRESH:
        hall.bus.subscribe(name, refresh)
