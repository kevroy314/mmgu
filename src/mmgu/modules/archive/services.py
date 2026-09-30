"""Archive logic shared by the web pages, the Discord bot, add-ons and the MCP server."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from rapidfuzz import fuzz, process
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from mmgu.core.audit import record, snapshot
from mmgu.core.auth import Viewer
from mmgu.core.hall import hall
from mmgu.core.models import Member
from mmgu.db import Base
from mmgu.modules.archive.models import DropReport, Item, ItemImage, ItemRevision

FTS_TABLE = "archive_items_fts"


class ArchiveError(ValueError):
    pass


class DuplicateItem(ArchiveError):
    def __init__(self, item: Item):
        super().__init__(f"{item.name} is already in the Archive.")
        self.item = item


def name_key(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower().replace("’", "'"))


# ----- turning loose input (forms, AI output, API) into clean fields -------------------------
def _num(v: Any, kind=int) -> Any:
    if v is None or v == "":
        return None
    try:
        s = str(v).strip().replace("+", "")
        return kind(float(s)) if kind is int else kind(s)
    except (TypeError, ValueError):
        return None


def _upper_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        v = re.split(r"[,\s/]+", v)
    return [str(x).strip().upper() for x in v if str(x).strip()]


def clean_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Normalise item fields from any source against the game pack. Unknown extras go to attributes."""
    g = hall.game
    name = re.sub(r"\s+", " ", str(data.get("name") or "").strip())
    if not name or len(name) > 120:
        raise ArchiveError("Every item needs a name (up to 120 characters).")
    flag_keys = {f["key"] for f in g.flags}
    flag_by_label = {f["label"].upper(): f["key"] for f in g.flags}
    flags = []
    for f in data.get("flags") or []:
        s = str(f).strip()
        k = flag_by_label.get(s.upper()) or re.sub(r"[^a-z]", "", s.lower()).removesuffix("item")
        if k in flag_keys and k not in flags:
            flags.append(k)
    slots = [s for s in _upper_list(data.get("slots")) if s in g.slots or not g.slots]
    classes_raw = data.get("classes") or []
    if isinstance(classes_raw, str):
        classes_raw = re.split(r"[,\s/]+", classes_raw)
    classes: list[str] = []
    for c in classes_raw:
        c = str(c).strip()
        if not c:
            continue
        if c.upper() == "ALL":
            classes = ["ALL"]
            break
        canon = g.class_from_any(c)
        if canon and canon not in classes:
            classes.append(canon)
    races_raw = data.get("races") or []
    if isinstance(races_raw, str):
        races_raw = [r.strip() for r in re.split(r"[,/]+", races_raw)]
    races = (
        ["ALL"]
        if any(str(r).upper() == "ALL" for r in races_raw)
        else [str(r).strip() for r in races_raw if str(r).strip()]
    )
    stat_keys = {s["key"] for s in g.stats}
    resist_keys = {s["key"] for s in g.resists}
    stats = {k: _num(v) for k, v in (data.get("stats") or {}).items() if k in stat_keys and _num(v)}
    resists = {k: _num(v) for k, v in (data.get("resists") or {}).items() if k in resist_keys and _num(v)}
    attributes = dict(data.get("attributes") or {})
    for k, v in (data.get("stats") or {}).items():
        if k not in stat_keys and _num(v):
            attributes[f"stat:{k}"] = _num(v)
    skill = (data.get("skill") or "").strip() or None
    if skill:
        for s in g.skills:
            if skill.upper() in (s["key"].upper(), s["label"].upper()):
                skill = s["key"]
                break
    size = (data.get("size") or "").strip().upper() or None
    effects = [str(e).strip() for e in (data.get("effects") or []) if str(e).strip()]
    return {
        "name": name,
        "item_type": (data.get("item_type") or "").strip() or None,
        "slots": slots,
        "flags": flags,
        "classes": classes,
        "races": races,
        "skill": skill,
        "damage": _num(data.get("damage")),
        "delay": _num(data.get("delay")),
        "ac": _num(data.get("ac")),
        "weight": _num(data.get("weight"), float),
        "size": size,
        "stats": stats,
        "resists": resists,
        "effects": effects[:10],
        "attributes": attributes,
        "description": (data.get("description") or "").strip() or None,
        "raw_text": (data.get("raw_text") or "").strip() or None,
        "patch_seen": (data.get("patch_seen") or "").strip() or None,
    }


