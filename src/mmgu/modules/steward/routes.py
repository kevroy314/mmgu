from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mmgu.core import store
from mmgu.core.audit import record
from mmgu.core.auth import Viewer, require, roles_for
from mmgu.core.hall import hall
from mmgu.core.models import AuditLog, Identity, Member, RoleGrant
from mmgu.core.permissions import DUTIES, RANK_ORDER, RANKS
from mmgu.core.web import redirect, render
from mmgu.db import get_session, utcnow

router = APIRouter(prefix="/steward")

HALL_FIELDS = [
    ("hall.guild_name", "Guild name", "text", "Shown in the header and on Discord posts."),
    ("hall.app_name", "Hall name", "text", "What the tool is called for your guild."),
    ("hall.default_role", "Rank for newcomers", "select", "Rank given to people who log in for the first time."),
    (
        "hall.allow_outsiders",
        "Let people outside the Discord server log in",
        "bool",
        "They arrive as the newcomer rank.",
    ),
]


def _reload_bot() -> None:
    if hall.discord is not None and hall.discord.available:
        asyncio.get_running_loop().create_task(hall.discord.reload_commands())


@router.get("", response_class=HTMLResponse)
async def office(
    request: Request, viewer: Viewer = Depends(require("members.manage")), session: AsyncSession = Depends(get_session)
):
    counts = {
        "members": (
            await session.execute(select(func.count()).select_from(Member).where(Member.status == "active"))
        ).scalar_one(),
        "audit": (await session.execute(select(func.count()).select_from(AuditLog))).scalar_one(),
    }
    acks = {m.id: store.get(f"modules.ack.{m.id}") for m in hall.modules.values()}
    from mmgu.core.modules import BROKEN

    return render(
        request,
        "steward/office.html",
        modules=list(hall.modules.values()),
        counts=counts,
        acks=acks,
        bot_online=bool(hall.discord and hall.discord.available),
        broken=BROKEN,
    )


@router.post("/modules/{module_id}")
async def toggle_module(
    request: Request,
    module_id: str,
    viewer: Viewer = Depends(require("admin.modules")),
    session: AsyncSession = Depends(get_session),
):
    m = hall.modules.get(module_id)
    if m is None or m.kind == "core":
        raise HTTPException(404)
    form = await request.form()
    turn_on = form.get("enable") == "1"
    state = dict(store.get("modules.enabled", {}) or {})
    if turn_on and m.needs_acknowledgement:
        if form.get("ack") != "yes":
            raise HTTPException(
                422, "Read the notice and tick the box to accept responsibility before turning this on."
            )
        await store.put(
            session,
            f"modules.ack.{module_id}",
            {"by": viewer.id, "at": utcnow().isoformat(), "notice": m.tos_notice},
            viewer.id,
        )
    missing = [r for r in m.requires if not hall.is_enabled(r)]
    if turn_on and missing:
        raise HTTPException(422, f"Turn on {', '.join(missing)} first; {m.name} needs it.")
    state[module_id] = turn_on
    await store.put(session, "modules.enabled", state, viewer.id)
    await record(
        session,
        "module.enabled" if turn_on else "module.disabled",
        actor_id=viewer.id,
        entity_type="Module",
        entity_id=module_id,
        summary=f"{'Opened' if turn_on else 'Closed'} {m.name}"
        + (" (accepted the terms-of-service notice)" if turn_on and m.needs_acknowledgement else ""),
    )
    await session.commit()
    _reload_bot()
    return redirect(request, "/steward", f"{m.name} is now {'open' if turn_on else 'closed'}.")


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, viewer: Viewer = Depends(require("admin.settings"))):
    channels: list[tuple[str, str, str]] = [
        ("default", "Default channel", "Used when a module has no channel of its own.")
    ]
    for m in hall.enabled_modules():
        for key, label in m.channels.items():
            channels.append((key, label, m.name))
    module_fields = [(m, m.settings) for m in hall.enabled_modules() if m.settings]
    return render(
        request,
        "steward/settings.html",
        hall_fields=HALL_FIELDS,
        channels=channels,
        module_fields=module_fields,
        values=store.all_values(),
        ranks=RANKS,
    )


