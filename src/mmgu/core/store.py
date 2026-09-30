"""Runtime settings stored in the database, cached in memory (single process)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmgu.core.models import Setting

_cache: dict[str, Any] = {}
_loaded = False


async def load_all(session: AsyncSession) -> None:
    global _loaded
    rows = (await session.execute(select(Setting))).scalars().all()
    _cache.clear()
    _cache.update({r.key: r.value for r in rows})
    _loaded = True


def get(key: str, default: Any = None) -> Any:
    return _cache.get(key, default)


def all_values() -> dict[str, Any]:
    return dict(_cache)


async def put(session: AsyncSession, key: str, value: Any, actor_id: int | None = None) -> None:
    row = await session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value, updated_by=actor_id))
    else:
        row.value = value
        row.updated_by = actor_id
    _cache[key] = value


def reset_cache() -> None:
    global _loaded
    _cache.clear()
    _loaded = False
