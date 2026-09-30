"""Write to the audit log. Call ``record`` for every change a person or machine makes."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession

from mmgu.core.models import AuditLog


def snapshot(obj: Any) -> dict[str, Any]:
    """A JSON-safe dict of an ORM object's column values."""
    if obj is None:
        return {}
    out: dict[str, Any] = {}
    for attr in sa_inspect(obj).mapper.column_attrs:
        v = getattr(obj, attr.key)
        if isinstance(v, (datetime, date)):
            v = v.isoformat()
        out[attr.key] = v
    return out


async def record(
    session: AsyncSession,
    action: str,
    *,
    actor_id: int | None,
    summary: str = "",
    entity: Any = None,
    entity_type: str | None = None,
    entity_id: Any = None,
    before: dict | None = None,
    after: dict | None = None,
    via: str = "web",
) -> AuditLog:
    if entity is not None:
        entity_type = entity_type or type(entity).__name__
        entity_id = entity_id if entity_id is not None else getattr(entity, "id", None)
        if after is None:
            after = snapshot(entity)
    row = AuditLog(
        actor_id=actor_id,
        via=via,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        summary=summary[:2000],
        before=before,
        after=after,
    )
    session.add(row)
    return row