@router.post("/settings")
async def settings_save(
    request: Request, viewer: Viewer = Depends(require("admin.settings")), session: AsyncSession = Depends(get_session)
):
    form = await request.form()
    changed = []
    for key, _label, kind, _help in HALL_FIELDS:
        value = (form.get(key) == "1") if kind == "bool" else (str(form.get(key, "")).strip() or None)
        if store.get(key) != value:
            await store.put(session, key, value, viewer.id)
            changed.append(key)
    for key, value in form.items():
        if key.startswith("channels."):
            v = str(value).strip() or None
            if v and not v.isdigit():
                raise HTTPException(
                    422, f"Channel ids are numbers. Right-click a channel in Discord → Copy Channel ID ({key})."
                )
            if store.get(key) != v:
                await store.put(session, key, v, viewer.id)
                changed.append(key)
    for m in hall.enabled_modules():
        for f in m.settings:
            full = f"{m.id}.{f.key}"
            if f.type == "bool":
                v = form.get(full) == "1"
            elif f.type == "int":
                raw = str(form.get(full, "")).strip()
                v = int(raw) if raw.lstrip("-").isdigit() else f.default
            elif f.type == "secret":
                raw = str(form.get(full, "")).strip()
                if not raw:
                    continue  # blank means keep the stored secret
                v = raw
            else:
                v = str(form.get(full, "")).strip() or None
            if store.get(full) != v:
                await store.put(session, full, v, viewer.id)
                changed.append(full)
    if changed:
        safe = [
            c
            for c in changed
            if not any(f.type == "secret" and c.endswith(f.key) for m in hall.modules.values() for f in m.settings)
        ]
        await record(
            session,
            "settings.changed",
            actor_id=viewer.id,
            entity_type="Settings",
            summary=f"Changed {len(changed)} setting(s)",
            after={"keys": safe},
        )
    await session.commit()
    return redirect(request, "/steward/settings", "Settings saved." if changed else "Nothing changed.")


@router.get("/permissions", response_class=HTMLResponse)
async def permissions_page(request: Request, viewer: Viewer = Depends(require("admin.permissions"))):
    overrides = store.get("permissions.overrides", {}) or {}
    groups: dict[str, list] = {}
    for p in hall.perms.perms.values():
        if p.module != "core" and not hall.is_enabled(p.module):
            continue
        min_rank, duties = hall.perms.rule(p.key, overrides)
        label = hall.modules[p.module].name if p.module in hall.modules else "The Hall"
        groups.setdefault(label, []).append(
            {"perm": p, "min_rank": min_rank, "duties": duties, "changed": p.key in overrides}
        )
    return render(request, "steward/permissions.html", groups=groups, ranks=RANKS, duties=DUTIES)


