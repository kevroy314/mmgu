"""Vault logic shared by the web pages, the Discord bot, other modules and the MCP server.

Holdings only change through transactions (deposit, withdraw, adjust, import), so every change
is in the history and the audit log.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mmgu.core.audit import record, snapshot
from mmgu.core.auth import Viewer
from mmgu.core.hall import hall
from mmgu.core.models import Character, Member, Proposal, Upload
from mmgu.core.notify import notify
from mmgu.core.uploads import upload_bytes
from mmgu.db import utcnow
from mmgu.modules.vault.models import Holding, Request, Transaction

log = logging.getLogger(__name__)

TX_KINDS = ("deposit", "withdraw", "adjust", "import")
REQUEST_STATUSES = ("pending", "approved", "denied", "fulfilled", "cancelled")
OPEN_STATUSES = ("pending", "approved")
MAX_QTY = 1_000_000


class VaultError(ValueError):
    pass


def name_key(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower().replace("’", "'"))


def clean_name(name: str | None) -> str:
    name = re.sub(r"\s+", " ", (name or "").strip())
    if not name:
        raise VaultError("Name the item.")
    if len(name) > 120:
        raise VaultError("Item names can be up to 120 characters.")
    return name


def clean_qty(qty: Any, *, allow_zero: bool = False) -> int:
    try:
        n = int(str(qty).strip())
    except (TypeError, ValueError):
        raise VaultError("Quantity must be a whole number.") from None
    if n < 0 or (n == 0 and not allow_zero) or n > MAX_QTY:
        raise VaultError("Quantity must be at least 1." if not allow_zero else "Quantity can't be negative.")
    return n


async def resolve_item(session: AsyncSession, name: str) -> tuple[int | None, str]:
    """(archive item id or None, the name to store). Uses the Archive's spelling when it knows the item."""
    name = clean_name(name)
    from mmgu.modules.archive.services import find_by_name

    item = await find_by_name(session, name)
    if item is not None:
        return item.id, item.name
    known = (
        await session.execute(select(Holding.name).where(Holding.name_key == name_key(name)).limit(1))
    ).scalar_one_or_none()
    return None, known or name


# ----- reads -------------------------------------------------------------------------------------
async def mules(session: AsyncSession) -> list[Character]:
    q = select(Character).where(Character.is_bank_mule.is_(True), Character.status == "active").order_by(Character.name)
    return list((await session.execute(q)).scalars().all())


async def get_mule(session: AsyncSession, character_id: int) -> Character:
    ch = await session.get(Character, character_id)
    if ch is None or not ch.is_bank_mule:
        raise VaultError("That character isn't a guild bank mule. Mark it as one on the Muster Roll first.")
    return ch


async def holdings(
    session: AsyncSession, *, q: str = "", character_id: int | None = None, item_id: int | None = None
) -> list[tuple[Holding, Character]]:
    stmt = (
        select(Holding, Character)
        .join(Character, Character.id == Holding.character_id)
        .where(Holding.qty > 0, Character.is_bank_mule.is_(True), Character.status == "active")
    )
    if q.strip():
        stmt = stmt.where(or_(Holding.name.ilike(f"%{q.strip()}%"), Holding.note.ilike(f"%{q.strip()}%")))
    if character_id:
        stmt = stmt.where(Holding.character_id == character_id)
    if item_id:
        stmt = stmt.where(Holding.item_id == item_id)
    stmt = stmt.order_by(Holding.name, Character.name)
    return [(h, c) for h, c in (await session.execute(stmt)).all()]


def totals(rows: list[tuple[Holding, Character]]) -> list[dict[str, Any]]:
    """Sum holdings per item across mules."""
    out: dict[str, dict[str, Any]] = {}
    for h, c in rows:
        t = out.setdefault(h.name_key, {"name": h.name, "item_id": h.item_id, "qty": 0, "mules": []})
        t["qty"] += h.qty
        t["item_id"] = t["item_id"] or h.item_id
        t["mules"].append((c, h.qty))
    return sorted(out.values(), key=lambda t: t["name"].lower())


async def held_qty(session: AsyncSession, *, item_id: int | None = None, name: str | None = None) -> int:
    return sum(h.qty for h, _ in await holders_of(session, item_id=item_id, name=name))


