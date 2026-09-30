"""Notice Board logic shared by the web pages, the Discord bot, the scheduler and the MCP server."""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any
from urllib.parse import quote_plus

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mmgu.core.audit import record, snapshot
from mmgu.core.auth import Viewer
from mmgu.core.hall import hall
from mmgu.core.models import Member
from mmgu.core.notify import notify
from mmgu.db import session_scope, utcnow
from mmgu.modules.board.models import BoardRequest, Listing

log = logging.getLogger(__name__)

KINDS = ("WTS", "WTB")
KIND_LABEL = {"WTS": "Selling", "WTB": "Buying"}
# One list, used by the web form, the /request command's choices and the API.
CATEGORIES = ["Crafting", "Port / travel", "Buffs", "Escort / help", "Corpse recovery", "Other"]
LISTING_STATUSES = ("open", "sold", "closed", "expired")
REQUEST_STATUSES = ("open", "claimed", "done", "closed")


class BoardError(ValueError):
    pass


def name_key(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower().replace("’", "'"))


def listing_days() -> int:
    try:
        return max(1, min(int(hall.setting("board", "listing_days") or 7), 90))
    except (TypeError, ValueError):
        return 7


def default_server() -> str:
    return hall.setting("roster", "default_server") or ""


def listing_url(listing: Listing) -> str:
    return f"/board/listings/{listing.id}"


def request_url(req: BoardRequest) -> str:
    return f"/board/requests/{req.id}"


_COIN = {"p": 1000, "pp": 1000, "g": 100, "gp": 100, "s": 10, "sp": 10, "c": 1, "cp": 1}


def price_value(price: str | None) -> int | None:
    """Rough value in copper for sorting ("5pp", "2p 5g", "1.5k"); None for "offer" or unreadable text."""
    if not price:
        return None
    total = 0.0
    found = False
    for num, k, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(k)?\s*(pp|gp|sp|cp|p|g|s|c)?\b", price.lower()):
        found = True
        v = float(num) * (1000 if k else 1)
        total += v * _COIN.get(unit or "pp", 1000)
    return int(total) if found else None


async def _resolve_item(session: AsyncSession, name: str) -> tuple[int | None, str]:
    from mmgu.modules.archive.services import find_by_name

    item = await find_by_name(session, name)
    return (item.id, item.name) if item else (None, name)


def _clean(text: Any, n: int) -> str:
    return re.sub(r"[ \t]+", " ", str(text or "").strip())[:n]


def can_edit(viewer: Viewer, author_id: int | None) -> bool:
    return viewer.can("board.moderate") or (author_id is not None and author_id == viewer.id)


# ----- listings ----------------------------------------------------------------------------------
async def create_listing(
    session: AsyncSession,
    viewer: Viewer,
    *,
    kind: str,
    item: str,
    qty: Any = 1,
    price: str | None = None,
    server: str | None = None,
    notes: str | None = None,
    via: str = "web",
) -> Listing:
    kind = (kind or "").upper()
    if kind not in KINDS:
        raise BoardError("Choose WTS (selling) or WTB (buying).")
    name = _clean(item, 120)
    if not name:
        raise BoardError("Name the item you're trading.")
    try:
        qty_n = int(str(qty or 1).strip())
    except ValueError:
        raise BoardError("Quantity must be a whole number.") from None
    if not 1 <= qty_n <= 100000:
        raise BoardError("Quantity must be at least 1.")
    item_id, name = await _resolve_item(session, name)
    listing = Listing(
        kind=kind,
        author_id=viewer.id,
        item_id=item_id,
        title=name,
        name_key=name_key(name),
        qty=qty_n,
        price=_clean(price, 60) or None,
        server=_clean(server if server is not None else default_server(), 40),
        notes=str(notes or "").strip()[:2000] or None,
        expires_at=utcnow() + timedelta(days=listing_days()),
    )
    session.add(listing)
    await session.flush()
    await record(
        session,
        "board.listing_posted",
        actor_id=viewer.id,
        entity=listing,
        via=via,
        summary=f"{kind} {qty_n} × {name}" + (f" for {listing.price}" if listing.price else ""),
    )
    await notify_matches(session, viewer, listing)
    await hall.bus.emit("board.posted", actor_id=viewer.id, kind="listing", id=listing.id)
    return listing


