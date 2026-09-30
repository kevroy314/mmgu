"""In-process event bus. Modules announce what happened; other modules react.

Example: the Archive emits ``archive.item_created``; the Discord bridge posts it to a channel,
the overlay shows it on stream, and the Tidings digest counts it. None of them import each other.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

Handler = Callable[["HallEvent"], Awaitable[None]]

# While a database session is open (see mmgu.db), emitted events wait here until it commits, so
# handlers that open their own session always see the new rows. A rollback discards them.
_pending: contextvars.ContextVar[list | None] = contextvars.ContextVar("mmgu_pending_events", default=None)


def begin_deferral() -> contextvars.Token:
    return _pending.set([])


def end_deferral(token: contextvars.Token, *, dispatch: bool) -> None:
    pending = _pending.get() or []
    try:
        _pending.reset(token)
    except ValueError:  # closed from a different context (e.g. a cancelled task); just stop deferring
        _pending.set(None)
    if dispatch:
        for bus, event in pending:
            bus._dispatch(event)


@dataclass
class HallEvent:
    name: str
    data: dict[str, Any] = field(default_factory=dict)
    actor_id: int | None = None


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._background: set[asyncio.Task] = set()

    def subscribe(self, name: str, handler: Handler) -> None:
        """Subscribe to one event name, a prefix such as ``archive.*``, or everything with ``*``."""
        self._handlers[name].append(handler)

    def on(self, name: str) -> Callable[[Handler], Handler]:
        def deco(fn: Handler) -> Handler:
            self.subscribe(name, fn)
            return fn

        return deco

    def _matching(self, name: str) -> list[Handler]:
        out = list(self._handlers.get(name, ()))
        out += self._handlers.get("*", [])
        prefix = name.split(".", 1)[0] + ".*"
        out += self._handlers.get(prefix, [])
        return out

    async def emit(self, name: str, actor_id: int | None = None, **data: Any) -> None:
        """Run handlers in the background, after the current database transaction commits.

        A slow Discord post never slows down a web request, and handlers never see uncommitted data.
        """
        event = HallEvent(name, data, actor_id)
        pending = _pending.get()
        if pending is not None:
            pending.append((self, event))
        else:
            self._dispatch(event)

    def _dispatch(self, event: HallEvent) -> None:
        for handler in self._matching(event.name):
            # Handlers start with no deferral list of their own; their sessions set one up.
            ctx = contextvars.copy_context()
            ctx.run(_pending.set, None)
            task = asyncio.get_running_loop().create_task(self._run(handler, event), context=ctx)
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    async def emit_and_wait(self, name: str, actor_id: int | None = None, **data: Any) -> None:
        event = HallEvent(name, data, actor_id)
        await asyncio.gather(*(self._run(h, event) for h in self._matching(name)))

    async def drain(self) -> None:
        if self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)

    @staticmethod
    async def _run(handler: Handler, event: HallEvent) -> None:
        try:
            await handler(event)
        except Exception:  # noqa: BLE001 - one bad subscriber must not break the others
            log.exception("event handler %s failed for %s", getattr(handler, "__name__", handler), event.name)
