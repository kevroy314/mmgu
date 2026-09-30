"""Extension points: named slots where modules contribute pieces to each other's pages.

Known slots (add new ones freely, a slot is just a string):

- ``item.panels``      async fn(request, session, item) -> str | None   extra HTML on an item page
- ``member.panels``    async fn(request, session, member) -> str | None  extra HTML on a member page
- ``hall.cards``       async fn(request, session) -> str | None          dashboard cards
- ``search.providers`` async fn(session, query, viewer) -> list[SearchHit]
- ``item.badges``      async fn(session, item_ids) -> dict[item_id, list[str]]
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class Contribution:
    fn: Callable[..., Any]
    order: int
    module: str


@dataclass
class SearchHit:
    kind: str  # e.g. "Item", "Member", "Listing"
    title: str
    url: str
    subtitle: str = ""
    score: float = 0.0


class Extensions:
    def __init__(self) -> None:
        self._slots: dict[str, list[Contribution]] = defaultdict(list)

    def add(self, slot: str, fn: Callable[..., Any], *, module: str, order: int = 100) -> None:
        self._slots[slot].append(Contribution(fn, order, module))
        self._slots[slot].sort(key=lambda c: c.order)

    def get(self, slot: str, enabled: set[str] | None = None) -> list[Contribution]:
        items = self._slots.get(slot, [])
        if enabled is None:
            return list(items)
        return [c for c in items if c.module in enabled]