async def set_listing_status(
    session: AsyncSession, viewer: Viewer, listing: Listing, action: str, *, via: str = "web"
) -> None:
    """close | sold | renew"""
    if not can_edit(viewer, listing.author_id):
        raise BoardError("Only the person who posted it (or an officer) can change this listing.")
    before = snapshot(listing)
    if action == "renew":
        listing.status = "open"
        listing.closed_at = None
        listing.expires_at = utcnow() + timedelta(days=listing_days())
    elif action in ("close", "sold"):
        if listing.status != "open":
            raise BoardError(f"That listing is already {listing.status}.")
        listing.status = "sold" if action == "sold" else "closed"
        listing.closed_at = utcnow()
    else:
        raise BoardError("Unknown action.")
    await session.flush()
    await record(
        session,
        f"board.listing_{action}",
        actor_id=viewer.id,
        entity=listing,
        before=before,
        via=via,
        summary=f"{listing.kind} {listing.title}: {listing.status}",
    )
    await hall.bus.emit("board.updated", actor_id=viewer.id, kind="listing", id=listing.id)


async def expire_due(session: AsyncSession) -> int:
    now = utcnow()
    due = (
        (await session.execute(select(Listing).where(Listing.status == "open", Listing.expires_at <= now)))
        .scalars()
        .all()
    )
    for listing in due:
        listing.status = "expired"
        listing.closed_at = now
        await record(
            session,
            "board.listing_expired",
            actor_id=None,
            entity=listing,
            via="system",
            summary=f"{listing.kind} {listing.title} expired",
        )
        await notify(
            session,
            listing.author_id,
            f"Your {listing.kind} listing for {listing.title} expired. Renew it if it's still up for grabs.",
            listing_url(listing),
            dm=False,
        )
        await hall.bus.emit("board.updated", kind="listing", id=listing.id)
    return len(due)


async def expire_job() -> None:
    async with session_scope() as session:
        n = await expire_due(session)
    if n:
        log.info("expired %d board listings", n)


def _same_item(stmt, item_id: int | None, key: str):
    if item_id:
        return stmt.where(or_(Listing.item_id == item_id, Listing.name_key == key))
    return stmt.where(Listing.name_key == key)


async def listings_for_item(
    session: AsyncSession, *, item_id: int | None, name: str, kind: str | None = None, exclude_id: int | None = None
) -> list[tuple[Listing, Member | None]]:
    stmt = (
        select(Listing, Member)
        .join(Member, Member.id == Listing.author_id, isouter=True)
        .where(Listing.status == "open", Listing.expires_at > utcnow())
    )
    stmt = _same_item(stmt, item_id, name_key(name))
    if kind:
        stmt = stmt.where(Listing.kind == kind)
    if exclude_id:
        stmt = stmt.where(Listing.id != exclude_id)
    rows = [(li, m) for li, m in (await session.execute(stmt.order_by(Listing.created_at.desc()))).all()]
    rows.sort(key=lambda r: (price_value(r[0].price) is None, price_value(r[0].price) or 0))
    return rows


async def vault_holders(session: AsyncSession, *, item_id: int | None, name: str) -> list[tuple[Any, Any]]:
    """Bank mules holding this item, when the Vault is open. [(Holding, Character)]"""
    if not hall.is_enabled("vault"):
        return []
    from mmgu.modules.vault import services as vault_services

    return await vault_services.holders_of(session, item_id=item_id, name=name)


async def notify_matches(session: AsyncSession, viewer: Viewer, listing: Listing) -> dict[str, Any]:
    """Tell buyers and sellers of the same item about each other (and about the guild bank)."""
    other_kind = "WTS" if listing.kind == "WTB" else "WTB"
    others = await listings_for_item(
        session, item_id=listing.item_id, name=listing.title, kind=other_kind, exclude_id=listing.id
    )
    others = [(li, m) for li, m in others if li.author_id != listing.author_id]
    bank = await vault_holders(session, item_id=listing.item_id, name=listing.title) if listing.kind == "WTB" else []
    url = listing_url(listing)
    if others:
        who = ", ".join(
            f"{m.display_name if m else '?'}{' (' + li.price + ')' if li.price else ''}" for li, m in others[:5]
        )
        verb = "selling" if listing.kind == "WTB" else "looking to buy"
        await notify(session, listing.author_id, f"Match for {listing.title}: {len(others)} {verb} it: {who}.", url)
        for li, _m in others:
            action = "wants to buy" if listing.kind == "WTB" else "is selling"
            await notify(
                session,
                li.author_id,
                f"{viewer.name} {action} {listing.title}"
                + (f" ({listing.price})" if listing.price else "")
                + f", matching your {li.kind} listing.",
                url,
            )
    if bank:
        total = sum(h.qty for h, _ in bank)
        await notify(
            session,
            listing.author_id,
            f"The guild bank holds {total} × {listing.title} (on {', '.join(c.name for _, c in bank[:3])}). "
            "Members can request it from the Vault.",
            "/vault/requests/new?item=" + quote_plus(listing.title),
        )
    return {"listings": others, "bank": bank}


