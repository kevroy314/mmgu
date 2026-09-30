from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mmgu.core.auth import Viewer, require
from mmgu.core.hall import hall
from mmgu.core.models import Character, Member
from mmgu.core.web import redirect, render
from mmgu.db import get_session
from mmgu.modules.crafters import services
from mmgu.modules.crafters.models import CraftSkill, Recipe

router = APIRouter()


def _ctx() -> dict:
    return {
        "categories": services.categories(),
        "skill_names": services.skill_names(),
        "skill_max": services.skill_max(),
        "slug": services.slug,
    }


@router.get("/crafters", response_class=HTMLResponse)
async def directory(
    request: Request, viewer: Viewer = Depends(require("crafters.view")), session: AsyncSession = Depends(get_session)
):
    groups = await services.directory(session)
    crafter_count = len({c.character.id for _, skills in groups for _, cs in skills for c in cs})
    recipe_count = (await session.execute(select(func.count()).select_from(Recipe))).scalar_one()
    return render(
        request,
        "crafters/index.html",
        groups=groups,
        crafter_count=crafter_count,
        recipe_count=recipe_count,
        tab="directory",
        **_ctx(),
    )


# ----- my skills grid -----------------------------------------------------------------------------
@router.get("/crafters/mine", response_class=HTMLResponse)
async def mine(
    request: Request,
    member_id: int | None = None,
    viewer: Viewer = Depends(require("crafters.edit")),
    session: AsyncSession = Depends(get_session),
):
    target = member_id or viewer.id
    if target != viewer.id and not viewer.can("crafters.manage"):
        raise HTTPException(403, "You can only edit your own tradeskills.")
    member = await session.get(Member, target)
    if member is None:
        raise HTTPException(404, "No such member.")
    rows = await services.member_skills(session, target)
    return render(request, "crafters/mine.html", member=member, rows=rows, tab="mine", **_ctx())


@router.post("/crafters/mine")
async def save_mine(
    request: Request, viewer: Viewer = Depends(require("crafters.edit")), session: AsyncSession = Depends(get_session)
):
    form = await request.form()
    target = int(form.get("member_id") or viewer.id)
    if target != viewer.id and not viewer.can("crafters.manage"):
        raise HTTPException(403, "You can only edit your own tradeskills.")
    chars = {c.id: c for c, _ in await services.member_skills(session, target)}
    existing = await services.skills_for_characters(session, list(chars))
    changed = 0
    try:
        for cid, rows in existing.items():
            ch = chars[cid]
            for row in rows:
                if form.get(f"del-{row.id}"):
                    await services.remove_skill(session, viewer, row)
                    changed += 1
                    continue
                lvl, spec = form.get(f"lvl-{row.id}"), form.get(f"spec-{row.id}")
                if lvl is None:
                    continue
                if str(lvl).strip() != str(row.level) or (spec or "").strip() != (row.specialty or ""):
                    await services.set_skill(session, viewer, ch, row.skill, lvl, spec)
                    changed += 1
        for cid, ch in chars.items():
            skill = (form.get(f"new-skill-{cid}") or "").strip()
            if not skill:
                continue
            lvl = form.get(f"new-lvl-{cid}") or 0
            await services.set_skill(session, viewer, ch, skill, lvl, form.get(f"new-spec-{cid}"))
            changed += 1
    except services.CraftError as e:
        raise HTTPException(422, str(e)) from e
    await session.commit()
    url = "/crafters/mine" + (f"?member_id={target}" if target != viewer.id else "")
    return redirect(request, url, f"Saved {changed} change{'s' if changed != 1 else ''}." if changed else "No changes.")


# ----- recipes --------------------------------------------------------------------------------------
@router.get("/crafters/recipes", response_class=HTMLResponse)
async def recipes(
    request: Request,
    q: str = "",
    skill: str = "",
    viewer: Viewer = Depends(require("crafters.view")),
    session: AsyncSession = Depends(get_session),
):
    skill = services.canonical_skill(skill) or ""
    rows = await services.list_recipes(session, q=q, skill=skill)
    comps = await services.components_of(session, [r.id for r in rows])
    return render(
        request, "crafters/recipes.html", recipes=rows, comps=comps, q=q, skill=skill, tab="recipes", **_ctx()
    )


def _recipe_form(form) -> dict:
    return {k: form.get(k) for k in ("skill", "result", "trivial", "yield_qty", "notes", "source_url", "components")}


async def _item_names(session: AsyncSession) -> list[str]:
    if not hall.is_enabled("archive"):
        return []
    from mmgu.modules.archive.models import Item

    return list((await session.execute(select(Item.name).order_by(Item.name).limit(3000))).scalars().all())


@router.get("/crafters/recipes/new", response_class=HTMLResponse)
async def new_recipe(
    request: Request,
    skill: str = "",
    result: str = "",
    viewer: Viewer = Depends(require("crafters.edit")),
    session: AsyncSession = Depends(get_session),
):
    data = {"skill": services.canonical_skill(skill) or "", "result": result}
    return render(
        request,
        "crafters/recipe_form.html",
        recipe=None,
        data=data,
        item_names=await _item_names(session),
        tab="recipes",
        **_ctx(),
    )


@router.post("/crafters/recipes")
async def create_recipe(
    request: Request, viewer: Viewer = Depends(require("crafters.edit")), session: AsyncSession = Depends(get_session)
):
    try:
        recipe = await services.save_recipe(session, viewer, _recipe_form(await request.form()))
    except services.CraftError as e:
        raise HTTPException(422, str(e)) from e
    await session.commit()
    return redirect(request, f"/crafters/recipes/{recipe.id}", f"Recipe for {recipe.result_name} added.")


