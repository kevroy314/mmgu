"""Turn batches of game-ledger events (sent by the companion script) into drop reports and spawn times."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mmgu.addons.ledger_sync.models import ImportBatch, SeenEvent
from mmgu.core.audit import record
from mmgu.core.auth import Viewer
from mmgu.core.hall import hall
from mmgu.core.models import Member, Proposal
from mmgu.db import utcnow
from mmgu.modules.archive import services as archive

log = logging.getLogger(__name__)

MODULE_ID = "ledger_sync"
EVENT_TYPES = {"loot", "kill", "vendor_sale", "harvest"}
MAX_EVENTS = 1000


class LedgerError(ValueError):
    pass


def _text(v: Any, limit: int = 120) -> str | None:
    s = " ".join(str(v or "").split())
    return s[:limit] or None


def parse_time(value: Any) -> datetime:
    """ISO 8601 → naive UTC. Times without a zone are taken as UTC."""
    s = str(value or "").strip()
    if not s:
        raise LedgerError("missing time")
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise LedgerError(f"bad time {s[:40]!r}") from e
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return min(dt, utcnow())


def event_hash(character: str, server: str, ev: dict[str, Any]) -> str:
    """Stable id for an event. The companion sends ``raw.event_id`` (file + position) so two identical
    loots in the same second still count twice; without it we fall back to the event's own fields."""
    raw = ev.get("raw") if isinstance(ev.get("raw"), dict) else {}
    if raw.get("event_id"):
        basis: Any = ["id", character.lower(), server.lower(), str(raw["event_id"])]
    else:
        basis = [
            "ev",
            character.lower(),
            server.lower(),
            ev.get("type"),
            ev.get("at"),
            (ev.get("item") or "").lower(),
            (ev.get("creature") or "").lower(),
            (ev.get("zone") or "").lower(),
            ev.get("qty"),
        ]
    return hashlib.sha256(json.dumps(basis, default=str).encode()).hexdigest()


async def _pending_item_proposals(session: AsyncSession) -> dict[str, Proposal]:
    rows = (
        (
            await session.execute(
                select(Proposal)
                .where(Proposal.kind == "archive.item", Proposal.status == "pending")
                .order_by(Proposal.id.desc())
                .limit(2000)
            )
        )
        .scalars()
        .all()
    )
    out: dict[str, Proposal] = {}
    for p in rows:
        name = ((p.payload or {}).get("item") or {}).get("name")
        if name:
            out.setdefault(archive.name_key(name), p)
    return out


async def _report_kill(session: AsyncSession, viewer: Viewer, creature: str, died_at: datetime) -> bool:
    """Tell the Watch about a kill. Returns True when a tracked timer was updated."""
    if not hall.is_enabled("watch"):
        return False
    try:
        from mmgu.modules.watch.services import report_death_by_name
    except (ImportError, AttributeError):
        log.warning("watch module has no report_death_by_name; skipping ledger kills")
        return False
    return await report_death_by_name(session, viewer, creature, died_at, source="ledger") is not None