async def list_listings(
    session: AsyncSession,
    *,
    kind: str | None = None,
    q: str = "",
    server: str = "",
    author_id: int | None = None,
    status: str | None = "open",
    limit: int = 200,
) -> list[tuple[Listing, Member | None]]:
    stmt = select(Listing, Member).join(Member, Member.id == Listing.author_id, isouter=True)
    if status == "open":
        stmt = stmt.where(Listing.status == "open", Listing.expires_at > utcnow())
    elif status:
        stmt = stmt.where(Listing.status == status)
    if kind:
        stmt = stmt.where(Listing.kind == kind.upper())
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Listing.title.ilike(like), Listing.notes.ilike(like)))
    if server:
        stmt = stmt.where(Listing.server == server)
    if author_id:
        stmt = stmt.where(Listing.author_id == author_id)
    stmt = stmt.order_by(Listing.created_at.desc(), Listing.id.desc()).limit(limit)
    return [(li, m) for li, m in (await session.execute(stmt)).all()]


# ----- requests ----------------------------------------------------------------------------------
async def create_request(
    session: AsyncSession,
    viewer: Viewer,
    *,
    category: str,
    title: str,
    details: str | None = None,
    item: str | None = None,
    tradeskill: str | None = None,
    via: str = "web",
) -> BoardRequest:
    cat = next((c for c in CATEGORIES if c.lower() == str(category or "").strip().lower()), None)
    if cat is None:
        raise BoardError(f"Pick a category: {', '.join(CATEGORIES)}.")
    title = _clean(title, 140)
    if not title:
        raise BoardError("Give your request a short title, like 'Need a port to Rothold'.")
    item_id, item_name = (None, None)
    if _clean(item, 120):
        item_id, item_name = await _resolve_item(session, _clean(item, 120))
    skill = _clean(tradeskill, 60) or None
    req = BoardRequest(
        author_id=viewer.id,
        category=cat,
        title=title,
        details=str(details or "").strip()[:4000] or None,
        item_id=item_id,
        item_name=item_name,
        tradeskill=skill,
    )
    session.add(req)
    await session.flush()
    await record(session, "board.request_posted", actor_id=viewer.id, entity=req, via=via, summary=f"{cat}: {title}")
    await hall.bus.emit("board.posted", actor_id=viewer.id, kind="request", id=req.id)
    return req


async def request_action(
    session: AsyncSession, viewer: Viewer, req: BoardRequest, action: str, *, via: str = "web"
) -> str:
    """claim | unclaim | done | thank | close. Returns a message for the person who did it."""
    before = snapshot(req)
    author = req.author_id
    if action == "claim":
        if req.status != "open":
            raise BoardError("Someone already took this one." if req.status == "claimed" else f"It's {req.status}.")
        if author == viewer.id:
            raise BoardError("You can't claim your own request.")
        req.status, req.claimed_by, req.claimed_at = "claimed", viewer.id, utcnow()
        await notify(session, author, f"{viewer.name} is on it: {req.title}.", request_url(req))
        msg = f"You've claimed it. Let {'them' if author else 'the poster'} know when you're on your way."
    elif action == "unclaim":
        if req.status != "claimed" or not (req.claimed_by == viewer.id or viewer.can("board.moderate")):
            raise BoardError("Only whoever claimed it can let it go.")
        await notify(
            session, author, f"{viewer.name} can't help with {req.title} after all; it's open again.", request_url(req)
        )
        req.status, req.claimed_by, req.claimed_at = "open", None, None
        msg = "Released. It's open for someone else."
    elif action == "done":
        if req.status not in ("open", "claimed"):
            raise BoardError(f"It's already {req.status}.")
        if not (viewer.id in (req.claimed_by, author) or viewer.can("board.moderate")):
            raise BoardError("Claim it first, then mark it done.")
        if req.claimed_by is None and viewer.id != author:
            req.claimed_by, req.claimed_at = viewer.id, utcnow()
        req.status, req.done_at = "done", utcnow()
        if viewer.id != author:
            await notify(
                session, author, f"{viewer.name} marked “{req.title}” done. Say thanks on the board!", request_url(req)
            )
        msg = "Marked done. Nice work."
    elif action == "thank":
        if author != viewer.id:
            raise BoardError("Only the person who asked can thank the helper.")
        if req.status != "done" or not req.claimed_by:
            raise BoardError("You can thank someone once they've finished helping.")
        if req.thanked_at:
            raise BoardError("You've already said thanks.")
        req.thanked_at = utcnow()
        await notify(
            session, req.claimed_by, f"{viewer.name} thanked you for helping with {req.title}.", request_url(req)
        )
        msg = "Thanks sent."
    elif action == "close":
        if not can_edit(viewer, author):
            raise BoardError("Only the person who asked (or an officer) can close it.")
        if req.status in ("done", "closed"):
            raise BoardError(f"It's already {req.status}.")
        req.status = "closed"
        msg = "Closed."
    else:
        raise BoardError("Unknown action.")
    await session.flush()
    await record(
        session,
        f"board.request_{action}",
        actor_id=viewer.id,
        entity=req,
        before=before,
        via=via,
        summary=f"{req.title}: {action}",
    )
    await hall.bus.emit("board.updated", actor_id=viewer.id, kind="request", id=req.id)
    return msg


