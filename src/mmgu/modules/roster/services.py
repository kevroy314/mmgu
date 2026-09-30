"""Roster logic shared by the web pages, the Discord bot and the MCP server."""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mmgu.core.audit import record, snapshot
from mmgu.core.auth import Viewer
from mmgu.core.hall import hall
from mmgu.core.models import Character, Member
from mmgu.core.whoparse import WhoLine


class RosterError(ValueError):
    pass


def default_server() -> str:
    return hall.setting("roster", "default_server") or ""


def clean_name(name: str) -> str:
    name = (name or "").strip()
    if not name or len(name) > 40:
        raise RosterError("Character names need 1–40 letters.")
    return name[:1].upper() + name[1:]


async def find_character(session: AsyncSession, name: str, server: str | None = None) -> Character | None:
    q = select(Character).where(func.lower(Character.name) == name.strip().lower())
    if server is not None:
        q = q.where(Character.server == server)
    return (await session.execute(q.order_by(Character.id))).scalars().first()


async def characters_of(session: AsyncSession, member_id: int, include_retired: bool = False) -> list[Character]:
    q = select(Character).where(Character.member_id == member_id)
    if not include_retired:
        q = q.where(Character.status == "active")
    q = q.order_by(Character.is_main.desc(), Character.is_bank_mule, Character.level.desc().nullslast(), Character.name)
    return list((await session.execute(q)).scalars().all())


def can_edit(viewer: Viewer, ch: Character) -> bool:
    return viewer.can("characters.edit_any") or (ch.member_id is not None and ch.member_id == viewer.id)


async def save_character(
    session: AsyncSession,
    viewer: Viewer,
    *,
    name: str,
    class_name: str | None,
    level: int | None,
    race: str | None = None,
    server: str | None = None,
    is_main: bool = False,
    is_bank_mule: bool = False,
    notes: str | None = None,
    member_id: int | None = None,
    character: Character | None = None,
    via: str = "web",
) -> Character:
    name = clean_name(name)
    cls = hall.game.class_from_any(class_name) if class_name else None
    if class_name and cls is None:
        raise RosterError(
            f"'{class_name}' isn't a class in {hall.game.name}. Try one of: {', '.join(hall.game.class_names)}."
        )
    if level is not None and not (1 <= level <= max(hall.game.level_cap, 1) + 20):
        raise RosterError(f"Level should be between 1 and {hall.game.level_cap}.")
    if race and race not in hall.game.races:
        race = race.strip()[:40]
    server = (server if server is not None else default_server()) or ""
    owner = member_id if member_id is not None else (character.member_id if character else viewer.id)
    if owner != viewer.id and not viewer.can("characters.edit_any"):
        raise RosterError("You can only add characters for yourself.")
    if is_bank_mule and not (viewer.can("vault.manage") or viewer.can("characters.edit_any")):
        raise RosterError("Only bankers and officers can mark a character as a guild bank mule.")

    clash = await find_character(session, name, server)
    if clash is not None and (character is None or clash.id != character.id):
        if clash.member_id is None and character is None:
            character = clash  # an unclaimed character (from a /who import); claim it
        else:
            raise RosterError(
                f"{name} on {server or 'this server'} already belongs to someone. Ask an officer if that's wrong."
            )

    before = snapshot(character) if character else None
    if character is None:
        character = Character(name=name, server=server)
        session.add(character)
    character.name = name
    character.server = server
    character.member_id = owner
    character.class_name = cls
    character.level = level
    character.race = race or None
    character.is_bank_mule = is_bank_mule
    character.notes = (notes or "").strip() or None
    character.status = "active"
    if is_main and owner:
        for other in await characters_of(session, owner):
            if other is not character:
                other.is_main = False
    character.is_main = is_main
    await session.flush()
    await record(
        session,
        "roster.character_saved",
        actor_id=viewer.id,
        entity=character,
        before=before,
        via=via,
        summary=f"{name} ({cls or '?'} {level or '?'})",
    )
    await hall.bus.emit("roster.character_saved", actor_id=viewer.id, character_id=character.id)
    return character


async def list_characters(
    session: AsyncSession,
    *,
    q: str = "",
    class_name: str = "",
    role: str = "",
    min_level: int | None = None,
    max_level: int | None = None,
    mules: bool | None = None,
    limit: int = 500,
) -> list[tuple[Character, Member | None]]:
    stmt = (
        select(Character, Member)
        .join(Member, Member.id == Character.member_id, isouter=True)
        .where(Character.status == "active")
    )
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Character.name.ilike(like), Member.display_name.ilike(like)))
    if class_name:
        stmt = stmt.where(Character.class_name == class_name)
    if role:
        stmt = stmt.where(Character.class_name.in_([c["name"] for c in hall.game.classes if c.get("role") == role]))
    if min_level is not None:
        stmt = stmt.where(Character.level >= min_level)
    if max_level is not None:
        stmt = stmt.where(Character.level <= max_level)
    if mules is not None:
        stmt = stmt.where(Character.is_bank_mule.is_(mules))
    stmt = stmt.order_by(Character.level.desc().nullslast(), Character.name).limit(limit)
    return [(c, m) for c, m in (await session.execute(stmt)).all()]


async def apply_who(
    session: AsyncSession, viewer: Viewer, lines: list[WhoLine], *, create_unknown: bool, server: str | None = None
) -> dict[str, list[str]]:
    """Update levels/classes for known characters; optionally add unknown ones as unclaimed."""
    server = server if server is not None else default_server()
    out: dict[str, list[str]] = {"updated": [], "created": [], "unchanged": []}
    for line in lines:
        ch = await find_character(session, line.name, server) or await find_character(session, line.name)
        if ch is None:
            if not create_unknown:
                continue
            ch = Character(
                name=line.name,
                server=server or "",
                class_name=line.class_name,
                level=line.level,
                race=line.race,
                member_id=None,
            )
            session.add(ch)
            out["created"].append(line.name)
            continue
        changed = False
        if line.level and line.level != ch.level:
            ch.level, changed = line.level, True
        if line.class_name and line.class_name != ch.class_name:
            ch.class_name, changed = line.class_name, True
        if line.race and not ch.race:
            ch.race, changed = line.race, True
        (out["updated"] if changed else out["unchanged"]).append(line.name)
    await session.flush()
    await record(
        session,
        "roster.who_import",
        actor_id=viewer.id,
        entity_type="Roster",
        summary=f"/who import: {len(out['updated'])} updated, {len(out['created'])} added",
        after=out,
    )
    return out
