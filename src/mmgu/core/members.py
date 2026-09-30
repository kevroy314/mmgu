"""Member utilities shared across modules."""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mmgu.core.models import Member
from mmgu.db import Base


def _member_fk_columns():
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            for fk in col.foreign_keys:
                if fk.column.table.name == "core_members" and fk.column.name == "id":
                    yield table, col


async def merge_members(session: AsyncSession, old_id: int, new_id: int) -> None:
    """Move everything that points at member ``old_id`` over to ``new_id``, then remove ``old_id``.

    Works for every module's tables automatically by following foreign keys. Rows that would break
    a uniqueness rule (e.g. both members signed up for the same event) keep the new member's row.
    """
    if old_id == new_id:
        return
    for table, col in _member_fk_columns():
        pk = list(table.primary_key.columns)
        rows = (await session.execute(select(*pk).where(col == old_id))).all()
        for row in rows:
            cond = [c == v for c, v in zip(pk, row, strict=True)]
            try:
                async with session.begin_nested():
                    await session.execute(update(table).where(*cond).values({col.name: new_id}))
            except IntegrityError:
                async with session.begin_nested():
                    await session.execute(table.delete().where(*cond))
    old = await session.get(Member, old_id)
    if old is not None:
        await session.delete(old)
    await session.flush()
