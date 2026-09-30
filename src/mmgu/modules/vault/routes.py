from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from mmgu.core.audit import record
from mmgu.core.auth import Viewer, require
from mmgu.core.hall import hall
from mmgu.core.models import Character, Member, Proposal
from mmgu.core.uploads import UploadError, parse_crop, save_image
from mmgu.core.web import redirect, render
from mmgu.db import get_session, utcnow
from mmgu.modules.vault import services
from mmgu.modules.vault.models import Holding
from mmgu.modules.vault.models import Request as VaultRequest

router = APIRouter()


def _int(v) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


async def _mule(session: AsyncSession, character_id: int) -> Character:
    try:
        return await services.get_mule(session, character_id)
    except services.VaultError as e:
        raise HTTPException(404, str(e)) from e


async def _request(session: AsyncSession, request_id: int) -> VaultRequest:
    req = await session.get(VaultRequest, request_id)
    if req is None:
        raise HTTPException(404, "That bank request doesn't exist.")
    return req


# ----- holdings ---------------------------------------------------------------------------------
@router.get("/vault", response_class=HTMLResponse)
async def index(
    request: Request,
    q: str = "",
    mule: str = "",
    view: str = "items",
    viewer: Viewer = Depends(require("vault.view")),
    session: AsyncSession = Depends(get_session),
):
    mule_id = _int(mule)
    rows = await services.holdings(session, q=q, character_id=mule_id)
    summaries = await services.mule_summaries(session)
    counts = await services.request_counts(session)
    mine = await services.list_requests(session, status="open", requester_id=viewer.id, limit=5) if viewer.id else []
    recent = await services.history(session, limit=6)
    view = "mules" if view == "mules" else "items"
    by_mule: dict[int, list] = {}
    for h, c in rows:
        by_mule.setdefault(c.id, []).append(h)
    ctx = dict(
        q=q,
        mule_id=mule_id,
        view=view,
        rows=rows,
        totals=services.totals(rows),
        by_mule=by_mule,
        summaries=summaries,
        pending=counts.get("pending", 0),
        approved=counts.get("approved", 0),
        mine=mine,
        recent=recent,
        searching=bool(q or mule_id),
    )
    tpl = "vault/_holdings.html" if request.headers.get("hx-target") == "holdings" else "vault/index.html"
    return render(request, tpl, **ctx)


@router.get("/vault/mules/{character_id}", response_class=HTMLResponse)
async def mule_page(
    request: Request,
    character_id: int,
    viewer: Viewer = Depends(require("vault.view")),
    session: AsyncSession = Depends(get_session),
):
    mule = await _mule(session, character_id)
    rows = await services.holdings(session, character_id=mule.id)
    owner = await session.get(Member, mule.member_id) if mule.member_id else None
    return render(
        request,
        "vault/mule.html",
        mule=mule,
        owner=owner,
        rows=rows,
        pieces=sum(h.qty for h, _ in rows),
        tx=await services.history(session, character_id=mule.id, limit=40),
        mules=await services.mules(session),
    )


@router.post("/vault/tx")
async def transaction(
    request: Request, viewer: Viewer = Depends(require("vault.manage")), session: AsyncSession = Depends(get_session)
):
    form = await request.form()
    mule = await _mule(session, _int(form.get("character_id")) or 0)
    kind = form.get("kind") or "deposit"
    if kind not in ("deposit", "withdraw", "adjust"):
        raise HTTPException(422, "Choose deposit, withdraw or set count.")
    try:
        tx = await services.change(
            session,
            viewer,
            mule,
            form.get("name") or "",
            kind=kind,
            qty=form.get("qty") or ("1" if kind != "adjust" else ""),
            note=form.get("note"),
        )
    except services.VaultError as e:
        raise HTTPException(422, str(e)) from e
    await session.commit()
    verb = {"deposit": "Deposited", "withdraw": "Withdrew", "adjust": "Count set:"}[kind]
    amount = abs(tx.delta) if kind != "adjust" else tx.qty_after
    back = form.get("next") or f"/vault/mules/{mule.id}"
    if not str(back).startswith("/"):
        back = f"/vault/mules/{mule.id}"
    return redirect(request, back, f"{verb} {amount} × {tx.name} on {mule.name}. {mule.name} now has {tx.qty_after}.")


