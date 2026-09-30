from __future__ import annotations

import secrets
from datetime import timedelta
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mmgu.core import store
from mmgu.core.audit import record
from mmgu.core.auth import (
    Viewer,
    current_viewer,
    find_or_create_member,
    hash_token,
    new_token,
    require,
    sync_roles,
)
from mmgu.core.extensions import SearchHit
from mmgu.core.hall import hall
from mmgu.core.models import ApiToken, Identity, LinkCode, Member, Notice, Proposal, Upload
from mmgu.core.permissions import ALL_ROLES, RANK_ORDER
from mmgu.core.uploads import upload_path
from mmgu.core.web import redirect, render
from mmgu.db import get_session, utcnow

router = APIRouter()
DISCORD_API = "https://discord.com/api/v10"


def _safe_next(nxt: str | None) -> str:
    return nxt if nxt and nxt.startswith("/") and not nxt.startswith("//") else "/"


# ----- dashboard ---------------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request, viewer: Viewer = Depends(current_viewer), session: AsyncSession = Depends(get_session)
):
    if not viewer.is_authenticated:
        return render(request, "hall/landing.html")
    if not viewer.member.onboarded:
        return RedirectResponse("/welcome", status_code=303)
    if not viewer.can("hall.view"):
        return render(request, "hall/waiting.html")
    cards = []
    for c in hall.ext.get("hall.cards", hall.enabled_ids):
        html = await c.fn(request, session)
        if html:
            cards.append(html)
    pending = 0
    if viewer.can("proposals.review"):
        pending = (
            await session.execute(select(func.count()).select_from(Proposal).where(Proposal.status == "pending"))
        ).scalar_one()
    return render(request, "hall/dashboard.html", cards=cards, pending_proposals=pending)


# ----- login -------------------------------------------------------------------------------
@router.get("/login", response_class=HTMLResponse)
async def login(request: Request, next: str | None = None, viewer: Viewer = Depends(current_viewer)):
    if viewer.is_authenticated:
        return RedirectResponse(_safe_next(next), status_code=303)
    modes = hall.settings.auth_mode_list
    return render(
        request,
        "hall/login.html",
        modes=modes,
        next=_safe_next(next),
        discord_ready=bool(hall.settings.discord_client_id and hall.settings.discord_client_secret),
        dev=hall.settings.dev_login and "dev" in modes,
    )


@router.post("/auth/dev")
async def dev_login(
    request: Request,
    name: str = Form(...),
    rank: str = Form("member"),
    next: str = Form("/"),
    session: AsyncSession = Depends(get_session),
):
    if not (hall.settings.dev_login and "dev" in hall.settings.auth_mode_list):
        raise HTTPException(404)
    name = name.strip()[:60] or "Tester"
    member, created = await find_or_create_member(session, "dev", name.lower(), display_name=name)
    member.onboarded = True
    if rank in ALL_ROLES:
        await sync_roles(session, member.id, {rank}, "dev")
    await session.commit()
    request.session["member_id"] = member.id
    return redirect(request, _safe_next(next), f"Welcome, {member.display_name}.")


@router.get("/auth/discord/login")
async def discord_login(request: Request, next: str | None = None):
    s = hall.settings
    if "discord" not in s.auth_mode_list or not s.discord_client_id:
        raise HTTPException(404, "Discord login isn't set up on this hall.")
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    request.session["oauth_next"] = _safe_next(next)
    scopes = "identify guilds.members.read" if s.discord_guild_id else "identify"
    q = urlencode(
        {
            "client_id": s.discord_client_id,
            "response_type": "code",
            "scope": scopes,
            "state": state,
            "redirect_uri": s.base_url.rstrip("/") + "/auth/discord/callback",
            "prompt": "none",
        }
    )
    return RedirectResponse(f"https://discord.com/oauth2/authorize?{q}", status_code=303)