async def holders_of(
    session: AsyncSession, *, item_id: int | None = None, name: str | None = None
) -> list[tuple[Holding, Character]]:
    stmt = (
        select(Holding, Character)
        .join(Character, Character.id == Holding.character_id)
        .where(Holding.qty > 0, Character.is_bank_mule.is_(True), Character.status == "active")
    )
    if item_id and name:
        stmt = stmt.where(or_(Holding.item_id == item_id, Holding.name_key == name_key(name)))
    elif item_id:
        stmt = stmt.where(Holding.item_id == item_id)
    elif name:
        stmt = stmt.where(Holding.name_key == name_key(name))
    else:
        return []
    return [(h, c) for h, c in (await session.execute(stmt.order_by(Holding.qty.desc()))).all()]


async def history(
    session: AsyncSession, *, character_id: int | None = None, q: str = "", limit: int = 100
) -> list[tuple[Transaction, Character, Member | None]]:
    stmt = (
        select(Transaction, Character, Member)
        .join(Character, Character.id == Transaction.character_id)
        .join(Member, Member.id == Transaction.actor_id, isouter=True)
    )
    if character_id:
        stmt = stmt.where(Transaction.character_id == character_id)
    if q.strip():
        stmt = stmt.where(Transaction.name.ilike(f"%{q.strip()}%"))
    stmt = stmt.order_by(Transaction.created_at.desc(), Transaction.id.desc()).limit(limit)
    return [(t, c, m) for t, c, m in (await session.execute(stmt)).all()]


async def mule_summaries(session: AsyncSession) -> list[dict[str, Any]]:
    """Every mule with how many different items and total pieces it holds."""
    rows = (
        await session.execute(
            select(Holding.character_id, func.count(Holding.id), func.coalesce(func.sum(Holding.qty), 0))
            .where(Holding.qty > 0)
            .group_by(Holding.character_id)
        )
    ).all()
    counts = {cid: (n, total) for cid, n, total in rows}
    out = []
    for m in await mules(session):
        n, total = counts.get(m.id, (0, 0))
        out.append({"character": m, "items": n, "pieces": int(total)})
    return out


# ----- writes: holdings ------------------------------------------------------------------------
async def _holding(session: AsyncSession, character: Character, name: str) -> Holding | None:
    return (
        await session.execute(
            select(Holding).where(Holding.character_id == character.id, Holding.name_key == name_key(name))
        )
    ).scalar_one_or_none()


async def change(
    session: AsyncSession,
    viewer: Viewer | None,
    character: Character,
    name: str,
    *,
    kind: str,
    qty: int,
    note: str | None = None,
    slot: str | None = None,
    via: str = "web",
    request_id: int | None = None,
    emit: bool = True,
) -> Transaction:
    """Deposit, withdraw or adjust (set the count to ``qty``) one item on one mule."""
    if kind not in TX_KINDS:
        raise VaultError(f"Unknown transaction type {kind}.")
    if not character.is_bank_mule:
        raise VaultError(f"{character.name} isn't a guild bank mule.")
    item_id, name = await resolve_item(session, name)
    qty = clean_qty(qty, allow_zero=kind in ("adjust", "import"))
    h = await _holding(session, character, name)
    have = h.qty if h else 0
    if kind == "deposit":
        new = have + qty
    elif kind == "withdraw":
        if qty > have:
            raise VaultError(
                f"{character.name} only has {have} × {name}." if have else f"{character.name} doesn't hold any {name}."
            )
        new = have - qty
    else:
        new = qty
    if h is None:
        h = Holding(character_id=character.id, name=name, name_key=name_key(name), qty=0)
        session.add(h)
    h.qty = new
    h.item_id = item_id or h.item_id
    h.updated_by = viewer.id if viewer else None
    if slot is not None:
        h.slot = slot.strip()[:60] or None
    note = (note or "").strip()[:300] or None
    tx = Transaction(
        character_id=character.id,
        item_id=h.item_id,
        name=name,
        kind=kind,
        delta=new - have,
        qty_after=new,
        note=note,
        actor_id=viewer.id if viewer else None,
        request_id=request_id,
        via=via,
    )
    session.add(tx)
    await session.flush()
    verb = {"deposit": "Deposited", "withdraw": "Withdrew", "adjust": "Counted", "import": "Imported"}[kind]
    await record(
        session,
        f"vault.{kind}",
        actor_id=viewer.id if viewer else None,
        entity=tx,
        via=via,
        summary=f"{verb} {abs(new - have) if kind != 'adjust' else new} × {name} on {character.name}"
        + (f" ({note})" if note else ""),
    )
    if emit:
        await hall.bus.emit("vault.holdings_changed", actor_id=viewer.id if viewer else None, character_id=character.id)
    return tx