@router.post("/vault/holdings/{holding_id}")
async def holding_details(
    request: Request,
    holding_id: int,
    viewer: Viewer = Depends(require("vault.manage")),
    session: AsyncSession = Depends(get_session),
):
    h = await session.get(Holding, holding_id)
    if h is None:
        raise HTTPException(404, "That bank entry is gone.")
    form = await request.form()
    await services.update_holding_details(session, viewer, h, slot=form.get("slot"), note=form.get("note"))
    await session.commit()
    return redirect(request, f"/vault/mules/{h.character_id}", f"Saved the note on {h.name}.")


@router.get("/vault/history", response_class=HTMLResponse)
async def history_page(
    request: Request,
    q: str = "",
    mule: str = "",
    viewer: Viewer = Depends(require("vault.view")),
    session: AsyncSession = Depends(get_session),
):
    mule_id = _int(mule)
    return render(
        request,
        "vault/history.html",
        tx=await services.history(session, character_id=mule_id, q=q, limit=300),
        q=q,
        mule_id=mule_id,
        mules=await services.mules(session),
    )


# ----- bulk inventory update -----------------------------------------------------------------
@router.get("/vault/mules/{character_id}/update", response_class=HTMLResponse)
async def update_form(
    request: Request,
    character_id: int,
    proposal: int | None = None,
    viewer: Viewer = Depends(require("vault.manage")),
    session: AsyncSession = Depends(get_session),
):
    mule = await _mule(session, character_id)
    prop = None
    preview = None
    lines: list[dict] = []
    if proposal:
        prop = await session.get(Proposal, proposal)
        if prop is None or prop.kind != "vault.holdings" or prop.status != "pending":
            raise HTTPException(404, "That screenshot was already reviewed.")
        if prop.payload.get("character_id") != mule.id:
            raise HTTPException(422, "That screenshot belongs to a different bank mule.")
        lines = services.clean_lines(prop.payload.get("lines") or [])
        preview = await services.diff(session, mule, lines)
    text = "\n".join(f"{ln['name']} x{ln['qty']}" for ln in lines)
    return render(
        request,
        "vault/update.html",
        mule=mule,
        proposal=prop,
        preview=preview,
        lines=lines,
        text=text,
        skipped=[],
        reader=services.reader_available(),
        bank_only=False,
    )


@router.post("/vault/mules/{character_id}/update/preview", response_class=HTMLResponse)
async def update_preview(
    request: Request,
    character_id: int,
    viewer: Viewer = Depends(require("vault.manage")),
    session: AsyncSession = Depends(get_session),
):
    mule = await _mule(session, character_id)
    form = await request.form()
    text = str(form.get("text") or "")
    bank_only = bool(form.get("bank_only"))
    parsed = services.parse_lines(text, bank_only=bank_only)
    if not parsed.lines and not form.get("allow_empty"):
        raise HTTPException(
            422,
            "No items found in what you pasted. Put one item per line, like 'Rusty Scimitar x3' or '3 Bone Chips'.",
        )
    prop = None
    if form.get("proposal_id"):
        prop = await session.get(Proposal, _int(form.get("proposal_id")) or 0)
    return render(
        request,
        "vault/update.html",
        mule=mule,
        proposal=prop,
        preview=await services.diff(session, mule, parsed.lines),
        lines=parsed.lines,
        text=text,
        skipped=parsed.skipped,
        fmt=parsed.format,
        reader=services.reader_available(),
        bank_only=bank_only,
    )


