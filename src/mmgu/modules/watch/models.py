from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from mmgu.db import Base, Timestamped, utcnow


class Timer(Base, Timestamped):
    """A tracked creature and how long it takes to come back."""

    __tablename__ = "watch_timers"

    id: Mapped[int] = mapped_column(primary_key=True)
    creature: Mapped[str] = mapped_column(String(120))
    creature_key: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    zone: Mapped[str | None] = mapped_column(String(120), nullable=True)
    respawn_minutes: Mapped[int] = mapped_column(Integer)
    variance_minutes: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("core_members.id"), nullable=True)


class DeathReport(Base):
    """'It died at this time.' The latest report per timer sets the next window."""

    __tablename__ = "watch_deaths"

    id: Mapped[int] = mapped_column(primary_key=True)
    timer_id: Mapped[int] = mapped_column(ForeignKey("watch_timers.id", ondelete="CASCADE"), index=True)
    died_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    reported_by: Mapped[int | None] = mapped_column(ForeignKey("core_members.id"), nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="manual")  # manual | discord | ledger | api
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    soon_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    open_sent: Mapped[bool] = mapped_column(Boolean, default=False)


class Watcher(Base):
    __tablename__ = "watch_watchers"
    __table_args__ = (UniqueConstraint("timer_id", "member_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    timer_id: Mapped[int] = mapped_column(ForeignKey("watch_timers.id", ondelete="CASCADE"), index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("core_members.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Camp(Base):
    """Someone holding a camp: zone + camp name + character, until they're done or it expires."""

    __tablename__ = "watch_camps"

    id: Mapped[int] = mapped_column(primary_key=True)
    zone: Mapped[str] = mapped_column(String(120))
    camp: Mapped[str] = mapped_column(String(120))
    member_id: Mapped[int | None] = mapped_column(ForeignKey("core_members.id", ondelete="SET NULL"), nullable=True)
    character_id: Mapped[int | None] = mapped_column(
        ForeignKey("core_characters.id", ondelete="SET NULL"), nullable=True
    )
    character_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    end_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)  # done | expired | replaced