async def update_holding_details(
    session: AsyncSession, viewer: Viewer, holding: Holding, *, slot: str | None, note: str | None
) -> None:
    before = snapshot(holding)
    holding.slot = (slot or "").strip()[:60] or None
    holding.note = (note or "").strip()[:200] or None
    holding.updated_by = viewer.id
    await record(
        session, "vault.holding_noted", actor_id=viewer.id, entity=holding, before=before, summary=holding.name
    )
    await hall.bus.emit("vault.holdings_changed", actor_id=viewer.id, character_id=holding.character_id)


# ----- bulk "this is what the mule holds now" ---------------------------------------------------
_QTY_AFTER = re.compile(r"^(?P<name>.+?)\s*(?:[x×*]\s*(?P<q1>\d+)|\((?P<q2>\d+)\)|-\s*(?P<q3>\d+))\s*$", re.I)
_QTY_BEFORE = re.compile(r"^(?P<q>\d+)\s*[x×*]?\s+(?P<name>.+)$", re.I)
_BANK_LOCATIONS = ("bank", "sharedbank", "shared bank")


@dataclass
class Parsed:
    lines: list[dict[str, Any]] = field(default_factory=list)  # [{"name", "qty"}], merged by name
    skipped: list[str] = field(default_factory=list)
    format: str = "list"


def parse_lines(text: str, *, bank_only: bool = False) -> Parsed:
    """Read pasted inventory lines.

    Understands ``Rusty Scimitar x3``, ``Rusty Scimitar (3)``, ``3 Bone Chips``, ``3x Bone Chips``, a bare
    name (qty 1), and EverQuest-style ``/outputfile inventory`` tab-separated rows
    (``Location  Name  ID  Count  Slots``). With ``bank_only`` only bank and shared-bank rows of the
    tab-separated format are kept.
    """
    out = Parsed()
    merged: dict[str, dict[str, Any]] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name: str | None = None
        qty = 1
        if "\t" in raw:
            cols = [c.strip() for c in raw.split("\t")]
            if [c.lower() for c in cols[:2]] == ["location", "name"]:
                out.format = "outputfile"
                continue
            if len(cols) >= 4:
                out.format = "outputfile"
                loc, nm, cnt = cols[0], cols[1], cols[3]
                if not nm or nm.lower() == "empty":
                    continue
                if bank_only and not loc.lower().replace("-", " ").startswith(_BANK_LOCATIONS):
                    continue
                name = nm
                qty = int(cnt) if cnt.isdigit() and int(cnt) > 0 else 1
            else:
                line = " ".join(c for c in cols if c)
        if name is None:
            m = _QTY_AFTER.match(line)
            if m:
                name = m.group("name")
                qty = int(m.group("q1") or m.group("q2") or m.group("q3"))
            else:
                m = _QTY_BEFORE.match(line)
                if m:
                    name, qty = m.group("name"), int(m.group("q"))
                else:
                    name = line
        name = re.sub(r"\s+", " ", (name or "").strip(" -•*\t"))
        if not name or len(name) > 120 or qty <= 0 or qty > MAX_QTY:
            out.skipped.append(raw.strip())
            continue
        k = name_key(name)
        if k in merged:
            merged[k]["qty"] += qty
        else:
            merged[k] = {"name": name, "qty": qty}
    out.lines = list(merged.values())
    return out


