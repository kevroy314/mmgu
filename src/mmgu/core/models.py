"""Tables every module can rely on: people, their characters, roles, settings and the audit trail."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from mmgu.db import Base, Timestamped, utcnow


class Member(Base, Timestamped):
    """A person in the guild. One member can have many characters and many login identities."""

    __tablename__ = "core_members"

    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str] = mapped_column(String(80))
    avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | left | banned
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    onboarded: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)


class Identity(Base):
    """A way to log in or be recognised: an email from the trusted proxy, a Discord user id, etc."""

    __tablename__ = "core_identities"
    __table_args__ = (UniqueConstraint("provider", "subject"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("core_members.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(20))  # email | discord | dev
    subject: Mapped[str] = mapped_column(String(200))
    label: Mapped[str | None] = mapped_column(String(200), nullable=True)  # e.g. Discord username
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RoleGrant(Base):
    __tablename__ = "core_role_grants"
    __table_args__ = (UniqueConstraint("member_id", "role"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("core_members.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(20), default="manual")  # manual | discord | bootstrap
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Character(Base, Timestamped):
    """An in-game character. Bank mules are characters with is_bank_mule set."""

    __tablename__ = "core_characters"
    __table_args__ = (UniqueConstraint("name", "server"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[int | None] = mapped_column(
        ForeignKey("core_members.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(64), index=True)
    class_name: Mapped[str | None] = mapped_column(String(40), nullable=True)
    race: Mapped[str | None] = mapped_column(String(40), nullable=True)
    level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    server: Mapped[str] = mapped_column(String(40), default="")
    is_main: Mapped[bool] = mapped_column(Boolean, default=False)
    is_bank_mule: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | retired
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)


class Setting(Base):
    """Runtime settings a leader can change in the web app. Values are JSON."""

    __tablename__ = "core_settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("core_members.id"), nullable=True)


class AuditLog(Base):
    """Append-only record of every change. Before/after snapshots make changes traceable and reversible."""

    __tablename__ = "core_audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("core_members.id"), nullable=True)
    via: Mapped[str] = mapped_column(String(20), default="web")  # web | discord | api | system | ai
    action: Mapped[str] = mapped_column(String(80), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    before: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class ApiToken(Base):
    """Personal tokens for companion tools, the MCP server and scripts. Only a hash is stored."""

    __tablename__ = "core_api_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("core_members.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    prefix: Mapped[str] = mapped_column(String(12))
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class Proposal(Base, Timestamped):
    """A change suggested by a machine (vision model, Claude, MCP, companion tool) that a person must approve."""

    __tablename__ = "core_proposals"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(60), index=True)  # e.g. archive.item, vault.holdings
    source: Mapped[str] = mapped_column(String(60))  # e.g. ollama:qwen3-vl:8b, claude-haiku-4-5, mcp
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    summary: Mapped[str] = mapped_column(String(300), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    upload_id: Mapped[int | None] = mapped_column(ForeignKey("core_uploads.id"), nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("core_members.id"), nullable=True)
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("core_members.id"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    result_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Upload(Base):
    """An image someone uploaded (screenshots). Stored on disk, EXIF stripped, deduplicated by hash."""

    __tablename__ = "core_uploads"

    id: Mapped[int] = mapped_column(primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    path: Mapped[str] = mapped_column(String(300))
    mime: Mapped[str] = mapped_column(String(60))
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    size: Mapped[int] = mapped_column(Integer, default=0)
    purpose: Mapped[str] = mapped_column(String(40), default="screenshot")
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("core_members.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LinkCode(Base):
    """Short-lived code for linking a Discord account to a web login (/link CODE)."""

    __tablename__ = "core_link_codes"

    code: Mapped[str] = mapped_column(String(12), primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("core_members.id", ondelete="CASCADE"))
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class Notice(Base):
    """In-app notification for one member ("Your vault request was approved")."""

    __tablename__ = "core_notices"

    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("core_members.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(String(400))
    url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