async def import_batch(session: AsyncSession, viewer: Viewer, body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise LedgerError("Send a JSON object with character, server and events.")
    character = _text(body.get("character"), 80)
    server = _text(body.get("server"), 80) or ""
    events = body.get("events")
    if not character:
        raise LedgerError("character is required.")
    if not isinstance(events, list):
        raise LedgerError("events must be a list.")
    if len(events) > MAX_EVENTS:
        raise LedgerError(f"Send at most {MAX_EVENTS} events per request.")

    counts = {
        "received": len(events),
        "duplicates": 0,
        "invalid": 0,
        "drops_recorded": 0,
        "items_proposed": 0,
        "unknown_items": 0,
        "kills": 0,
        "timers_updated": 0,
        "other": 0,
    }
    hashes = [event_hash(character, server, e) if isinstance(e, dict) else "" for e in events]
    seen = set(
        (await session.execute(select(SeenEvent.event_hash).where(SeenEvent.event_hash.in_([h for h in hashes if h]))))
        .scalars()
        .all()
    )
    propose_unknown = bool(hall.setting(MODULE_ID, "propose_unknown_items"))
    max_kill_age = timedelta(hours=int(hall.setting(MODULE_ID, "max_kill_age_hours") or 72))
    proposals: dict[str, Proposal] | None = None
    latest_kill: dict[str, tuple[str, datetime]] = {}

    for ev, h in zip(events, hashes, strict=True):
        if not h:
            counts["invalid"] += 1
            continue
        if h in seen:
            counts["duplicates"] += 1
            continue
        kind = str(ev.get("type") or "").lower()
        try:
            at = parse_time(ev.get("at"))
        except LedgerError:
            counts["invalid"] += 1
            continue
        if kind not in EVENT_TYPES:
            counts["invalid"] += 1
            continue
        seen.add(h)
        session.add(SeenEvent(event_hash=h, member_id=viewer.id))
        item_name = _text(ev.get("item"))
        creature = _text(ev.get("creature"))
        zone = _text(ev.get("zone"))

        if kind == "loot" and item_name:
            item = await archive.find_by_name(session, item_name)
            if item is not None:
                rep = await archive.add_drop(
                    session,
                    viewer,
                    item,
                    creature=creature,
                    zone=zone,
                    note=f"From {character}'s game log, {at:%Y-%m-%d %H:%M} UTC",
                    source="ledger",
                    via="api",
                )
                counts["drops_recorded"] += 1 if rep else 0
                continue
            counts["unknown_items"] += 1
            if not propose_unknown:
                continue
            if proposals is None:
                proposals = await _pending_item_proposals(session)
            key = archive.name_key(item_name)
            if key in proposals:
                continue
            p = Proposal(
                kind="archive.item",
                source="ledger",
                created_by=viewer.id,
                summary=f"New item looted: {item_name}",
                payload={"item": {"name": item_name}, "creature": creature, "zone": zone},
            )
            session.add(p)
            await session.flush()
            p.payload = {**p.payload, "review_url": f"/archive/new?proposal={p.id}"}
            proposals[key] = p
            counts["items_proposed"] += 1
        elif kind == "kill" and creature:
            counts["kills"] += 1
            if utcnow() - at <= max_kill_age:
                prev = latest_kill.get(creature.lower())
                if prev is None or at > prev[1]:
                    latest_kill[creature.lower()] = (creature, at)
        else:
            counts["other"] += 1

    for creature, at in latest_kill.values():
        if await _report_kill(session, viewer, creature, at):
            counts["timers_updated"] += 1

    batch = ImportBatch(
        member_id=viewer.id,
        character=character,
        server=server,
        received=counts["received"],
        duplicates=counts["duplicates"],
        counts=counts,
    )
    session.add(batch)
    await session.flush()
    new = counts["received"] - counts["duplicates"] - counts["invalid"]
    await record(
        session,
        "ledger.imported",
        actor_id=viewer.id,
        entity=batch,
        via="api",
        summary=f"Imported {new} game-log event(s) for {character}: {counts['drops_recorded']} drop(s), "
        f"{counts['items_proposed']} new item suggestion(s), {counts['timers_updated']} spawn timer(s)",
    )
    await hall.bus.emit("ledger.imported", actor_id=viewer.id, batch_id=batch.id)
    return {"batch_id": batch.id, **counts}


async def recent_imports(session: AsyncSession, limit: int = 50) -> list[tuple[ImportBatch, Member | None]]:
    rows = (
        await session.execute(
            select(ImportBatch, Member)
            .join(Member, Member.id == ImportBatch.member_id, isouter=True)
            .order_by(ImportBatch.created_at.desc())
            .limit(limit)
        )
    ).all()
    return [(b, m) for b, m in rows]


async def member_totals(session: AsyncSession) -> list[dict[str, Any]]:
    """Per member: how many uploads, the last one, and events imported (excluding repeats)."""
    rows = (
        await session.execute(
            select(
                Member,
                func.count(ImportBatch.id),
                func.max(ImportBatch.created_at),
                func.sum(ImportBatch.received - ImportBatch.duplicates),
            )
            .join(Member, Member.id == ImportBatch.member_id)
            .group_by(Member.id)
            .order_by(func.max(ImportBatch.created_at).desc())
        )
    ).all()
    return [{"member": m, "batches": n, "last": last, "events": int(ev or 0)} for m, n, last, ev in rows]
