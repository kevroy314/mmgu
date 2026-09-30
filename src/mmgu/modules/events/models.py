from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from mmgu.db import Base, Timestamped, utcnow


class Event(Base, Timestamped):
    """A raid, group night, crafting party or social. Times are naive UTC."""

    __tablename__ = "events_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(20), default="Raid", index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    zone: Mapped[str | None] = mapped_column(String(120), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    leader_id: Mapped[int | None] = mapped_column(ForeignKey("core_members.id", ondelete="SET NULL"), nullable=True)
    capacity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    roles: Mapped[dict] = mapped_column(JSON, default=dict)  # {"tank": 2, "healer": 3, ...}
    status: Mapped[str] = mapped_column(String(20), default="scheduled", index=True)  # scheduled|active|done|cancelled
    series_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)  # shared by weekly repeats
    created_by: Mapped[int | None] = mapped_column(ForeignKey("core_members.id"), nullable=True)
    discord_channel_id: Mapped[str | None] = mapped_column(String(30), nullable=True)
    discord_message_id: Mapped[str | None] = mapped_column(String(30), nullable=True)
    reminded: Mapped[bool] = mapped_column(Boolean, default=False)
    start_announced: Mapped[bool] = mapped_column(Boolean, default=False)


class Signup(Base, Timestamped):
    """One member's answer for one event: which character, going or not, in what role."""

    __tablename__ = "events_signups"
    __table_args__ = (UniqueConstraint("event_id", "member_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events_events.id", ondelete="CASCADE"), index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("core_members.id", ondelete="CASCADE"), index=True)
    character_id: Mapped[int | None] = mapped_column(
        ForeignKey("core_characters.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(10), default="going")  # going | maybe | late | cant
    role: Mapped[str] = mapped_column(String(20), default="any")
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)


class Attendance(Base):
    """Someone was there. ``member_id`` is empty for an unclaimed character matched from /who."""

    __tablename__ = "events_attendance"
    __table_args__ = (UniqueConstraint("event_id", "member_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events_events.id", ondelete="CASCADE"), index=True)
    member_id: Mapped[int | None] = mapped_column(
        ForeignKey("core_members.id", ondelete="CASCADE"), nullable=True, index=True
    )
    character_id: Mapped[int | None] = mapped_column(
        ForeignKey("core_characters.id", ondelete="SET NULL"), nullable=True
    )
    source: Mapped[str] = mapped_column(String(20), default="manual")  # manual | checkin | who
    recorded_by: Mapped[int | None] = mapped_column(ForeignKey("core_members.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LootAward(Base):
    """An item given to a character. ``points`` is stored for a future points/DKP module; nothing spends it here."""

    __tablename__ = "events_loot"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int | None] = mapped_column(
        ForeignKey("events_events.id", ondelete="SET NULL"), nullable=True, index=True
    )
    item_id: Mapped[int | None] = mapped_column(
        ForeignKey("archive_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    item_name: Mapped[str] = mapped_column(String(120))
    character_id: Mapped[int | None] = mapped_column(
        ForeignKey("core_characters.id", ondelete="SET NULL"), nullable=True, index=True
    )
    character_name: Mapped[str] = mapped_column(String(64))
    member_id: Mapped[int | None] = mapped_column(
        ForeignKey("core_members.id", ondelete="SET NULL"), nullable=True, index=True
    )
    method: Mapped[str] = mapped_column(String(20), default="Roll")
    points: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str | None] = mapped_column(String(300), nullable=True)
    awarded_by: Mapped[int | None] = mapped_column(ForeignKey("core_members.id"), nullable=True)
    awarded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class CalendarToken(Base):
    """The secret in a member's calendar-feed URL. Regenerating it breaks old subscriptions."""

    __tablename__ = "events_calendar_tokens"

    member_id: Mapped[int] = mapped_column(ForeignKey("core_members.id", ondelete="CASCADE"), primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