def fields_from_form(form) -> dict[str, Any]:
    g = hall.game
    stats = {s["key"]: form.get(f"stat_{s['key']}") for s in g.stats}
    resists = {s["key"]: form.get(f"res_{s['key']}") for s in g.resists}
    effects = [e for e in (form.get("effects") or "").splitlines() if e.strip()]
    return {
        "name": form.get("name"),
        "item_type": form.get("item_type"),
        "slots": form.getlist("slots"),
        "flags": form.getlist("flags"),
        "classes": form.getlist("classes") or (["ALL"] if form.get("all_classes") else []),
        "races": form.get("races") or "ALL",
        "skill": form.get("skill"),
        "damage": form.get("damage"),
        "delay": form.get("delay"),
        "ac": form.get("ac"),
        "weight": form.get("weight"),
        "size": form.get("size"),
        "stats": stats,
        "resists": resists,
        "effects": effects,
        "description": form.get("description"),
        "raw_text": form.get("raw_text"),
        "patch_seen": form.get("patch_seen"),
    }


# ----- search index --------------------------------------------------------------------------
def _search_body(item: Item) -> str:
    g = hall.game
    parts = [
        item.item_type or "",
        " ".join(item.slots or []),
        " ".join(item.classes or []),
        " ".join(g.class_abbr(c) for c in item.classes or [] if c != "ALL"),
        g.skill_label(item.skill),
        " ".join(g.stat_label(k) for k in (item.stats or {})),
        " ".join(item.effects or []),
        item.description or "",
    ]
    return " ".join(p for p in parts if p)


