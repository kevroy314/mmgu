from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from mmgu.db import Base, utcnow


class SeenEvent(Base):
    """A ledger event the hall has already imported, so re-sending it does nothing."""

    __tablename__ = "ledger_sync_seen"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    member_id: Mapped[int | None] = mapped_column(ForeignKey("core_members.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ImportBatch(Base):
    """One upload from someone's companion script, with what came of it."""

    __tablename__ = "ledger_sync_imports"

    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[int | None] = mapped_column(ForeignKey("core_members.id"), nullable=True, index=True)
    character: Mapped[str] = mapped_column(String(80), default="")
    server: Mapped[str] = mapped_column(String(80), default="")
    received: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    counts: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
