from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from mmgu.core.auth import Viewer, require
from mmgu.core.hall import hall
from mmgu.core.models import Member
from mmgu.core.web import redirect, render
from mmgu.db import get_session
from mmgu.modules.board import services
from mmgu.modules.board.models import BoardRequest, Listing

router = APIRouter()

TABS = ("all", "wts", "wtb", "requests", "mine")


async def _listing(session: AsyncSession, listing_id: int) -> Listing:
    li = await session.get(Listing, listing_id)
    if li is None:
        raise HTTPException(404, "That listing isn't on the board.")
    return li


async def _req(session: AsyncSession, request_id: int) -> BoardRequest:
    r = await session.get(BoardRequest, request_id)
    if r is None:
        raise HTTPException(404, "That request isn't on the board.")
    return r


@router.get("/board", response_class=HTMLResponse)
async def board(
    request: Request,
    tab: str = "all",
    q: str = "",
    server: str = "",
    category: str = "",
    viewer: Viewer = Depends(require("board.view")),
    session: AsyncSession = Depends(get_session),
):
    tab = tab if tab in TABS else "all"
    listings: list = []
    reqs: list = []
    if tab in ("all", "wts", "wtb"):
        kind = tab.upper() if tab != "all" else None
        listings = await services.list_listings(session, kind=kind, q=q, server=server)
    if tab in ("all", "requests"):
        reqs = await services.list_requests(session, status="active", category=category, q=q)
    if tab == "mine":
        listings = await services.list_listings(session, author_id=viewer.id, status=None, q=q, limit=100)
        reqs = await services.list_requests(
            session, status=None, author_id=viewer.id, claimed_by=viewer.id, q=q, limit=100
        )
    servers = sorted({s for s in [services.default_server(), *hall.game.servers] if s})
    ctx = dict(
        tab=tab,
        q=q,
        server=server,
        category=category,
        listings=listings,
        reqs=reqs,
        counts=await services.counts(session),
        categories=services.CATEGORIES,
        servers=servers,
        helpers=await services.top_helpers(session),
        filtering=bool(q or server or category),
    )
    tpl = "board/_results.html" if request.headers.get("hx-target") == "board-results" else "board/index.html"
    return render(request, tpl, **ctx)


@router.get("/board/new", response_class=HTMLResponse)
async def new_form(
    request: Request,
    kind: str = "WTS",
    item: str = "",
    item_id: int | None = None,
    category: str = "",
    viewer: Viewer = Depends(require("board.post")),
    session: AsyncSession = Depends(get_session),
):
    kind = kind.upper() if kind.upper() in (*services.KINDS, "REQUEST") else "WTS"
    if item_id and not item:
        from mmgu.modules.archive.models import Item

        it = await session.get(Item, item_id)
        item = it.name if it else ""
    return render(
        request,
        "board/new.html",
        kind=kind,
        item=item,
        category=category if category in services.CATEGORIES else "",
        categories=services.CATEGORIES,
        default_server=services.default_server(),
        servers=hall.game.servers,
        tradeskills=hall.game.tradeskill_names,
        days=services.listing_days(),
    )


@router.post("/board/listings")
async def create_listing(
    request: Request, viewer: Viewer = Depends(require("board.post")), session: AsyncSession = Depends(get_session)
):
    form = await request.form()
    try:
        li = await services.create_listing(
            session,
            viewer,
            kind=form.get("kind") or "",
            item=form.get("item") or "",
            qty=form.get("qty") or 1,
            price=form.get("price"),
            server=form.get("server"),
            notes=form.get("notes"),
        )
    except services.BoardError as e:
        raise HTTPException(422, str(e)) from e
    await session.commit()
    return redirect(
        request,
        services.listing_url(li),
        f"Posted. Your {li.kind} listing stays up for {services.listing_days()} days.",
    )


@router.post("/board/requests")
async def create_request(
    request: Request, viewer: Viewer = Depends(require("board.post")), session: AsyncSession = Depends(get_session)
):
    form = await request.form()
    try:
        r = await services.create_request(
            session,
            viewer,
            category=form.get("category") or "",
            title=form.get("title") or "",
            details=form.get("details"),
            item=form.get("item"),
            tradeskill=form.get("tradeskill"),
        )
    except services.BoardError as e:
        raise HTTPException(422, str(e)) from e
    await session.commit()
    return redirect(request, services.request_url(r), "Posted. You'll get a notice when someone takes it on.")


@router.get("/board/listings/{listing_id}", response_class=HTMLResponse)
async def listing_page(
    request: Request,
    listing_id: int,
    viewer: Viewer = Depends(require("board.view")),
    session: AsyncSession = Depends(get_session),
):
    li = await _listing(session, listing_id)
    author = await session.get(Member, li.author_id) if li.author_id else None
    other = "WTS" if li.kind == "WTB" else "WTB"
    matches = await services.listings_for_item(session, item_id=li.item_id, name=li.title, kind=other, exclude_id=li.id)
    same = await services.listings_for_item(session, item_id=li.item_id, name=li.title, kind=li.kind, exclude_id=li.id)
    bank = await services.vault_holders(session, item_id=li.item_id, name=li.title) if viewer.can("vault.view") else []
    return render(
        request,
        "board/listing.html",
        li=li,
        author=author,
        matches=matches,
        same=same,
        bank=bank,
        can_edit=services.can_edit(viewer, li.author_id),
    )