@router.post("/permissions")
async def permissions_save(
    request: Request,
    viewer: Viewer = Depends(require("admin.permissions")),
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    overrides: dict = {}
    for key, p in hall.perms.perms.items():
        rank = form.get(f"rank:{key}")
        if rank not in RANK_ORDER:
            continue
        duties = tuple(sorted(form.getlist(f"duty:{key}")))
        if rank != p.min_rank or duties != tuple(sorted(p.duties)):
            overrides[key] = {"min_rank": rank, "duties": list(duties)}
    # Never let a leader lock everyone out of the permission screen.
    overrides.pop("admin.permissions", None)
    before = store.get("permissions.overrides", {})
    await store.put(session, "permissions.overrides", overrides, viewer.id)
    await record(
        session,
        "permissions.changed",
        actor_id=viewer.id,
        entity_type="Permissions",
        summary=f"{len(overrides)} permission(s) differ from defaults",
        before=before,
        after=overrides,
    )
    await session.commit()
    return redirect(request, "/steward/permissions", "Permissions saved.")


@router.get("/members", response_class=HTMLResponse)
async def members_page(
    request: Request, viewer: Viewer = Depends(require("members.manage")), session: AsyncSession = Depends(get_session)
):
    members = (await session.execute(select(Member).order_by(Member.display_name))).scalars().all()
    grants = (await session.execute(select(RoleGrant))).scalars().all()
    idents = (await session.execute(select(Identity.member_id, Identity.provider))).all()
    by_member: dict[int, dict] = {m.id: {"roles": {}, "providers": set()} for m in members}
    for g in grants:
        by_member.setdefault(g.member_id, {"roles": {}, "providers": set()})["roles"][g.role] = g.source
    for mid, prov in idents:
        by_member.setdefault(mid, {"roles": {}, "providers": set()})["providers"].add(prov)
    return render(request, "steward/members.html", members=members, info=by_member, ranks=RANKS, duties=DUTIES)


@router.post("/members/{member_id}")
async def member_save(
    request: Request,
    member_id: int,
    viewer: Viewer = Depends(require("members.manage")),
    session: AsyncSession = Depends(get_session),
):
    member = await session.get(Member, member_id)
    if member is None:
        raise HTTPException(404)
    form = await request.form()
    rank = form.get("rank")
    duties = set(form.getlist("duty"))
    status = form.get("status") or member.status
    if rank not in RANK_ORDER:
        raise HTTPException(422, "Unknown rank.")
    # Officers can't create leaders or change leaders; only leaders can.
    current = await roles_for(session, member_id)
    if not viewer.at_least("leader") and ("leader" in current or RANK_ORDER[rank] >= RANK_ORDER["officer"]):
        raise HTTPException(403, "Only a leader can promote to officer or change a leader.")
    if member_id == viewer.id and RANK_ORDER[rank] < RANK_ORDER[viewer.rank]:
        raise HTTPException(422, "You can't demote yourself here. Ask another leader.")
    before = {"roles": sorted(current), "status": member.status}
    rows = (await session.execute(select(RoleGrant).where(RoleGrant.member_id == member_id))).scalars().all()
    for g in rows:
        if g.source in ("manual", "bootstrap", "dev"):
            await session.delete(g)
    await session.flush()
    remaining = {g.role for g in rows if g.source not in ("manual", "bootstrap", "dev")}
    for role in {rank} | {d for d in duties if d in dict((k, 1) for k, _, _ in DUTIES)}:
        if role not in remaining:
            session.add(RoleGrant(member_id=member_id, role=role, source="manual"))
    if status in ("active", "left", "banned"):
        member.status = status
    await session.flush()
    after = {"roles": sorted(await roles_for(session, member_id)), "status": member.status}
    await record(
        session,
        "member.roles_changed",
        actor_id=viewer.id,
        entity_type="Member",
        entity_id=member_id,
        summary=f"{member.display_name}: {', '.join(after['roles']) or 'no roles'} ({member.status})",
        before=before,
        after=after,
    )
    await session.commit()
    return redirect(request, "/steward/members", f"Updated {member.display_name}.")


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(
    request: Request,
    action: str = "",
    page: int = 1,
    viewer: Viewer = Depends(require("audit.view")),
    session: AsyncSession = Depends(get_session),
):
    q = select(AuditLog).order_by(AuditLog.at.desc())
    if action:
        q = q.where(AuditLog.action.like(f"{action}%"))
    page = max(1, page)
    rows = (await session.execute(q.offset((page - 1) * 100).limit(101))).scalars().all()
    actor_ids = {r.actor_id for r in rows if r.actor_id}
    names = {}
    if actor_ids:
        names = dict(
            (await session.execute(select(Member.id, Member.display_name).where(Member.id.in_(actor_ids)))).all()
        )
    return render(
        request, "steward/audit.html", rows=rows[:100], more=len(rows) > 100, page=page, action=action, names=names
    )
