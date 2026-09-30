"""Database plumbing: one async engine, one declarative Base shared by every module.

Each table records which module owns it (``table.info["module"]``), derived from the Python
package the model lives in. Migrations use that to give every module its own Alembic branch.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, MetaData, event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from mmgu.core.bus import begin_deferral, end_deferral

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def module_id_for(py_module: str) -> str:
    """mmgu.core.models -> core; mmgu.modules.archive.models -> archive; thirdparty.x -> thirdparty."""
    parts = py_module.split(".")
    if parts[0] == "mmgu" and len(parts) > 2 and parts[1] in ("modules", "addons"):
        return parts[2]
    if parts[0] == "mmgu":
        return "core"
    return parts[0]


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map = {dict: JSON, list: JSON}

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        table = getattr(cls, "__table__", None)
        if table is not None:
            table.info.setdefault("module", getattr(cls, "__mmgu_module__", None) or module_id_for(cls.__module__))


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def init_engine(url: str, echo: bool = False) -> AsyncEngine:
    global _engine, _sessionmaker
    kwargs: dict = {"echo": echo}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"timeout": 30}
    _engine = create_async_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(_engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def engine() -> AsyncEngine:
    assert _engine is not None, "init_engine() has not been called"
    return _engine


def sessionmaker() -> async_sessionmaker[AsyncSession]:
    assert _sessionmaker is not None, "init_engine() has not been called"
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """A session that commits on success and rolls back on error. For jobs, the bot and scripts.

    Bus events emitted inside it are delivered after the commit, and dropped on rollback.
    """
    token = begin_deferral()
    ok = False
    try:
        async with sessionmaker()() as session:
            try:
                yield session
                await session.commit()
                ok = True
            except BaseException:
                await session.rollback()
                raise
    finally:
        end_deferral(token, dispatch=ok)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request, committed when the handler returns."""
    async with session_scope() as session:
        yield session