@router.post("/board/listings/{listing_id}/{action}")
async def listing_action(
    request: Request,
    listing_id: int,
    action: str,
    viewer: Viewer = Depends(require("board.post")),
    session: AsyncSession = Depends(get_session),
):
    li = await _listing(session, listing_id)
    if action not in ("close", "sold", "renew"):
        raise HTTPException(404)
    try:
        await services.set_listing_status(session, viewer, li, action)
    except services.BoardError as e:
        raise HTTPException(422, str(e)) from e
    await session.commit()
    form = await request.form()
    back = form.get("next") or services.listing_url(li)
    if not str(back).startswith("/"):
        back = services.listing_url(li)
    msg = {
        "close": "Listing taken down.",
        "sold": "Marked as done. Enjoy the coin!" if li.kind == "WTS" else "Marked as done. Enjoy!",
        "renew": f"Renewed for another {services.listing_days()} days.",
    }[action]
    return redirect(request, back, msg)


@router.get("/board/requests/{request_id}", response_class=HTMLResponse)
async def request_page(
    request: Request,
    request_id: int,
    viewer: Viewer = Depends(require("board.view")),
    session: AsyncSession = Depends(get_session),
):
    r = await _req(session, request_id)
    author = await session.get(Member, r.author_id) if r.author_id else None
    helper = await session.get(Member, r.claimed_by) if r.claimed_by else None
    helped = await services.helped_counts(session, helper.id) if helper else (0, 0)
    sellers = (
        await services.listings_for_item(session, item_id=r.item_id, name=r.item_name, kind="WTS")
        if r.item_name
        else []
    )
    bank = (
        await services.vault_holders(session, item_id=r.item_id, name=r.item_name)
        if r.item_name and viewer.can("vault.view")
        else []
    )
    return render(
        request,
        "board/request.html",
        r=r,
        author=author,
        helper=helper,
        helped=helped,
        sellers=sellers,
        bank=bank,
        can_edit=services.can_edit(viewer, r.author_id),
    )


@router.post("/board/requests/{request_id}/{action}")
async def request_action(
    request: Request,
    request_id: int,
    action: str,
    viewer: Viewer = Depends(require("board.post")),
    session: AsyncSession = Depends(get_session),
):
    r = await _req(session, request_id)
    try:
        msg = await services.request_action(session, viewer, r, action)
    except services.BoardError as e:
        raise HTTPException(422, str(e)) from e
    await session.commit()
    form = await request.form()
    back = form.get("next") or services.request_url(r)
    if not str(back).startswith("/"):
        back = services.request_url(r)
    return redirect(request, back, msg)


# ----- JSON API ---------------------------------------------------------------------------------
def _abs(path: str) -> str:
    return hall.settings.base_url.rstrip("/") + path


@router.get("/api/board/listings")
async def api_listings(
    kind: str = "",
    q: str = "",
    viewer: Viewer = Depends(require("board.view")),
    session: AsyncSession = Depends(get_session),
):
    if kind and kind.upper() not in services.KINDS:
        raise HTTPException(422, "kind must be WTS or WTB")
    rows = await services.list_listings(session, kind=kind or None, q=q)
    return {
        "listings": [
            {
                "id": li.id,
                "kind": li.kind,
                "title": li.title,
                "item": li.title,
                "item_id": li.item_id,
                "qty": li.qty,
                "price": li.price,
                "server": li.server,
                "notes": li.notes,
                "author": m.display_name if m else None,
                "status": li.status,
                "expires_at": li.expires_at.isoformat() + "Z",
                "url": _abs(services.listing_url(li)),
            }
            for li, m in rows
        ]
    }


def _req_json(r: BoardRequest, a: Member | None, h: Member | None) -> dict:
    return {
        "id": r.id,
        "category": r.category,
        "title": r.title,
        "details": r.details,
        "item": r.item_name,
        "tradeskill": r.tradeskill,
        "author": a.display_name if a else None,
        "status": r.status,
        "claimed_by": h.display_name if h else None,
        "created_at": r.created_at.isoformat() + "Z",
        "url": _abs(services.request_url(r)),
    }


@router.get("/api/board/requests")
async def api_requests(
    status: str = "open",
    category: str = "",
    viewer: Viewer = Depends(require("board.view")),
    session: AsyncSession = Depends(get_session),
):
    if status not in (*services.REQUEST_STATUSES, "active", "all"):
        raise HTTPException(422, f"status must be one of: {', '.join(services.REQUEST_STATUSES)}, active, all")
    rows = await services.list_requests(session, status=None if status == "all" else status, category=category)
    return {"requests": [_req_json(r, a, h) for r, a, h in rows]}


@router.post("/api/board/requests")
async def api_create_request(
    request: Request, viewer: Viewer = Depends(require("board.post")), session: AsyncSession = Depends(get_session)
):
    try:
        body = await request.json()
    except ValueError as e:
        raise HTTPException(422, "Send a JSON body: {category, title, details}.") from e
    try:
        r = await services.create_request(
            session,
            viewer,
            category=body.get("category") or "Other",
            title=body.get("title") or "",
            details=body.get("details"),
            item=body.get("item"),
            tradeskill=body.get("tradeskill"),
            via="api",
        )
    except services.BoardError as e:
        raise HTTPException(422, str(e)) from e
    await session.commit()
    return {"id": r.id, "url": _abs(services.request_url(r))}
