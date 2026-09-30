from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from mmgu.db import Base, Timestamped, utcnow


class Item(Base, Timestamped):
    """One item as it appears on the in-game inspect window.

    Well-known fields get columns so they can be filtered and sorted. Everything the game pack
    adds later (new stats, resists) lands in ``stats``/``resists`` JSON; anything else in ``attributes``.
    """

    __tablename__ = "archive_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    name_key: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    item_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    slots: Mapped[list] = mapped_column(JSON, default=list)
    flags: Mapped[list] = mapped_column(JSON, default=list)
    classes: Mapped[list] = mapped_column(JSON, default=list)  # ["ALL"] or class names
    races: Mapped[list] = mapped_column(JSON, default=list)
    skill: Mapped[str | None] = mapped_column(String(20), nullable=True)
    damage: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delay: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ac: Mapped[int | None] = mapped_column(Integer, nullable=True)
    weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    size: Mapped[str | None] = mapped_column(String(20), nullable=True)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    resists: Mapped[dict] = mapped_column(JSON, default=dict)
    effects: Mapped[list] = mapped_column(JSON, default=list)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | stale
    patch_seen: Mapped[str | None] = mapped_column(String(40), nullable=True)
    first_cataloged_by: Mapped[int | None] = mapped_column(ForeignKey("core_members.id"), nullable=True)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("core_members.id"), nullable=True)


class ItemRevision(Base):
    __tablename__ = "archive_item_revisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("archive_items.id", ondelete="CASCADE"), index=True)
    editor_id: Mapped[int | None] = mapped_column(ForeignKey("core_members.id"), nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    note: Mapped[str] = mapped_column(String(200), default="")
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class ItemImage(Base):
    __tablename__ = "archive_item_images"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("archive_items.id", ondelete="CASCADE"), index=True)
    upload_id: Mapped[int] = mapped_column(ForeignKey("core_uploads.id"))
    added_by: Mapped[int | None] = mapped_column(ForeignKey("core_members.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DropReport(Base):
    """ "I saw this drop from that creature in that zone." Many reports build a loot table."""

    __tablename__ = "archive_drop_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("archive_items.id", ondelete="CASCADE"), index=True)
    creature: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    zone: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    note: Mapped[str | None] = mapped_column(String(300), nullable=True)
    source: Mapped[str] = mapped_column(String(30), default="manual")  # manual | screenshot | ledger | import
    reported_by: Mapped[int | None] = mapped_column(ForeignKey("core_members.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
