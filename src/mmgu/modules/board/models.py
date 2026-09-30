from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from mmgu.db import Base, Timestamped


class Listing(Base, Timestamped):
    """A want-to-sell or want-to-buy post."""

    __tablename__ = "board_listings"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(3), index=True)  # WTS | WTB
    author_id: Mapped[int | None] = mapped_column(ForeignKey("core_members.id"), nullable=True, index=True)
    item_id: Mapped[int | None] = mapped_column(
        ForeignKey("archive_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(120))  # the item name as written (or the Archive's spelling)
    name_key: Mapped[str] = mapped_column(String(120), index=True)
    qty: Mapped[int] = mapped_column(Integer, default=1)
    price: Mapped[str | None] = mapped_column(String(60), nullable=True)  # free text: "5pp", "offer"
    server: Mapped[str] = mapped_column(String(40), default="")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)  # open | sold | closed | expired
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    discord_channel_id: Mapped[str | None] = mapped_column(String(30), nullable=True)
    discord_message_id: Mapped[str | None] = mapped_column(String(30), nullable=True)


class BoardRequest(Base, Timestamped):
    """Someone asking for an item or a service (a craft, a port, buffs, a corpse recovery)."""

    __tablename__ = "board_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int | None] = mapped_column(ForeignKey("core_members.id"), nullable=True, index=True)
    category: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(String(140))
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("archive_items.id", ondelete="SET NULL"), nullable=True)
    item_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    tradeskill: Mapped[str | None] = mapped_column(String(60), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)  # open | claimed | done | closed
    claimed_by: Mapped[int | None] = mapped_column(ForeignKey("core_members.id"), nullable=True, index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    done_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    thanked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    discord_channel_id: Mapped[str | None] = mapped_column(String(30), nullable=True)
    discord_message_id: Mapped[str | None] = mapped_column(String(30), nullable=True)