def clean_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalise ``[{"name", "qty"}]`` from a reader, API or hidden form field."""
    merged: dict[str, dict[str, Any]] = {}
    for ln in lines or []:
        try:
            name = clean_name(str(ln.get("name") or ""))
            qty = int(ln.get("qty") or 1)
        except (VaultError, TypeError, ValueError, AttributeError):
            continue
        if qty <= 0:
            continue
        k = name_key(name)
        if k in merged:
            merged[k]["qty"] += qty
        else:
            merged[k] = {"name": name, "qty": min(qty, MAX_QTY)}
    return list(merged.values())


async def diff(session: AsyncSession, character: Character, lines: list[dict[str, Any]]) -> dict[str, Any]:
    """What would change if ``lines`` became this mule's whole inventory."""
    current = {h.name_key: h for h, _ in await holdings(session, character_id=character.id)}
    added, changed, same = [], [], []
    seen = set()
    for ln in clean_lines(lines):
        k = name_key(ln["name"])
        seen.add(k)
        h = current.get(k)
        if h is None:
            item_id, canon = await resolve_item(session, ln["name"])
            added.append({"name": canon, "qty": ln["qty"], "item_id": item_id})
        elif h.qty != ln["qty"]:
            changed.append({"name": h.name, "old": h.qty, "qty": ln["qty"], "item_id": h.item_id})
        else:
            same.append({"name": h.name, "qty": h.qty, "item_id": h.item_id})
    removed = [{"name": h.name, "qty": h.qty, "item_id": h.item_id} for k, h in current.items() if k not in seen]
    return {
        "added": added,
        "changed": changed,
        "removed": removed,
        "same": same,
        "has_changes": bool(added or changed or removed),
    }


async def set_inventory(
    session: AsyncSession,
    viewer: Viewer,
    character: Character,
    lines: list[dict[str, Any]],
    *,
    note: str | None = None,
    via: str = "web",
) -> dict[str, Any]:
    """Replace a mule's holdings with ``lines``; one import transaction per changed item."""
    d = await diff(session, character, lines)
    note = (note or "").strip() or "inventory update"
    for row in d["added"] + d["changed"]:
        await change(
            session, viewer, character, row["name"], kind="import", qty=row["qty"], note=note, via=via, emit=False
        )
    for row in d["removed"]:
        await change(session, viewer, character, row["name"], kind="import", qty=0, note=note, via=via, emit=False)
    await record(
        session,
        "vault.inventory_set",
        actor_id=viewer.id,
        entity=character,
        via=via,
        summary=f"{character.name}: {len(d['added'])} added, {len(d['changed'])} changed, {len(d['removed'])} removed",
    )
    await hall.bus.emit("vault.holdings_changed", actor_id=viewer.id, character_id=character.id)
    return d


# ----- screenshots ----------------------------------------------------------------------------
def reader_available() -> bool:
    return bool(hall.ext.get("vault.readers", hall.enabled_ids))


async def read_screenshot(session: AsyncSession, viewer: Viewer, upload: Upload, character: Character) -> Proposal:
    """Ask the ``vault.readers`` add-ons to read a bank screenshot; the result waits for review."""
    lines: list[dict] | None = None
    error = None
    source = "manual"
    for c in hall.ext.get("vault.readers", hall.enabled_ids):
        try:
            lines = await c.fn(upload_bytes(upload))
        except Exception as e:  # noqa: BLE001
            log.exception("vault reader %s failed", c.module)
            error = f"{c.module}: {e}"
        if lines is not None:
            source = c.module
            break
    p = Proposal(
        kind="vault.holdings",
        source=source,
        summary=f"Bank screenshot for {character.name}",
        upload_id=upload.id,
        created_by=viewer.id,
        payload={"character_id": character.id, "lines": clean_lines(lines or [])},
        error=error,
    )
    session.add(p)
    await session.flush()
    p.payload = {**p.payload, "review_url": f"/vault/mules/{character.id}/update?proposal={p.id}"}
    await record(session, "vault.screenshot_read", actor_id=viewer.id, entity=p, summary=p.summary)
    return p


# ----- requests --------------------------------------------------------------------------------
def request_url(req: Request) -> str:
    return f"/vault/requests/{req.id}"


async def create_request(
    session: AsyncSession,
    viewer: Viewer,
    *,
    name: str,
    qty: Any = 1,
    reason: str | None = None,
    item_id: int | None = None,
    via: str = "web",
) -> Request:
    if item_id and not name:
        from mmgu.modules.archive.models import Item

        item = await session.get(Item, item_id)
        name = item.name if item else ""
    resolved_id, name = await resolve_item(session, name)
    req = Request(
        requester_id=viewer.id,
        item_id=resolved_id,
        name=name,
        qty=clean_qty(qty or 1),
        reason=(reason or "").strip()[:1000] or None,
    )
    session.add(req)
    await session.flush()
    await record(
        session,
        "vault.request_created",
        actor_id=viewer.id,
        entity=req,
        via=via,
        summary=f"{viewer.name} requested {req.qty} × {req.name}",
    )
    await hall.bus.emit("vault.request_created", actor_id=viewer.id, request_id=req.id)
    return req