async def ensure_fts(session: AsyncSession) -> None:
    if not hall.settings.is_sqlite:
        return
    await session.execute(
        text(f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5(name, body, drops, tokenize='trigram')")
    )


async def reindex(session: AsyncSession, item: Item) -> None:
    if not hall.settings.is_sqlite:
        return
    drops = (
        await session.execute(select(DropReport.creature, DropReport.zone).where(DropReport.item_id == item.id))
    ).all()
    drop_text = " ".join(f"{c or ''} {z or ''}" for c, z in drops)
    await session.execute(text(f"DELETE FROM {FTS_TABLE} WHERE rowid = :id"), {"id": item.id})
    await session.execute(
        text(f"INSERT INTO {FTS_TABLE}(rowid, name, body, drops) VALUES (:id, :n, :b, :d)"),
        {"id": item.id, "n": item.name, "b": _search_body(item), "d": drop_text},
    )


async def reindex_all(session: AsyncSession) -> int:
    if not hall.settings.is_sqlite:
        return 0
    await ensure_fts(session)
    await session.execute(text(f"DELETE FROM {FTS_TABLE}"))
    items = (await session.execute(select(Item))).scalars().all()
    for it in items:
        await reindex(session, it)
    return len(items)


async def _text_matches(session: AsyncSession, q: str) -> list[int] | None:
    q = q.strip()
    if not q:
        return None
    if hall.settings.is_sqlite and len(q) >= 3:
        safe = '"' + q.replace('"', '""') + '"'
        rows = await session.execute(
            text(f"SELECT rowid FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH :q ORDER BY rank LIMIT 500"), {"q": safe}
        )
        return [r[0] for r in rows]
    like = f"%{q}%"
    rows = await session.execute(
        select(Item.id).where(or_(Item.name.ilike(like), Item.description.ilike(like))).limit(500)
    )
    return [r[0] for r in rows]


async def search_items(
    session: AsyncSession,
    *,
    q: str = "",
    slot: str = "",
    cls: str = "",
    stat: str = "",
    item_type: str = "",
    flag: str = "",
    zone: str = "",
    creature: str = "",
    include_stale: bool = False,
    limit: int = 60,
) -> list[Item]:
    ids = await _text_matches(session, q)
    stmt = select(Item)
    if ids is not None:
        if not ids:
            return []
        stmt = stmt.where(Item.id.in_(ids))
    if not include_stale:
        stmt = stmt.where(Item.status == "active")
    if item_type:
        stmt = stmt.where(Item.item_type == item_type)
    if zone or creature:
        sub = select(DropReport.item_id)
        if zone:
            sub = sub.where(DropReport.zone.ilike(zone))
        if creature:
            sub = sub.where(DropReport.creature.ilike(f"%{creature}%"))
        stmt = stmt.where(Item.id.in_(sub))
    items = list((await session.execute(stmt.order_by(Item.updated_at.desc()).limit(2000))).scalars().all())
    if slot:
        items = [i for i in items if slot.upper() in (i.slots or [])]
    if cls:
        items = [i for i in items if "ALL" in (i.classes or []) or cls in (i.classes or [])]
    if stat:
        items = [i for i in items if (i.stats or {}).get(stat) or (i.resists or {}).get(stat)]
        items.sort(key=lambda i: -((i.stats or {}).get(stat) or (i.resists or {}).get(stat) or 0))
    if flag:
        items = [i for i in items if flag in (i.flags or [])]
    if ids is not None and not stat:
        order = {v: n for n, v in enumerate(ids)}
        items.sort(key=lambda i: order.get(i.id, 9999))
    return items[:limit]


async def find_by_name(session: AsyncSession, name: str) -> Item | None:
    return (await session.execute(select(Item).where(Item.name_key == name_key(name)))).scalar_one_or_none()


async def suggest_names(session: AsyncSession, q: str, limit: int = 25) -> list[tuple[int, str]]:
    """Autocomplete: prefix/substring hits first, then fuzzy matches for typos."""
    q = q.strip()
    if not q:
        rows = (await session.execute(select(Item.id, Item.name).order_by(Item.updated_at.desc()).limit(limit))).all()
        return [(i, n) for i, n in rows]
    rows = (await session.execute(select(Item.id, Item.name).where(Item.name.ilike(f"%{q}%")).limit(limit))).all()
    out = sorted(((i, n) for i, n in rows), key=lambda r: (not r[1].lower().startswith(q.lower()), len(r[1])))
    if len(out) < limit:
        allnames = (await session.execute(select(Item.id, Item.name))).all()
        choices = {i: n for i, n in allnames if i not in {o[0] for o in out}}
        for n, score, i in process.extract(q, choices, scorer=fuzz.WRatio, limit=limit - len(out)):
            if score >= 70:
                out.append((i, n))
    return out[:limit]


# ----- writes -------------------------------------------------------------------------------
async def create_item(
    session: AsyncSession, viewer: Viewer, data: dict[str, Any], *, via: str = "web", upload_id: int | None = None
) -> Item:
    fields = clean_fields(data)
    existing = await find_by_name(session, fields["name"])
    if existing is not None:
        raise DuplicateItem(existing)
    item = Item(name_key=name_key(fields["name"]), first_cataloged_by=viewer.id, updated_by=viewer.id, **fields)
    session.add(item)
    await session.flush()
    session.add(ItemRevision(item_id=item.id, editor_id=viewer.id, note="cataloged", data=snapshot(item)))
    if upload_id:
        session.add(ItemImage(item_id=item.id, upload_id=upload_id, added_by=viewer.id))
    await ensure_fts(session)
    await reindex(session, item)
    await record(
        session, "archive.item_created", actor_id=viewer.id, entity=item, via=via, summary=f"Cataloged {item.name}"
    )
    await hall.bus.emit("archive.item_created", actor_id=viewer.id, item_id=item.id)
    return item


async def update_item(
    session: AsyncSession, viewer: Viewer, item: Item, data: dict[str, Any], *, note: str = "", via: str = "web"
) -> Item:
    fields = clean_fields(data)
    other = await find_by_name(session, fields["name"])
    if other is not None and other.id != item.id:
        raise ArchiveError(f"Another item is already called {other.name}. Merge them instead.")
    before = snapshot(item)
    for k, v in fields.items():
        setattr(item, k, v)
    item.name_key = name_key(fields["name"])
    item.updated_by = viewer.id
    await session.flush()
    session.add(ItemRevision(item_id=item.id, editor_id=viewer.id, note=note[:200] or "edited", data=snapshot(item)))
    await reindex(session, item)
    await record(
        session,
        "archive.item_updated",
        actor_id=viewer.id,
        entity=item,
        before=before,
        via=via,
        summary=f"Edited {item.name}" + (f": {note}" if note else ""),
    )
    await hall.bus.emit("archive.item_updated", actor_id=viewer.id, item_id=item.id)
    return item


async def add_drop(
    session: AsyncSession,
    viewer: Viewer | None,
    item: Item,
    *,
    creature: str | None,
    zone: str | None,
    note: str | None = None,
    source: str = "manual",
    via: str = "web",
) -> DropReport | None:
    creature = (creature or "").strip()[:120] or None
    zone = (zone or "").strip()[:120] or None
    if not creature and not zone:
        return None
    rep = DropReport(
        item_id=item.id,
        creature=creature,
        zone=zone,
        note=(note or "").strip()[:300] or None,
        source=source,
        reported_by=viewer.id if viewer else None,
    )
    session.add(rep)
    await session.flush()
    await reindex(session, item)
    await record(
        session,
        "archive.drop_reported",
        actor_id=viewer.id if viewer else None,
        entity=rep,
        via=via,
        summary=f"{item.name} from {creature or '?'} in {zone or '?'}",
    )
    await hall.bus.emit(
        "archive.drop_reported", actor_id=viewer.id if viewer else None, item_id=item.id, drop_id=rep.id
    )
    return rep


async def add_image(session: AsyncSession, viewer: Viewer, item: Item, upload_id: int) -> None:
    exists = (
        await session.execute(select(ItemImage).where(ItemImage.item_id == item.id, ItemImage.upload_id == upload_id))
    ).scalar_one_or_none()
    if exists is None:
        session.add(ItemImage(item_id=item.id, upload_id=upload_id, added_by=viewer.id))
        await session.flush()


async def merge_items(session: AsyncSession, viewer: Viewer, keep: Item, drop: Item) -> None:
    """Fold ``drop`` into ``keep``: every row in any module that points at ``drop`` now points at ``keep``."""
    if keep.id == drop.id:
        raise ArchiveError("Pick a different item to merge into.")
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            for fk in col.foreign_keys:
                if fk.column.table.name == "archive_items" and table.name != "archive_items":
                    await session.execute(update(table).where(col == drop.id).values({col.name: keep.id}))
    before = snapshot(drop)
    await session.delete(drop)
    await session.flush()
    if hall.settings.is_sqlite:
        await session.execute(text(f"DELETE FROM {FTS_TABLE} WHERE rowid = :id"), {"id": before["id"]})
    await reindex(session, keep)
    await record(
        session,
        "archive.items_merged",
        actor_id=viewer.id,
        entity=keep,
        before=before,
        summary=f"Merged {before['name']} into {keep.name}",
    )


# ----- reads for display ------------------------------------------------------------------------
async def drop_summary(session: AsyncSession, item_id: int) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(DropReport).where(DropReport.item_id == item_id).order_by(DropReport.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    grouped: dict[tuple, dict] = {}
    for r in rows:
        key = ((r.creature or "").lower(), (r.zone or "").lower())
        g = grouped.setdefault(
            key,
            {"creature": r.creature, "zone": r.zone, "count": 0, "last": r.created_at, "ids": [], "sources": Counter()},
        )
        g["count"] += 1
        g["ids"].append(r.id)
        g["sources"][r.source] += 1
    return sorted(grouped.values(), key=lambda g: -g["count"])


async def known_places(session: AsyncSession) -> tuple[list[str], list[str]]:
    creatures = [
        r[0]
        for r in (
            await session.execute(
                select(DropReport.creature)
                .where(DropReport.creature.is_not(None))
                .group_by(DropReport.creature)
                .order_by(func.count().desc())
                .limit(500)
            )
        ).all()
    ]
    zones = [
        r[0]
        for r in (
            await session.execute(select(DropReport.zone).where(DropReport.zone.is_not(None)).group_by(DropReport.zone))
        ).all()
    ]
    zones = sorted(set(zones) | set(hall.game.zones))
    return creatures, zones


async def loot_table(session: AsyncSession, *, creature: str = "", zone: str = "") -> list[tuple[Item, int]]:
    stmt = select(Item, func.count(DropReport.id)).join(DropReport, DropReport.item_id == Item.id)
    if creature:
        stmt = stmt.where(DropReport.creature.ilike(creature))
    if zone:
        stmt = stmt.where(DropReport.zone.ilike(zone))
    stmt = stmt.group_by(Item.id).order_by(func.count(DropReport.id).desc(), Item.name)
    return [(i, n) for i, n in (await session.execute(stmt.limit(200))).all()]


async def top_catalogers(session: AsyncSession, limit: int = 5) -> list[tuple[str, int]]:
    rows = (
        await session.execute(
            select(Member.display_name, func.count(Item.id))
            .join(Item, Item.first_cataloged_by == Member.id)
            .group_by(Member.id)
            .order_by(func.count(Item.id).desc())
            .limit(limit)
        )
    ).all()
    return [(n, c) for n, c in rows]


def item_lines(item: Item) -> dict[str, Any]:
    """The inspect-window lines, in the game's own vocabulary. Used by web, Discord and the overlay."""
    g = hall.game
    flag_labels = {f["key"]: f["label"] for f in g.flags}
    lines: dict[str, Any] = {"name": item.name}
    lines["flags"] = "  ".join(flag_labels.get(f, f.upper()) for f in item.flags or [])
    lines["slot"] = " ".join(item.slots or [])
    weapon = []
    if item.skill:
        weapon.append(f"Skill: {g.skill_label(item.skill)}")
    if item.delay:
        weapon.append(f"Atk Delay: {item.delay}")
    if item.damage:
        weapon.append(f"DMG: {item.damage}")
    if item.ac:
        weapon.append(f"AC: {item.ac}")
    lines["combat"] = "  ".join(weapon)
    lines["stats"] = [(g.stat_label(k), v) for k, v in (item.stats or {}).items()]
    lines["resists"] = [(g.stat_label(k), v) for k, v in (item.resists or {}).items()]
    lines["effects"] = item.effects or []
    wt = []
    if item.weight is not None:
        wt.append(f"WT: {item.weight:g}")
    if item.size:
        wt.append(f"Size: {item.size}")
    lines["weight"] = "  ".join(wt)
    classes = item.classes or []
    lines["classes"] = "ALL" if "ALL" in classes else " ".join(g.class_abbr(c) for c in classes)
    lines["races"] = "ALL" if "ALL" in (item.races or []) or not item.races else " ".join(item.races)
    return lines


def item_text(item: Item) -> str:
    """Plain-text inspect window (Discord embeds, MCP answers)."""
    L = item_lines(item)
    out = []
    if L["flags"]:
        out.append(L["flags"])
    if L["slot"]:
        out.append(f"Slot: {L['slot']}")
    if L["combat"]:
        out.append(L["combat"])
    if L["stats"]:
        out.append("  ".join(f"{k}: {'+' if v > 0 else ''}{v}" for k, v in L["stats"]))
    if L["resists"]:
        out.append("  ".join(f"{k}: {'+' if v > 0 else ''}{v}" for k, v in L["resists"]))
    for e in L["effects"]:
        out.append(f"Effect: {e}")
    if L["weight"]:
        out.append(L["weight"])
    if L["classes"]:
        out.append(f"Class: {L['classes']}")
    if L["races"]:
        out.append(f"Race: {L['races']}")
    return "\n".join(out)
