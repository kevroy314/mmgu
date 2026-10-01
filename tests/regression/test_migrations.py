"""Migrations build exactly the schema the models describe, on a fresh database.

Fails when someone changes a model without running `mmgu makemigrations <module>`.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from mmgu.cli import alembic_config
from mmgu.core.hall import hall
from mmgu.db import Base


def _fresh_db():
    d = Path(tempfile.mkdtemp(prefix="mmgu-mig-"))
    return f"sqlite+aiosqlite:///{d / 'm.db'}", f"sqlite:///{d / 'm.db'}"


def _upgrade(async_url: str) -> None:
    old = hall.settings.database_url
    hall.settings.database_url = async_url
    try:
        command.upgrade(alembic_config(), "heads")
    finally:
        hall.settings.database_url = old


def _ignore(obj, name, type_, reflected, compare_to):
    return not (type_ == "table" and (name.endswith("_fts") or "_fts_" in name or name.startswith("sqlite_")))


async def test_migrations_match_models():
    async_url, sync_url = _fresh_db()
    await asyncio.to_thread(_upgrade, async_url)
    engine = create_engine(sync_url)
    with engine.connect() as conn:
        ctx = MigrationContext.configure(
            conn, opts={"include_object": _ignore, "compare_type": True, "render_as_batch": True}
        )
        diff = compare_metadata(ctx, Base.metadata)
    engine.dispose()
    assert not diff, "models and migrations disagree; run `mmgu makemigrations <module>`:\n" + "\n".join(map(str, diff))


async def test_migrations_are_idempotent():
    async_url, _ = _fresh_db()
    await asyncio.to_thread(_upgrade, async_url)
    await asyncio.to_thread(_upgrade, async_url)  # second run is a no-op, not an error


def test_every_module_with_tables_has_its_own_branch():
    script = ScriptDirectory.from_config(alembic_config())
    labels = {label for rev in script.walk_revisions() for label in (rev.branch_labels or ())}
    owners = {t.info.get("module") for t in Base.metadata.tables.values()}
    missing = sorted(o for o in owners if o and o not in labels)
    assert not missing, f"modules with tables but no migration branch: {missing}"


def test_migrations_referencing_archive_depend_on_it():
    """Postgres needs referenced tables to exist first; SQLite wouldn't notice."""
    script = ScriptDirectory.from_config(alembic_config())
    archive = [r.revision for r in script.walk_revisions() if "archive" in (r.branch_labels or ())]
    for rev in script.walk_revisions():
        src = Path(rev.path).read_text()
        if "archive_items.id" in src and "archive" not in (rev.branch_labels or ()):
            deps = rev.dependencies or ()
            deps = (deps,) if isinstance(deps, str) else deps
            down = rev.down_revision
            assert set(archive) & set(deps) or down in archive or rev.revision in archive, (
                f"{Path(rev.path).name} references archive_items but doesn't depend on the archive branch"
            )
