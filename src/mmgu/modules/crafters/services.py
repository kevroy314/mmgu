"""Crafters' Hall logic shared by the web pages, the Discord bot and the JSON API."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mmgu.core.audit import record, snapshot
from mmgu.core.auth import Viewer
from mmgu.core.hall import hall
from mmgu.core.models import Character, Member
from mmgu.modules.crafters.models import CraftSkill, Recipe, RecipeComponent


class CraftError(ValueError):
    pass


# ----- the game pack's tradeskills ------------------------------------------------------------
def categories() -> dict[str, list[str]]:
    return {cat: list(skills) for cat, skills in (hall.game.tradeskills or {}).items()}


def skill_names() -> list[str]:
    return hall.game.tradeskill_names


def skill_max() -> int:
    return max(hall.game.tradeskill_max, 1)


def category_of(skill: str) -> str:
    for cat, skills in categories().items():
        if skill in skills:
            return cat
    return "other"


def canonical_skill(text: str | None) -> str | None:
    """'blacksmithing', 'Blacksmithing ' -> 'Blacksmithing'; unknown names -> None."""
    t = re.sub(r"\s+", " ", (text or "").strip()).lower()
    if not t:
        return None
    for s in skill_names():
        if s.lower() == t:
            return s
    return None


def slug(skill: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", skill.lower()).strip("-")


def _level(v: Any) -> int | None:
    if v is None or str(v).strip() == "":
        return None
    try:
        n = int(float(str(v).strip()))
    except ValueError as e:
        raise CraftError(f"'{v}' isn't a number. Skill levels are whole numbers like 125.") from e
    if n < 0 or n > skill_max():
        raise CraftError(f"Skill levels go from 0 to {skill_max()}.")
    return n


def _clean(text: Any, n: int) -> str | None:
    s = re.sub(r"\s+", " ", str(text or "").strip())
    return s[:n] or None


def _name_key(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower().replace("’", "'"))


# ----- skills ---------------------------------------------------------------------------------
def can_edit_character(viewer: Viewer, ch: Character) -> bool:
    if viewer.can("crafters.manage"):
        return True
    return viewer.can("crafters.edit") and ch.member_id is not None and ch.member_id == viewer.id


async def set_skill(
    session: AsyncSession,
    viewer: Viewer,
    character: Character,
    skill: str,
    level: Any,
    specialty: str | None = None,
    *,
    via: str = "web",
) -> CraftSkill | None:
    """Create, update or (with an empty level and no specialty) remove one skill line."""
    if not can_edit_character(viewer, character):
        raise CraftError(f"{character.name} isn't your character, so you can't change their tradeskills.")
    name = canonical_skill(skill)
    if name is None:
        raise CraftError(f"'{skill}' isn't a tradeskill in {hall.game.name}. Pick one from the list.")
    lvl = _level(level)
    spec = _clean(specialty, 300)
    row = (
        await session.execute(
            select(CraftSkill).where(CraftSkill.character_id == character.id, CraftSkill.skill == name)
        )
    ).scalar_one_or_none()
    if lvl is None and spec is None:
        if row is not None:
            await remove_skill(session, viewer, row, via=via)
        return None
    before = snapshot(row) if row else None
    if row is None:
        row = CraftSkill(character_id=character.id, skill=name)
        session.add(row)
    elif row.level == (lvl or 0) and row.specialty == spec:
        return row
    row.level = lvl or 0
    row.specialty = spec
    row.updated_by = viewer.id
    await session.flush()
    await record(
        session,
        "crafters.skill_saved",
        actor_id=viewer.id,
        entity=row,
        before=before,
        via=via,
        summary=f"{character.name}: {name} {row.level}" + (f" ({spec})" if spec else ""),
    )
    await hall.bus.emit("crafters.skill_saved", actor_id=viewer.id, character_id=character.id)
    return row


async def remove_skill(session: AsyncSession, viewer: Viewer, row: CraftSkill, *, via: str = "web") -> None:
    ch = await session.get(Character, row.character_id)
    if ch is None or not can_edit_character(viewer, ch):
        raise CraftError("That isn't your character.")
    await record(
        session,
        "crafters.skill_removed",
        actor_id=viewer.id,
        entity=row,
        before=snapshot(row),
        after={},
        via=via,
        summary=f"{ch.name}: removed {row.skill}",
    )
    await session.delete(row)
    await session.flush()
    await hall.bus.emit("crafters.skill_saved", actor_id=viewer.id, character_id=ch.id)


async def skills_for_characters(session: AsyncSession, char_ids: list[int]) -> dict[int, list[CraftSkill]]:
    out: dict[int, list[CraftSkill]] = {cid: [] for cid in char_ids}
    if not char_ids:
        return out
    rows = (
        (
            await session.execute(
                select(CraftSkill)
                .where(CraftSkill.character_id.in_(char_ids))
                .order_by(CraftSkill.level.desc(), CraftSkill.skill)
            )
        )
        .scalars()
        .all()
    )
    for r in rows:
        out[r.character_id].append(r)
    return out


async def member_skills(session: AsyncSession, member_id: int) -> list[tuple[Character, list[CraftSkill]]]:
    from mmgu.modules.roster.services import characters_of

    chars = await characters_of(session, member_id)
    by = await skills_for_characters(session, [c.id for c in chars])
    return [(c, by[c.id]) for c in chars]


@dataclass
class Crafter:
    row: CraftSkill
    character: Character
    member: Member | None

    @property
    def pct(self) -> int:
        return min(100, round(100 * (self.row.level or 0) / skill_max()))


async def crafters_with(
    session: AsyncSession, skill: str | None = None, *, min_level: int | None = None, specialty_like: str = ""
) -> list[Crafter]:
    stmt = (
        select(CraftSkill, Character, Member)
        .join(Character, Character.id == CraftSkill.character_id)
        .join(Member, Member.id == Character.member_id, isouter=True)
        .where(Character.status == "active")
    )
    if skill:
        stmt = stmt.where(CraftSkill.skill == skill)
    if min_level is not None:
        stmt = stmt.where(CraftSkill.level >= min_level)
    if specialty_like:
        stmt = stmt.where(CraftSkill.specialty.ilike(f"%{specialty_like}%"))
    stmt = stmt.order_by(CraftSkill.level.desc(), Character.name)
    return [Crafter(r, c, m) for r, c, m in (await session.execute(stmt)).all()]


async def directory(session: AsyncSession) -> list[tuple[str, list[tuple[str, list[Crafter]]]]]:
    """[(category, [(skill, crafters highest first)])], every skill from the game pack in pack order."""
    everyone = await crafters_with(session)
    by_skill: dict[str, list[Crafter]] = {}
    for c in everyone:
        by_skill.setdefault(c.row.skill, []).append(c)
    out = []
    known: set[str] = set()
    for cat, skills in categories().items():
        out.append((cat, [(s, by_skill.get(s, [])) for s in skills]))
        known.update(skills)
    leftovers = sorted(s for s in by_skill if s not in known)  # skills removed from the pack by a patch
    if leftovers:
        out.append(("retired skills", [(s, by_skill[s]) for s in leftovers]))
    return out


# ----- recipes --------------------------------------------------------------------------------
COMPONENT_RE = [
    re.compile(r"^(?P<qty>\d+)\s*[x×]?\s+(?P<name>.+)$", re.I),
    re.compile(r"^(?P<name>.+?)\s*[x×]\s*(?P<qty>\d+)$", re.I),
    re.compile(r"^(?P<name>.+?)\s*\((?P<qty>\d+)\)$"),
]


def parse_components(text: str) -> list[tuple[str, int]]:
    """One component per line: '2 Bronze Bar', '2x Bronze Bar', 'Bronze Bar x2' or just 'Bronze Bar'."""
    out: list[tuple[str, int]] = []
    for raw in (text or "").splitlines():
        line = re.sub(r"\s+", " ", raw.strip().lstrip("-•*").strip())
        if not line:
            continue
        name, qty = line, 1
        for rx in COMPONENT_RE:
            m = rx.match(line)
            if m:
                name, qty = m.group("name").strip(), int(m.group("qty"))
                break
        if not name:
            continue
        out.append((name[:120], max(1, min(qty, 9999))))
    return out[:30]


def components_text(components: list[RecipeComponent]) -> str:
    return "\n".join(f"{c.qty} {c.name}" if c.qty != 1 else c.name for c in components)


async def find_item(session: AsyncSession, name: str):
    """The Archive item with exactly this name, when the Archive is open."""
    if not name or not hall.is_enabled("archive"):
        return None
    from mmgu.modules.archive.services import find_by_name

    return await find_by_name(session, name)


def can_edit_recipe(viewer: Viewer, recipe: Recipe) -> bool:
    return viewer.can("crafters.manage") or (viewer.can("crafters.edit") and recipe.created_by == viewer.id)


async def save_recipe(
    session: AsyncSession,
    viewer: Viewer,
    data: dict[str, Any],
    *,
    recipe: Recipe | None = None,
    via: str = "web",
) -> Recipe:
    if recipe is not None and not can_edit_recipe(viewer, recipe):
        raise CraftError("Only the person who added this recipe, or an officer, can change it.")
    if recipe is None and not viewer.can("crafters.edit"):
        raise CraftError("Your rank can't add recipes yet.")
    skill = canonical_skill(data.get("skill"))
    if skill is None:
        raise CraftError("Pick the tradeskill this recipe uses.")
    result = _clean(data.get("result"), 120)
    if not result:
        raise CraftError("Name what the recipe makes, e.g. 'Bronze Short Sword'.")
    trivial = _level(data.get("trivial"))
    try:
        yield_qty = max(1, min(int(str(data.get("yield_qty") or 1).strip()), 999))
    except ValueError as e:
        raise CraftError("How many it makes should be a whole number.") from e
    url = _clean(data.get("source_url"), 500)
    if url and not re.match(r"^https?://", url):
        raise CraftError("The source link should start with http:// or https://.")
    comps = data.get("components")
    if isinstance(comps, str):
        comps = parse_components(comps)
    comps = list(comps or [])

    before = snapshot(recipe) if recipe else None
    if recipe is None:
        recipe = Recipe(created_by=viewer.id, skill=skill, result_name=result)
        session.add(recipe)
    item = await find_item(session, result)
    recipe.skill = skill
    recipe.result_name = item.name if item else result
    recipe.result_item_id = item.id if item else None
    recipe.trivial = trivial
    recipe.yield_qty = yield_qty
    recipe.notes = str(data.get("notes") or "").strip()[:4000] or None
    recipe.source_url = url
    await session.flush()
    old = (await session.execute(select(RecipeComponent).where(RecipeComponent.recipe_id == recipe.id))).scalars().all()
    for c in old:
        await session.delete(c)
    for pos, (name, qty) in enumerate(comps):
        it = await find_item(session, name)
        session.add(
            RecipeComponent(
                recipe_id=recipe.id, item_id=it.id if it else None, name=it.name if it else name, qty=qty, position=pos
            )
        )
    await session.flush()
    await record(
        session,
        "crafters.recipe_saved",
        actor_id=viewer.id,
        entity=recipe,
        before=before,
        via=via,
        summary=f"Recipe: {recipe.result_name} ({skill}{f' {trivial}' if trivial else ''})",
    )
    await hall.bus.emit("crafters.recipe_saved", actor_id=viewer.id, recipe_id=recipe.id)
    return recipe


async def delete_recipe(session: AsyncSession, viewer: Viewer, recipe: Recipe, *, via: str = "web") -> None:
    if not can_edit_recipe(viewer, recipe):
        raise CraftError("Only the person who added this recipe, or an officer, can remove it.")
    await record(
        session,
        "crafters.recipe_deleted",
        actor_id=viewer.id,
        entity=recipe,
        before=snapshot(recipe),
        after={},
        via=via,
        summary=f"Removed recipe: {recipe.result_name}",
    )
    await session.delete(recipe)
    await session.flush()
    await hall.bus.emit("crafters.recipe_deleted", actor_id=viewer.id, recipe_id=recipe.id)


async def components_of(session: AsyncSession, recipe_ids: list[int]) -> dict[int, list[RecipeComponent]]:
    out: dict[int, list[RecipeComponent]] = {r: [] for r in recipe_ids}
    if not recipe_ids:
        return out
    rows = (
        (
            await session.execute(
                select(RecipeComponent)
                .where(RecipeComponent.recipe_id.in_(recipe_ids))
                .order_by(RecipeComponent.recipe_id, RecipeComponent.position)
            )
        )
        .scalars()
        .all()
    )
    for c in rows:
        out[c.recipe_id].append(c)
    return out


async def list_recipes(session: AsyncSession, *, q: str = "", skill: str = "", limit: int = 200) -> list[Recipe]:
    stmt = select(Recipe)
    if skill:
        stmt = stmt.where(Recipe.skill == skill)
    if q.strip():
        like = f"%{q.strip()}%"
        comp_ids = select(RecipeComponent.recipe_id).where(RecipeComponent.name.ilike(like))
        stmt = stmt.where(or_(Recipe.result_name.ilike(like), Recipe.notes.ilike(like), Recipe.id.in_(comp_ids)))
    stmt = stmt.order_by(Recipe.skill, Recipe.trivial.nullslast(), Recipe.result_name).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def recipes_for_item(session: AsyncSession, item) -> tuple[list[Recipe], list[Recipe]]:
    """(recipes that make this item, recipes that use it as a component)."""
    key = _name_key(item.name)
    makes = (
        (
            await session.execute(
                select(Recipe)
                .where(or_(Recipe.result_item_id == item.id, func.lower(Recipe.result_name) == key))
                .order_by(Recipe.trivial.nullslast())
            )
        )
        .scalars()
        .all()
    )
    used_ids = select(RecipeComponent.recipe_id).where(
        or_(RecipeComponent.item_id == item.id, func.lower(RecipeComponent.name) == key)
    )
    uses = (
        (await session.execute(select(Recipe).where(Recipe.id.in_(used_ids)).order_by(Recipe.result_name)))
        .scalars()
        .all()
    )
    return list(makes), list(uses)


async def makers_of(session: AsyncSession, recipe: Recipe) -> list[Crafter]:
    """Everyone whose skill reaches the recipe's trivial (everyone with the skill when there's none)."""
    return await crafters_with(session, recipe.skill, min_level=recipe.trivial)


# ----- who can make X? ------------------------------------------------------------------------
@dataclass
class WhoCanResult:
    q: str
    skill: str | None = None
    skill_crafters: list[Crafter] = field(default_factory=list)
    recipes: list[tuple[Recipe, list[RecipeComponent], list[Crafter]]] = field(default_factory=list)
    specialists: list[Crafter] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.skill_crafters or self.recipes or self.specialists)

    def flat(self) -> list[dict[str, Any]]:
        """One row per (crafter, reason) for the JSON API and Discord."""
        rows: list[dict[str, Any]] = []
        seen: set[tuple[int, str | None]] = set()

        def add(c: Crafter, recipe: str | None) -> None:
            k = (c.row.id, recipe)
            if k in seen:
                return
            seen.add(k)
            rows.append(
                {
                    "character": c.character.name,
                    "member": c.member.display_name if c.member else None,
                    "skill": c.row.skill,
                    "level": c.row.level,
                    "recipe": recipe,
                    "specialty": c.row.specialty,
                }
            )

        for r, _comps, crafters in self.recipes:
            for c in crafters:
                add(c, r.result_name)
        for c in self.skill_crafters:
            add(c, None)
        for c in self.specialists:
            add(c, None)
        return rows


async def who_can(session: AsyncSession, q: str) -> WhoCanResult:
    q = re.sub(r"\s+", " ", (q or "").strip())
    res = WhoCanResult(q=q)
    if len(q) < 2:
        return res
    skill = canonical_skill(q)
    if skill:
        res.skill = skill
        res.skill_crafters = await crafters_with(session, skill)
        return res
    like = f"%{q}%"
    recipes = (
        (
            await session.execute(
                select(Recipe)
                .where(Recipe.result_name.ilike(like))
                .order_by(func.length(Recipe.result_name), Recipe.result_name)
                .limit(15)
            )
        )
        .scalars()
        .all()
    )
    comps = await components_of(session, [r.id for r in recipes])
    for r in recipes:
        res.recipes.append((r, comps[r.id], await makers_of(session, r)))
    res.specialists = await crafters_with(session, specialty_like=q)
    return res


# ----- asking the guild for components -------------------------------------------------------
async def ask_for_components(session: AsyncSession, viewer: Viewer, recipe: Recipe, *, via: str = "web") -> int | None:
    """Post a Notice Board request for the recipe's components when the board is open, and always
    emit ``crafters.components_wanted``. Returns the board request id, or None when no request was posted.
    """
    comps = (await components_of(session, [recipe.id]))[recipe.id]
    if not comps:
        raise CraftError("This recipe has no components listed, so there's nothing to ask for.")
    lines = "\n".join(f"- {c.qty} × {c.name}" for c in comps)
    title = f"Components for {recipe.result_name}"
    details = (
        f"{viewer.name} wants to make **{recipe.result_name}** ({recipe.skill}"
        f"{f', trivial {recipe.trivial}' if recipe.trivial else ''}) and needs:\n{lines}\n\n"
        f"Recipe: {hall.settings.base_url.rstrip('/')}/crafters/recipes/{recipe.id}"
    )
    posted = await _post_board_request(session, viewer, recipe, title, details, via=via)
    await record(
        session,
        "crafters.components_wanted",
        actor_id=viewer.id,
        entity=recipe,
        via=via,
        summary=f"Asked the guild for components: {recipe.result_name}",
    )
    await hall.bus.emit(
        "crafters.components_wanted",
        actor_id=viewer.id,
        recipe_id=recipe.id,
        member_id=viewer.id,
        board_request_id=posted,
    )
    return posted


async def _post_board_request(
    session: AsyncSession, viewer: Viewer, recipe: Recipe, title: str, details: str, *, via: str
):
    """Create a Notice Board request when the board is open; returns its id, or None."""
    if not hall.is_enabled("board") or not viewer.can("board.post"):
        return None
    from mmgu.modules.board import services as board

    try:
        req = await board.create_request(
            session,
            viewer,
            category="Crafting",
            title=title[:140],
            details=details,
            item=recipe.result_name,
            tradeskill=recipe.skill,
            via=via,
        )
    except board.BoardError as e:
        raise CraftError(str(e)) from e
    return req.id
