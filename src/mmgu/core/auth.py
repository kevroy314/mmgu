"""Who is making this request, and what may they do?

Supported ways in (enable with MMGU_AUTH_MODES):

- ``discord``  Discord OAuth2. Discord roles can map to hall ranks and duties.
- ``header``   A trusted reverse proxy (oauth2-proxy, Cloudflare Access, IAP) has already
               logged the person in and passes their email. The proxy must also send the shared
               secret in ``X-MMGU-Proxy-Secret`` so nothing else on the network can pretend.
- ``dev``      Pick any name and rank. Local testing only.
- API tokens   ``Authorization: Bearer mmgu_...`` for companion tools, MCP and scripts.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass, field

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmgu.core import store
from mmgu.core.hall import hall
from mmgu.core.models import ApiToken, Identity, Member, RoleGrant
from mmgu.core.permissions import RANK_ORDER, best_rank
from mmgu.db import get_session, utcnow

log = logging.getLogger(__name__)

TOKEN_PREFIX = "mmgu_"


@dataclass
class Viewer:
    member: Member | None = None
    rank: str = "guest"
    duties: set[str] = field(default_factory=set)
    perms: set[str] = field(default_factory=set)
    via: str = "anonymous"  # session | header | token | discord | anonymous

    @property
    def id(self) -> int | None:
        return self.member.id if self.member else None

    @property
    def is_authenticated(self) -> bool:
        return self.member is not None

    @property
    def name(self) -> str:
        return self.member.display_name if self.member else "Visitor"

    def can(self, perm: str) -> bool:
        return perm in self.perms

    def at_least(self, rank: str) -> bool:
        return RANK_ORDER.get(self.rank, 0) >= RANK_ORDER.get(rank, 99)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


async def roles_for(session: AsyncSession, member_id: int) -> set[str]:
    rows = await session.execute(select(RoleGrant.role).where(RoleGrant.member_id == member_id))
    return set(rows.scalars().all())


def default_rank() -> str:
    return store.get("hall.default_role") or hall.settings.default_role


async def build_viewer(session: AsyncSession, member: Member | None, via: str) -> Viewer:
    overrides = store.get("permissions.overrides", {}) or {}
    if member is None or member.status == "banned":
        perms = hall.perms.grants("guest", set(), overrides)
        return Viewer(None, "guest", set(), perms, "anonymous")
    roles = await roles_for(session, member.id)
    rank = best_rank(roles, default_rank())
    duties = {r for r in roles if r not in RANK_ORDER}
    if member.status == "left":
        rank = "guest"
    perms = hall.perms.grants(rank, duties, overrides)
    return Viewer(member, rank, duties, perms, via)


def _random_display_name() -> str:
    return f"Adventurer {secrets.randbelow(9000) + 1000}"


async def find_or_create_member(
    session: AsyncSession,
    provider: str,
    subject: str,
    *,
    label: str | None = None,
    display_name: str | None = None,
    avatar_url: str | None = None,
) -> tuple[Member, bool]:
    """Find the member behind an identity, creating one on first sight.

    New members get a neutral placeholder name (never derived from an email address) and pick
    their own name on the welcome page.
    """
    ident = (
        await session.execute(select(Identity).where(Identity.provider == provider, Identity.subject == subject))
    ).scalar_one_or_none()
    if ident is not None:
        member = await session.get(Member, ident.member_id)
        if label and ident.label != label:
            ident.label = label
        return member, False
    member = Member(display_name=display_name or _random_display_name(), avatar_url=avatar_url)
    session.add(member)
    await session.flush()
    session.add(Identity(member_id=member.id, provider=provider, subject=subject, label=label))
    if f"{provider}:{subject}".lower() in [b.lower() for b in hall.settings.bootstrap_leader_list]:
        session.add(RoleGrant(member_id=member.id, role="leader", source="bootstrap"))
    await session.flush()
    from mmgu.core.audit import record

    await record(
        session,
        "member.joined",
        actor_id=member.id,
        entity=member,
        via="system",
        summary=f"New member arrived via {provider}",
    )
    return member, True


async def sync_roles(session: AsyncSession, member_id: int, roles: set[str], source: str) -> None:
    """Make the member's roles from ``source`` exactly ``roles``, leaving other sources alone."""
    rows = (await session.execute(select(RoleGrant).where(RoleGrant.member_id == member_id))).scalars().all()
    existing = {r.role: r for r in rows}
    for role, grant in existing.items():
        if grant.source == source and role not in roles:
            await session.delete(grant)
    for role in roles:
        if role not in existing:
            session.add(RoleGrant(member_id=member_id, role=role, source=source))
    await session.flush()


