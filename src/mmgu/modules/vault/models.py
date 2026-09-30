from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from mmgu.db import Base, Timestamped, utcnow


class Holding(Base):
    """How many of one item a bank mule holds. Linked to the Archive when the name matches."""

    __tablename__ = "vault_holdings"
    __table_args__ = (UniqueConstraint("character_id", "name_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    character_id: Mapped[int] = mapped_column(ForeignKey("core_characters.id", ondelete="CASCADE"), index=True)
    item_id: Mapped[int | None] = mapped_column(
        ForeignKey("archive_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    name_key: Mapped[str] = mapped_column(String(120), index=True)
    qty: Mapped[int] = mapped_column(Integer, default=0)
    slot: Mapped[str | None] = mapped_column(String(60), nullable=True)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("core_members.id"), nullable=True)


class Transaction(Base):
    """One change to a mule's holdings. ``delta`` is signed; ``qty_after`` is what was left."""

    __tablename__ = "vault_transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    character_id: Mapped[int] = mapped_column(ForeignKey("core_characters.id", ondelete="CASCADE"), index=True)
    item_id: Mapped[int | None] = mapped_column(
        ForeignKey("archive_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(20))  # deposit | withdraw | adjust | import
    delta: Mapped[int] = mapped_column(Integer)
    qty_after: Mapped[int] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(String(300), nullable=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("core_members.id"), nullable=True)
    request_id: Mapped[int | None] = mapped_column(ForeignKey("vault_requests.id"), nullable=True)
    via: Mapped[str] = mapped_column(String(20), default="web")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Request(Base, Timestamped):
    """A member asking the bankers for an item."""

    __tablename__ = "vault_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    requester_id: Mapped[int | None] = mapped_column(ForeignKey("core_members.id"), nullable=True, index=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("archive_items.id", ondelete="SET NULL"), nullable=True)
    name: Mapped[str] = mapped_column(String(120))
    qty: Mapped[int] = mapped_column(Integer, default=1)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # pending | approved | denied | fulfilled | cancelled
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("core_members.id"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decision_note: Mapped[str | None] = mapped_column(String(300), nullable=True)
    fulfilled_from: Mapped[int | None] = mapped_column(
        ForeignKey("core_characters.id", ondelete="SET NULL"), nullable=True
    )
    discord_channel_id: Mapped[str | None] = mapped_column(String(30), nullable=True)
    discord_message_id: Mapped[str | None] = mapped_column(String(30), nullable=True)