async def _set_status(
    session: AsyncSession, viewer: Viewer, req: Request, status: str, *, note: str | None, via: str
) -> None:
    before = snapshot(req)
    req.status = status
    req.decided_by = viewer.id
    req.decided_at = utcnow()
    if note is not None:
        req.decision_note = note.strip()[:300] or None
    await session.flush()
    await record(
        session,
        f"vault.request_{status}",
        actor_id=viewer.id,
        entity=req,
        before=before,
        via=via,
        summary=f"Request for {req.qty} × {req.name}: {status}",
    )
    await hall.bus.emit("vault.request_updated", actor_id=viewer.id, request_id=req.id)


async def approve_request(
    session: AsyncSession, viewer: Viewer, req: Request, note: str | None = None, via: str = "web"
) -> None:
    if req.status != "pending":
        raise VaultError(f"That request is already {req.status}.")
    await _set_status(session, viewer, req, "approved", note=note, via=via)
    await notify(
        session,
        req.requester_id,
        f"Your bank request for {req.qty} × {req.name} was approved by {viewer.name}. A banker will hand it over."
        + (f" Note: {req.decision_note}" if req.decision_note else ""),
        request_url(req),
    )


async def deny_request(
    session: AsyncSession, viewer: Viewer, req: Request, note: str | None = None, via: str = "web"
) -> None:
    if req.status not in OPEN_STATUSES:
        raise VaultError(f"That request is already {req.status}.")
    await _set_status(session, viewer, req, "denied", note=note, via=via)
    await notify(
        session,
        req.requester_id,
        f"Your bank request for {req.qty} × {req.name} was declined by {viewer.name}."
        + (f" Reason: {req.decision_note}" if req.decision_note else ""),
        request_url(req),
    )


async def fulfil_request(
    session: AsyncSession,
    viewer: Viewer,
    req: Request,
    character: Character,
    *,
    qty: int | None = None,
    note: str | None = None,
    via: str = "web",
) -> Transaction:
    """Hand the item over: withdraw it from ``character`` and close the request."""
    if req.status not in OPEN_STATUSES:
        raise VaultError(f"That request is already {req.status}.")
    requester = await session.get(Member, req.requester_id) if req.requester_id else None
    tx = await change(
        session,
        viewer,
        character,
        req.name,
        kind="withdraw",
        qty=qty or req.qty,
        note=f"request #{req.id}" + (f" for {requester.display_name}" if requester else ""),
        via=via,
        request_id=req.id,
    )
    req.fulfilled_from = character.id
    await _set_status(session, viewer, req, "fulfilled", note=note, via=via)
    await notify(
        session,
        req.requester_id,
        f"{viewer.name} is handing you {abs(tx.delta)} × {req.name} from {character.name}. "
        "Meet up in game to collect it.",
        request_url(req),
    )
    return tx


async def cancel_request(session: AsyncSession, viewer: Viewer, req: Request, via: str = "web") -> None:
    if req.requester_id != viewer.id and not viewer.can("vault.manage"):
        raise VaultError("Only the person who asked can cancel a request.")
    if req.status not in OPEN_STATUSES:
        raise VaultError(f"That request is already {req.status}.")
    await _set_status(session, viewer, req, "cancelled", note=None, via=via)


async def list_requests(
    session: AsyncSession, *, status: str | None = None, requester_id: int | None = None, limit: int = 200
) -> list[tuple[Request, Member | None]]:
    stmt = select(Request, Member).join(Member, Member.id == Request.requester_id, isouter=True)
    if status == "open":
        stmt = stmt.where(Request.status.in_(OPEN_STATUSES))
    elif status:
        stmt = stmt.where(Request.status == status)
    if requester_id:
        stmt = stmt.where(Request.requester_id == requester_id)
    stmt = stmt.order_by(Request.created_at.desc(), Request.id.desc()).limit(limit)
    return [(r, m) for r, m in (await session.execute(stmt)).all()]


async def request_counts(session: AsyncSession) -> dict[str, int]:
    rows = (await session.execute(select(Request.status, func.count()).group_by(Request.status))).all()
    return {s: n for s, n in rows}


async def suggest_mule(session: AsyncSession, req: Request) -> Character | None:
    """The mule holding the most of the requested item, if any holds enough."""
    rows = await holders_of(session, item_id=req.item_id, name=req.name)
    for h, c in rows:
        if h.qty >= req.qty:
            return c
    return rows[0][1] if rows else None