@router.get("/auth/discord/callback")
async def discord_callback(
    request: Request, code: str | None = None, state: str | None = None, session: AsyncSession = Depends(get_session)
):
    s = hall.settings
    if not code or not state or state != request.session.pop("oauth_state", None):
        raise HTTPException(400, "The Discord login didn't complete. Try again from the login page.")
    async with httpx.AsyncClient(timeout=15) as client:
        tok = await client.post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": s.base_url.rstrip("/") + "/auth/discord/callback",
            },
            auth=(s.discord_client_id, s.discord_client_secret),
        )
        if tok.status_code != 200:
            raise HTTPException(400, "Discord refused the login. Check the OAuth redirect URL in the Discord app.")
        access = tok.json()["access_token"]
        headers = {"Authorization": f"Bearer {access}"}
        user = (await client.get(f"{DISCORD_API}/users/@me", headers=headers)).json()
        roles: set[str] | None = None
        if s.discord_guild_id:
            gm = await client.get(f"{DISCORD_API}/users/@me/guilds/{s.discord_guild_id}/member", headers=headers)
            if gm.status_code == 200:
                mapping = s.discord_role_pairs
                roles = {mapping[r] for r in gm.json().get("roles", []) if r in mapping and mapping[r] in ALL_ROLES}
            elif not store.get("hall.allow_outsiders"):
                raise HTTPException(403, "You need to be in the guild's Discord server to enter this hall.")
    avatar = None
    if user.get("avatar"):
        avatar = f"https://cdn.discordapp.com/avatars/{user['id']}/{user['avatar']}.png?size=128"
    current_id = request.session.get("member_id")
    existing = (
        await session.execute(select(Identity).where(Identity.provider == "discord", Identity.subject == user["id"]))
    ).scalar_one_or_none()
    if current_id and existing is None:
        # Logged in another way already: attach Discord to this member.
        session.add(Identity(member_id=current_id, provider="discord", subject=user["id"], label=user.get("username")))
        member = await session.get(Member, current_id)
    else:
        member, _ = await find_or_create_member(
            session,
            "discord",
            user["id"],
            label=user.get("username"),
            display_name=user.get("global_name") or user.get("username"),
            avatar_url=avatar,
        )
        member.onboarded = True
    if avatar and not member.avatar_url:
        member.avatar_url = avatar
    if roles is not None and s.discord_role_pairs:
        await sync_roles(session, member.id, roles, "discord")
    await session.commit()
    request.session["member_id"] = member.id
    return RedirectResponse(request.session.pop("oauth_next", "/"), status_code=303)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return redirect(request, "/login", "Logged out.")


# ----- onboarding & profile ----------------------------------------------------------------
@router.get("/welcome", response_class=HTMLResponse)
async def welcome(request: Request, viewer: Viewer = Depends(current_viewer)):
    if not viewer.is_authenticated:
        return RedirectResponse("/login", status_code=303)
    return render(request, "hall/welcome.html")


@router.post("/welcome")
async def welcome_post(
    request: Request,
    display_name: str = Form(...),
    viewer: Viewer = Depends(current_viewer),
    session: AsyncSession = Depends(get_session),
):
    if not viewer.is_authenticated:
        raise HTTPException(401)
    name = display_name.strip()[:60]
    if len(name) < 2:
        raise HTTPException(422, "Pick a name with at least two letters.")
    member = await session.get(Member, viewer.id)
    member.display_name = name
    member.onboarded = True
    await session.commit()
    nxt = "/roster/me" if hall.is_enabled("roster") else "/"
    return redirect(request, nxt, f"Welcome to the hall, {name}. Add your characters next.")


