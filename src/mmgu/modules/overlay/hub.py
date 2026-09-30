"""In-process pub/sub between bus handlers and open overlay streams.

Each open ``/overlay/<token>/stream`` connection subscribes with its scene id and the feeds that
scene shows; it gets its own bounded queue. Publishing never blocks: a stalled browser loses its
oldest messages rather than holding up the hall (it catches up from ``/state`` on reconnect).
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any

QUEUE_SIZE = 100
CLOSE = object()  # sentinel: the stream should end (scene deleted or its URL regenerated)


@dataclass(eq=False)
class Subscription:
    scene_id: int
    feeds: set[str]
    active: bool = True
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(QUEUE_SIZE))

    def offer(self, item: Any) -> None:
        while True:
            try:
                self.queue.put_nowait(item)
                return
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    self.queue.get_nowait()


class Hub:
    def __init__(self) -> None:
        self._subs: set[Subscription] = set()

    def subscribe(self, scene_id: int, feeds: set[str] | list[str], active: bool = True) -> Subscription:
        sub = Subscription(scene_id, set(feeds), active)
        self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        self._subs.discard(sub)

    def count(self, scene_id: int | None = None) -> int:
        return sum(1 for s in self._subs if scene_id is None or s.scene_id == scene_id)

    def publish(self, message: dict[str, Any], scene_id: int | None = None) -> int:
        """Deliver to every matching subscriber; returns how many got it.

        ``message["feed"]`` must be one the scene shows; ``refresh``/``reload`` go to everyone.
        Scenes that are off air only get housekeeping messages.
        """
        feed = message.get("feed")
        housekeeping = message.get("kind") in ("refresh", "reload")
        n = 0
        for sub in list(self._subs):
            if scene_id is not None and sub.scene_id != scene_id:
                continue
            if not housekeeping and scene_id is None and (not sub.active or feed not in sub.feeds):
                continue
            sub.offer(message)
            n += 1
        return n

    def configure(self, scene_id: int, feeds: set[str] | list[str], active: bool) -> None:
        """A scene's settings changed: update open streams and tell the page to reload."""
        for sub in list(self._subs):
            if sub.scene_id == scene_id:
                sub.feeds = set(feeds)
                sub.active = active
                sub.offer({"kind": "reload"})

    def close(self, scene_id: int) -> None:
        """End every open stream for a scene (deleted, or its URL was regenerated)."""
        for sub in list(self._subs):
            if sub.scene_id == scene_id:
                sub.offer(CLOSE)
                self._subs.discard(sub)


hub = Hub()