async def _get_recipe(session: AsyncSession, recipe_id: int) -> Recipe:
    recipe = await session.get(Recipe, recipe_id)
    if recipe is None:
        raise HTTPException(404, "That recipe isn't in the Crafters' Hall (it may have been removed).")
    return recipe


@router.get("/crafters/recipes/{recipe_id}", response_class=HTMLResponse)
async def recipe_page(
    request: Request,
    recipe_id: int,
    viewer: Viewer = Depends(require("crafters.view")),
    session: AsyncSession = Depends(get_session),
):
    recipe = await _get_recipe(session, recipe_id)
    comps = (await services.components_of(session, [recipe.id]))[recipe.id]
    makers = await services.makers_of(session, recipe)
    author = await session.get(Member, recipe.created_by) if recipe.created_by else None
    return render(
        request,
        "crafters/recipe.html",
        recipe=recipe,
        comps=comps,
        makers=makers,
        author=author,
        can_edit=services.can_edit_recipe(viewer, recipe),
        board_open=hall.is_enabled("board"),
        tab="recipes",
        **_ctx(),
    )


@router.get("/crafters/recipes/{recipe_id}/edit", response_class=HTMLResponse)
async def edit_recipe_form(
    request: Request,
    recipe_id: int,
    viewer: Viewer = Depends(require("crafters.edit")),
    session: AsyncSession = Depends(get_session),
):
    recipe = await _get_recipe(session, recipe_id)
    if not services.can_edit_recipe(viewer, recipe):
        raise HTTPException(403, "Only the person who added this recipe, or an officer, can change it.")
    comps = (await services.components_of(session, [recipe.id]))[recipe.id]
    data = {
        "skill": recipe.skill,
        "result": recipe.result_name,
        "trivial": recipe.trivial,
        "yield_qty": recipe.yield_qty,
        "notes": recipe.notes,
        "source_url": recipe.source_url,
        "components": services.components_text(comps),
    }
    return render(
        request,
        "crafters/recipe_form.html",
        recipe=recipe,
        data=data,
        item_names=await _item_names(session),
        tab="recipes",
        **_ctx(),
    )


@router.post("/crafters/recipes/{recipe_id}/edit")
async def edit_recipe(
    request: Request,
    recipe_id: int,
    viewer: Viewer = Depends(require("crafters.edit")),
    session: AsyncSession = Depends(get_session),
):
    recipe = await _get_recipe(session, recipe_id)
    try:
        await services.save_recipe(session, viewer, _recipe_form(await request.form()), recipe=recipe)
    except services.CraftError as e:
        raise HTTPException(422 if services.can_edit_recipe(viewer, recipe) else 403, str(e)) from e
    await session.commit()
    return redirect(request, f"/crafters/recipes/{recipe.id}", "Recipe saved.")


@router.post("/crafters/recipes/{recipe_id}/delete")
async def delete_recipe(
    request: Request,
    recipe_id: int,
    viewer: Viewer = Depends(require("crafters.edit")),
    session: AsyncSession = Depends(get_session),
):
    recipe = await _get_recipe(session, recipe_id)
    name = recipe.result_name
    try:
        await services.delete_recipe(session, viewer, recipe)
    except services.CraftError as e:
        raise HTTPException(403, str(e)) from e
    await session.commit()
    return redirect(request, "/crafters/recipes", f"Removed the recipe for {name}.")


@router.post("/crafters/recipes/{recipe_id}/ask")
async def ask_components(
    request: Request,
    recipe_id: int,
    viewer: Viewer = Depends(require("crafters.view")),
    session: AsyncSession = Depends(get_session),
):
    recipe = await _get_recipe(session, recipe_id)
    try:
        req_id = await services.ask_for_components(session, viewer, recipe)
    except services.CraftError as e:
        raise HTTPException(422, str(e)) from e
    await session.commit()
    if req_id:
        return redirect(
            request, f"/board/requests/{req_id}", "Posted a request for the components on the Notice Board."
        )
    return redirect(request, f"/crafters/recipes/{recipe.id}", "The guild has been told you're after these components.")


# ----- who can make X? ------------------------------------------------------------------------------
@router.get("/crafters/whocan", response_class=HTMLResponse)
async def whocan(
    request: Request,
    q: str = "",
    viewer: Viewer = Depends(require("crafters.view")),
    session: AsyncSession = Depends(get_session),
):
    res = await services.who_can(session, q)
    return render(request, "crafters/whocan.html", res=res, q=q, tab="whocan", **_ctx())


@router.get("/api/crafters/whocan")
async def api_whocan(
    q: str = "", viewer: Viewer = Depends(require("crafters.view")), session: AsyncSession = Depends(get_session)
):
    res = await services.who_can(session, q)
    return {"results": res.flat()}


@router.get("/api/crafters/skills")
async def api_skills(
    character: str = "",
    viewer: Viewer = Depends(require("crafters.view")),
    session: AsyncSession = Depends(get_session),
):
    """Every recorded tradeskill line, optionally for one character."""
    stmt = select(CraftSkill, Character).join(Character, Character.id == CraftSkill.character_id)
    if character:
        stmt = stmt.where(Character.name.ilike(character.strip()))
    rows = (await session.execute(stmt.order_by(CraftSkill.skill, CraftSkill.level.desc()))).all()
    return {
        "skills": [{"character": c.name, "skill": s.skill, "level": s.level, "specialty": s.specialty} for s, c in rows]
    }
