"""Alembic environment. Each module has its own migration branch in <module>/migrations."""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from mmgu.core.hall import hall
from mmgu.db import Base

if not hall.booted:
    hall.boot()

config = context.config
target_metadata = Base.metadata
only_module = config.attributes.get("only_module")


def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table":
        if name.startswith("sqlite_") or name.endswith("_fts") or "_fts_" in name:
            return False
        if only_module:
            table = obj if not reflected else Base.metadata.tables.get(name)
            owner = table.info.get("module") if table is not None else None
            return owner == only_module
    return True


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        render_as_batch=True,  # SQLite needs batch mode to alter tables
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _online() -> None:
    engine = create_async_engine(hall.settings.db_url)
    async with engine.connect() as conn:
        await conn.run_sync(_run)
        await conn.commit()
    await engine.dispose()


if context.is_offline_mode():
    context.configure(
        url=hall.settings.db_url, target_metadata=target_metadata, literal_binds=True, render_as_batch=True
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_online())
