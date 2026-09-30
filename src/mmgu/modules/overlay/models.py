from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from mmgu.db import Base, Timestamped, utcnow


class Scene(Base, Timestamped):
    """One overlay URL for OBS. The token in the URL is the only key: whoever has it sees this scene."""

    __tablename__ = "overlay_scenes"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)  # "on air": receives new cards
    position: Mapped[str] = mapped_column(String(20), default="bottom-right")
    theme: Mapped[str] = mapped_column(String(20), default="window")  # window | clear
    scale: Mapped[int] = mapped_column(Integer, default=100)  # percent
    card_seconds: Mapped[int] = mapped_column(Integer, default=12)
    timers_count: Mapped[int] = mapped_column(Integer, default=3)
    feeds: Mapped[list] = mapped_column(JSON, default=list)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("core_members.id"), nullable=True)


class Message(Base):
    """Something shown on stream. Kept for a day so a reloaded overlay can catch up."""

    __tablename__ = "overlay_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    feed: Mapped[str] = mapped_column(String(20), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    # None: every scene on air. Set: only that scene (test cards).
    scene_id: Mapped[int | None] = mapped_column(
        ForeignKey("overlay_scenes.id", ondelete="CASCADE"), nullable=True, index=True
    )
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("core_members.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