async def _viewer_from_token(session: AsyncSession, raw: str) -> Viewer | None:
    tok = (
        await session.execute(
            select(ApiToken).where(ApiToken.token_hash == hash_token(raw), ApiToken.revoked.is_(False))
        )
    ).scalar_one_or_none()
    if tok is None:
        return None
    tok.last_used_at = utcnow()
    member = await session.get(Member, tok.member_id)
    viewer = await build_viewer(session, member, "token")
    if tok.scopes:
        viewer.perms &= set(tok.scopes)
    return viewer


async def resolve_viewer(request: Request, session: AsyncSession) -> Viewer:
    cached = getattr(request.state, "viewer", None)
    if cached is not None:
        return cached
    viewer: Viewer | None = None
    modes = hall.settings.auth_mode_list

    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer ") and auth[7:].startswith(TOKEN_PREFIX):
        viewer = await _viewer_from_token(session, auth[7:].strip())
        if viewer is None:
            raise HTTPException(401, "That API token is not valid or was revoked.")

    if viewer is None and "header" in modes and hall.settings.proxy_secret:
        sent = request.headers.get("x-mmgu-proxy-secret", "")
        email = request.headers.get(hall.settings.proxy_email_header, "").strip().lower()
        if email and hmac.compare_digest(sent, hall.settings.proxy_secret):
            member, _ = await find_or_create_member(session, "email", email)
            viewer = await build_viewer(session, member, "header")

    if viewer is None:
        member_id = request.session.get("member_id") if "session" in request.scope else None
        if member_id:
            member = await session.get(Member, member_id)
            if member is not None:
                viewer = await build_viewer(session, member, "session")

    if viewer is None:
        viewer = await build_viewer(session, None, "anonymous")

    if viewer.member is not None:
        now = utcnow()
        if viewer.member.last_seen_at is None or (now - viewer.member.last_seen_at).total_seconds() > 300:
            viewer.member.last_seen_at = now
    request.state.viewer = viewer
    return viewer


async def current_viewer(request: Request, session: AsyncSession = Depends(get_session)) -> Viewer:
    return await resolve_viewer(request, session)


class LoginRequired(Exception):
    pass


def require(perm: str):
    """Route dependency: 401 → login page for visitors, 403 for members without the permission."""

    async def dep(request: Request, session: AsyncSession = Depends(get_session)) -> Viewer:
        viewer = await resolve_viewer(request, session)
        if viewer.can(perm):
            return viewer
        if not viewer.is_authenticated:
            raise LoginRequired()
        raise HTTPException(403, f"Your rank doesn't allow this ({hall.perms.perms.get(perm, perm)!s}).")

    return dep


def require_module(module_id: str):
    async def dep() -> None:
        if not hall.is_enabled(module_id):
            raise HTTPException(404, "That part of the hall is closed. A leader can open it in the Steward's Office.")

    return dep


# ----- CSRF --------------------------------------------------------------------------------
def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(24)
        request.session["csrf"] = tok
    return tok


async def csrf_protect(request: Request) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if request.headers.get("authorization", "").lower().startswith("bearer "):
        return
    if request.url.path.startswith(("/auth/", "/overlay/")):
        return
    expected = request.session.get("csrf")
    sent = request.headers.get("x-csrf-token")
    if not sent:
        ctype = request.headers.get("content-type", "")
        if "form" in ctype:
            form = await request.form()
            sent = form.get("csrf")
    if not expected or not sent or not hmac.compare_digest(str(sent), expected):
        raise HTTPException(403, "This form expired. Reload the page and try again.")