async def list_requests(
    session: AsyncSession,
    *,
    status: str | None = "open",
    category: str = "",
    q: str = "",
    author_id: int | None = None,
    claimed_by: int | None = None,
    limit: int = 200,
) -> list[tuple[BoardRequest, Member | None, Member | None]]:
    from sqlalchemy.orm import aliased

    Helper = aliased(Member)
    stmt = (
        select(BoardRequest, Member, Helper)
        .join(Member, Member.id == BoardRequest.author_id, isouter=True)
        .join(Helper, Helper.id == BoardRequest.claimed_by, isouter=True)
    )
    if status == "active":
        stmt = stmt.where(BoardRequest.status.in_(("open", "claimed")))
    elif status:
        stmt = stmt.where(BoardRequest.status == status)
    if category:
        stmt = stmt.where(BoardRequest.category == category)
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(BoardRequest.title.ilike(like), BoardRequest.details.ilike(like), BoardRequest.item_name.ilike(like))
        )
    if author_id and claimed_by:
        stmt = stmt.where(or_(BoardRequest.author_id == author_id, BoardRequest.claimed_by == claimed_by))
    elif author_id:
        stmt = stmt.where(BoardRequest.author_id == author_id)
    elif claimed_by:
        stmt = stmt.where(BoardRequest.claimed_by == claimed_by)
    stmt = stmt.order_by(BoardRequest.created_at.desc(), BoardRequest.id.desc()).limit(limit)
    return [(r, a, h) for r, a, h in (await session.execute(stmt)).all()]


async def helped_counts(session: AsyncSession, member_id: int) -> tuple[int, int]:
    """(requests they finished for others, how many of those people thanked them)"""
    row = (
        await session.execute(
            select(func.count(BoardRequest.id), func.count(BoardRequest.thanked_at)).where(
                BoardRequest.claimed_by == member_id,
                BoardRequest.status == "done",
                or_(BoardRequest.author_id.is_(None), BoardRequest.author_id != member_id),
            )
        )
    ).one()
    return int(row[0] or 0), int(row[1] or 0)


async def top_helpers(session: AsyncSession, limit: int = 5) -> list[tuple[str, int]]:
    rows = (
        await session.execute(
            select(Member.display_name, func.count(BoardRequest.id))
            .join(BoardRequest, BoardRequest.claimed_by == Member.id)
            .where(BoardRequest.status == "done")
            .group_by(Member.id)
            .order_by(func.count(BoardRequest.id).desc())
            .limit(limit)
        )
    ).all()
    return [(n, c) for n, c in rows]


async def counts(session: AsyncSession) -> dict[str, int]:
    now = utcnow()
    kinds = dict(
        (
            await session.execute(
                select(Listing.kind, func.count())
                .where(Listing.status == "open", Listing.expires_at > now)
                .group_by(Listing.kind)
            )
        ).all()
    )
    reqs = (
        await session.execute(
            select(func.count()).select_from(BoardRequest).where(BoardRequest.status.in_(("open", "claimed")))
        )
    ).scalar_one()
    return {"WTS": kinds.get("WTS", 0), "WTB": kinds.get("WTB", 0), "requests": reqs}