@router.post("/vault/mules/{character_id}/update/apply")
async def update_apply(
    request: Request,
    character_id: int,
    viewer: Viewer = Depends(require("vault.manage")),
    session: AsyncSession = Depends(get_session),
):
    mule = await _mule(session, character_id)
    form = await request.form()
    try:
        lines = services.clean_lines(json.loads(form.get("lines") or "[]"))
    except (ValueError, TypeError) as e:
        raise HTTPException(422, "The preview was damaged. Paste the inventory again.") from e
    d = await services.set_inventory(session, viewer, mule, lines, note=form.get("note"))
    if form.get("proposal_id"):
        prop = await session.get(Proposal, _int(form.get("proposal_id")) or 0)
        if prop is not None and prop.status == "pending" and prop.kind == "vault.holdings":
            prop.status, prop.decided_by, prop.decided_at = "accepted", viewer.id, utcnow()
            prop.result_ref = f"vault.mule:{mule.id}"
            await record(session, "proposal.accepted", actor_id=viewer.id, entity=prop, summary=prop.summary)
    await session.commit()
    msg = (
        f"{mule.name} updated: {len(d['added'])} added, {len(d['changed'])} changed, {len(d['removed'])} removed."
        if d["has_changes"]
        else f"Nothing changed; {mule.name} already matched."
    )
    return redirect(request, f"/vault/mules/{mule.id}", msg)


@router.post("/vault/mules/{character_id}/read")
async def read(
    request: Request,
    character_id: int,
    viewer: Viewer = Depends(require("vault.manage")),
    session: AsyncSession = Depends(get_session),
):
    mule = await _mule(session, character_id)
    if not services.reader_available():
        raise HTTPException(422, "No screenshot reader is turned on. Paste the inventory as text instead.")
    form = await request.form()
    shot = form.get("screenshot")
    if shot is None or not getattr(shot, "filename", ""):
        raise HTTPException(422, "Choose or paste a screenshot of the bank window first.")
    try:
        up = await save_image(session, await shot.read(), uploaded_by=viewer.id, crop=parse_crop(form.get("crop")))
    except UploadError as e:
        raise HTTPException(422, str(e)) from e
    prop = await services.read_screenshot(session, viewer, up, mule)
    await session.commit()
    if not prop.payload.get("lines"):
        return redirect(
            request,
            f"/vault/mules/{mule.id}/update",
            "The reader couldn't find any items in that screenshot. Paste the inventory as text instead.",
            "warn",
        )
    return redirect(request, prop.payload["review_url"], "Read the screenshot. Check the changes before applying.")


# ----- requests ----------------------------------------------------------------------------------
@router.get("/vault/requests", response_class=HTMLResponse)
async def requests_page(
    request: Request,
    tab: str = "",
    viewer: Viewer = Depends(require("vault.view")),
    session: AsyncSession = Depends(get_session),
):
    banker = viewer.can("vault.manage")
    tab = tab or ("open" if banker else "mine")
    if tab not in ("open", "mine", "done", "all"):
        tab = "open"
    if tab == "mine":
        rows = await services.list_requests(session, requester_id=viewer.id)
    elif tab == "done":
        rows = [
            r
            for r in await services.list_requests(session, limit=300)
            if r[0].status in ("fulfilled", "denied", "cancelled")
        ][:100]
    elif tab == "all":
        rows = await services.list_requests(session)
    else:
        rows = await services.list_requests(session, status="open")
    stock = {}
    for r, _ in rows:
        if r.status in services.OPEN_STATUSES:
            stock[r.id] = await services.held_qty(session, item_id=r.item_id, name=r.name)
    counts = await services.request_counts(session)
    return render(
        request,
        "vault/requests.html",
        rows=rows,
        tab=tab,
        stock=stock,
        banker=banker,
        open_count=counts.get("pending", 0) + counts.get("approved", 0),
        mules=await services.mules(session) if banker else [],
    )


@router.get("/vault/requests/new", response_class=HTMLResponse)
async def new_request_form(
    request: Request,
    item: str = "",
    item_id: int | None = None,
    viewer: Viewer = Depends(require("vault.request")),
    session: AsyncSession = Depends(get_session),
):
    name = item
    if item_id and not name:
        from mmgu.modules.archive.models import Item

        it = await session.get(Item, item_id)
        name = it.name if it else ""
    held = await services.held_qty(session, item_id=item_id, name=name) if (item_id or name) else None
    names = sorted({t["name"] for t in services.totals(await services.holdings(session))})
    return render(request, "vault/request_new.html", name=name, held=held, names=names)