@router.get("/me", response_class=HTMLResponse)
async def me(
    request: Request, viewer: Viewer = Depends(require("hall.view")), session: AsyncSession = Depends(get_session)
):
    idents = (await session.execute(select(Identity).where(Identity.member_id == viewer.id))).scalars().all()
    tokens = (
        (
            await session.execute(
                select(ApiToken)
                .where(ApiToken.member_id == viewer.id, ApiToken.revoked.is_(False))
                .order_by(ApiToken.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    panels = []
    for c in hall.ext.get("member.panels", hall.enabled_ids):
        html = await c.fn(request, session, viewer.member)
        if html:
            panels.append(html)
    new_token_value = request.session.pop("new_token", None)
    link_code = request.session.pop("link_code", None)
    return render(
        request,
        "hall/me.html",
        identities=idents,
        tokens=tokens,
        panels=panels,
        new_token=new_token_value,
        link_code=link_code,
        has_discord=any(i.provider == "discord" for i in idents),
    )


@router.post("/me")
async def me_post(
    request: Request,
    display_name: str = Form(...),
    timezone: str = Form(""),
    viewer: Viewer = Depends(require("hall.view")),
    session: AsyncSession = Depends(get_session),
):
    member = await session.get(Member, viewer.id)
    name = display_name.strip()[:60]
    if len(name) >= 2:
        member.display_name = name
    member.timezone = timezone.strip()[:64] or None
    await session.commit()
    return redirect(request, "/me", "Profile saved.")


@router.post("/me/link-code")
async def link_code(
    request: Request, viewer: Viewer = Depends(require("hall.view")), session: AsyncSession = Depends(get_session)
):
    code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
    session.add(LinkCode(code=code, member_id=viewer.id, expires_at=utcnow() + timedelta(minutes=15)))
    await session.commit()
    request.session["link_code"] = code
    return redirect(request, "/me#discord")


@router.post("/me/tokens")
async def create_token(
    request: Request,
    name: str = Form("companion"),
    viewer: Viewer = Depends(require("api.tokens")),
    session: AsyncSession = Depends(get_session),
):
    raw = new_token()
    tok = ApiToken(member_id=viewer.id, name=name.strip()[:80] or "token", prefix=raw[:9], token_hash=hash_token(raw))
    session.add(tok)
    await record(
        session,
        "token.created",
        actor_id=viewer.id,
        entity_type="ApiToken",
        summary=f"API token '{tok.name}'",
        after={"name": tok.name, "prefix": tok.prefix},
    )
    await session.commit()
    request.session["new_token"] = raw
    return redirect(request, "/me#tokens")


@router.post("/me/tokens/{token_id}/revoke")
async def revoke_token(
    request: Request,
    token_id: int,
    viewer: Viewer = Depends(require("hall.view")),
    session: AsyncSession = Depends(get_session),
):
    tok = await session.get(ApiToken, token_id)
    if tok is None or tok.member_id != viewer.id:
        raise HTTPException(404)
    tok.revoked = True
    await record(session, "token.revoked", actor_id=viewer.id, entity=tok, summary=f"Revoked token '{tok.name}'")
    await session.commit()
    return redirect(request, "/me#tokens", "Token revoked.")


# ----- notices -----------------------------------------------------------------------------
@router.get("/notices", response_class=HTMLResponse)
async def notices(
    request: Request, viewer: Viewer = Depends(require("hall.view")), session: AsyncSession = Depends(get_session)
):
    rows = (
        (
            await session.execute(
                select(Notice).where(Notice.member_id == viewer.id).order_by(Notice.created_at.desc()).limit(100)
            )
        )
        .scalars()
        .all()
    )
    await session.execute(update(Notice).where(Notice.member_id == viewer.id, Notice.read.is_(False)).values(read=True))
    await session.commit()
    return render(request, "hall/notices.html", notices=rows)


@router.get("/notices/count", response_class=HTMLResponse)
async def notices_count(viewer: Viewer = Depends(current_viewer), session: AsyncSession = Depends(get_session)):
    if not viewer.is_authenticated:
        return HTMLResponse("<span></span>")
    n = (
        await session.execute(
            select(func.count()).select_from(Notice).where(Notice.member_id == viewer.id, Notice.read.is_(False))
        )
    ).scalar_one()
    return HTMLResponse(f'<span class="count">{n}</span>' if n else "<span></span>")


# ----- search ------------------------------------------------------------------------------
async def _search(session: AsyncSession, q: str, viewer: Viewer) -> list[SearchHit]:
    hits: list[SearchHit] = []
    if len(q.strip()) < 2:
        return hits
    for c in hall.ext.get("search.providers", hall.enabled_ids):
        hits.extend(await c.fn(session, q.strip(), viewer))
    hits.sort(key=lambda h: -h.score)
    return hits


@router.get("/search", response_class=HTMLResponse)
async def search(
    request: Request,
    q: str = "",
    viewer: Viewer = Depends(require("hall.view")),
    session: AsyncSession = Depends(get_session),
):
    return render(request, "hall/search.html", q=q, hits=await _search(session, q, viewer))


@router.get("/search/quick", response_class=HTMLResponse)
async def search_quick(
    request: Request,
    q: str = "",
    viewer: Viewer = Depends(require("hall.view")),
    session: AsyncSession = Depends(get_session),
):
    hits = (await _search(session, q, viewer))[:8]
    return render(request, "hall/_quick.html", q=q, hits=hits)


# ----- uploads & proposals -----------------------------------------------------------------
@router.get("/uploads/{upload_id}")
async def get_upload(
    upload_id: int, viewer: Viewer = Depends(require("hall.view")), session: AsyncSession = Depends(get_session)
):
    up = await session.get(Upload, upload_id)
    if up is None or not upload_path(up).exists():
        raise HTTPException(404, "That image is gone.")
    return FileResponse(upload_path(up), media_type=up.mime, headers={"Cache-Control": "private, max-age=86400"})


@router.get("/proposals", response_class=HTMLResponse)
async def proposals(
    request: Request, viewer: Viewer = Depends(require("hall.view")), session: AsyncSession = Depends(get_session)
):
    q = select(Proposal).where(Proposal.status == "pending").order_by(Proposal.created_at.desc())
    if not viewer.can("proposals.review"):
        q = q.where(Proposal.created_by == viewer.id)
    rows = (await session.execute(q.limit(200))).scalars().all()
    return render(request, "hall/proposals.html", proposals=rows)


@router.post("/proposals/{proposal_id}/reject")
async def reject_proposal(
    request: Request,
    proposal_id: int,
    viewer: Viewer = Depends(require("hall.view")),
    session: AsyncSession = Depends(get_session),
):
    p = await session.get(Proposal, proposal_id)
    if p is None:
        raise HTTPException(404)
    if p.created_by != viewer.id and not viewer.can("proposals.review"):
        raise HTTPException(403, "Only the person who made it or an officer can discard this.")
    p.status = "rejected"
    p.decided_by = viewer.id
    p.decided_at = utcnow()
    await record(session, "proposal.rejected", actor_id=viewer.id, entity=p, summary=p.summary)
    await session.commit()
    return redirect(request, "/proposals", "Discarded.")


def rank_sort_key(rank: str) -> int:
    return -RANK_ORDER.get(rank, 0)


# ----- JSON API: suggested changes (used by the MCP server and companion tools) ------------
@router.get("/api/proposals")
async def api_proposals(
    status: str = "pending",
    viewer: Viewer = Depends(require("hall.view")),
    session: AsyncSession = Depends(get_session),
):
    q = select(Proposal).where(Proposal.status == status).order_by(Proposal.created_at.desc()).limit(100)
    if not viewer.can("proposals.review"):
        q = q.where(Proposal.created_by == viewer.id)
    rows = (await session.execute(q)).scalars().all()
    return {
        "proposals": [
            {
                "id": p.id,
                "kind": p.kind,
                "source": p.source,
                "summary": p.summary,
                "status": p.status,
                "payload": p.payload,
                "created_at": p.created_at.isoformat(),
            }
            for p in rows
        ]
    }


@router.post("/api/proposals")
async def api_propose(
    request: Request, viewer: Viewer = Depends(require("hall.view")), session: AsyncSession = Depends(get_session)
):
    """Suggest a change for a person to review. Machines (Claude, companions) never write directly."""
    body = await request.json()
    kind = str(body.get("kind") or "note")[:60]
    p = Proposal(
        kind=kind,
        source=str(body.get("source") or "api")[:60],
        summary=str(body.get("summary") or "")[:300],
        payload=dict(body.get("payload") or {}),
        created_by=viewer.id,
    )
    session.add(p)
    await session.flush()
    await record(session, "proposal.created", actor_id=viewer.id, entity=p, via="api", summary=p.summary)
    await session.commit()
    return {"id": p.id, "status": p.status}