@router.post("/vault/requests")
async def create_request(
    request: Request, viewer: Viewer = Depends(require("vault.request")), session: AsyncSession = Depends(get_session)
):
    form = await request.form()
    try:
        req = await services.create_request(
            session, viewer, name=form.get("name") or "", qty=form.get("qty") or 1, reason=form.get("reason")
        )
    except services.VaultError as e:
        raise HTTPException(422, str(e)) from e
    await session.commit()
    return redirect(request, f"/vault/requests/{req.id}", "Request sent. The bankers will see it in their queue.")


@router.get("/vault/requests/{request_id}", response_class=HTMLResponse)
async def request_page(
    request: Request,
    request_id: int,
    viewer: Viewer = Depends(require("vault.view")),
    session: AsyncSession = Depends(get_session),
):
    req = await _request(session, request_id)
    requester = await session.get(Member, req.requester_id) if req.requester_id else None
    decider = await session.get(Member, req.decided_by) if req.decided_by else None
    source = await session.get(Character, req.fulfilled_from) if req.fulfilled_from else None
    holders = await services.holders_of(session, item_id=req.item_id, name=req.name)
    suggested = await services.suggest_mule(session, req)
    return render(
        request,
        "vault/request.html",
        req=req,
        requester=requester,
        decider=decider,
        source=source,
        holders=holders,
        suggested=suggested,
        mules=await services.mules(session),
        banker=viewer.can("vault.manage"),
    )


@router.post("/vault/requests/{request_id}/{action}")
async def request_action(
    request: Request,
    request_id: int,
    action: str,
    viewer: Viewer = Depends(require("vault.view")),
    session: AsyncSession = Depends(get_session),
):
    req = await _request(session, request_id)
    form = await request.form()
    note = form.get("note")
    back = form.get("next") or services.request_url(req)
    if not str(back).startswith("/"):
        back = services.request_url(req)
    if action != "cancel" and not viewer.can("vault.manage"):
        raise HTTPException(403, "Only bankers and officers can decide bank requests.")
    try:
        if action == "approve":
            await services.approve_request(session, viewer, req, note)
            msg = f"Approved. {req.name} is waiting to be handed over."
        elif action == "deny":
            await services.deny_request(session, viewer, req, note)
            msg = "Declined. They've been told."
        elif action == "fulfil":
            mule = await _mule(session, _int(form.get("character_id")) or 0)
            tx = await services.fulfil_request(session, viewer, req, mule, qty=_int(form.get("qty")), note=note)
            msg = f"Done. Withdrew {abs(tx.delta)} × {req.name} from {mule.name}."
        elif action == "cancel":
            await services.cancel_request(session, viewer, req)
            msg = "Request cancelled."
        else:
            raise HTTPException(404)
    except services.VaultError as e:
        raise HTTPException(422, str(e)) from e
    await session.commit()
    return redirect(request, back, msg)


# ----- JSON API ---------------------------------------------------------------------------------
@router.get("/api/vault/holdings")
async def api_holdings(
    q: str = "", viewer: Viewer = Depends(require("vault.view")), session: AsyncSession = Depends(get_session)
):
    rows = await services.holdings(session, q=q)
    return {
        "holdings": [
            {"item": h.name, "item_id": h.item_id, "qty": h.qty, "character": c.name, "slot": h.slot, "note": h.note}
            for h, c in rows
        ]
    }


@router.get("/api/vault/requests")
async def api_requests(
    status: str = "pending",
    viewer: Viewer = Depends(require("vault.view")),
    session: AsyncSession = Depends(get_session),
):
    if status not in (*services.REQUEST_STATUSES, "open", "all"):
        raise HTTPException(422, f"status must be one of: {', '.join(services.REQUEST_STATUSES)}, open, all")
    rows = await services.list_requests(session, status=None if status == "all" else status)
    base = hall.settings.base_url.rstrip("/")
    return {
        "requests": [
            {
                "id": r.id,
                "item": r.name,
                "item_id": r.item_id,
                "qty": r.qty,
                "reason": r.reason,
                "requester": m.display_name if m else None,
                "status": r.status,
                "created_at": r.created_at.isoformat() + "Z",
                "url": base + services.request_url(r),
            }
            for r, m in rows
        ]
    }
