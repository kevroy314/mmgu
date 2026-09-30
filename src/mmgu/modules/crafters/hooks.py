"""How the Crafters' Hall plugs into the rest of the hall."""

from __future__ import annotations

from urllib.parse import quote

from sqlalchemy import select

from mmgu.core.extensions import SearchHit
from mmgu.core.models import Character
from mmgu.core.web import render_string


def register(hall) -> None:
    from mmgu.modules.crafters import services
    from mmgu.modules.crafters.models import CraftSkill, Recipe

    async def member_panel(request, session, member):
        viewer = request.state.viewer
        if not viewer.can("crafters.view"):
            return None
        rows = await services.member_skills(session, member.id)
        can_edit = viewer.can("crafters.manage") or (viewer.id == member.id and viewer.can("crafters.edit"))
        if not any(skills for _, skills in rows) and not can_edit:
            return None
        return render_string(
            "crafters/_member_panel.html",
            request,
            rows=rows,
            member=member,
            can_edit=can_edit,
            skill_max=services.skill_max(),
        )

    async def item_panel(request, session, item):
        if not request.state.viewer.can("crafters.view"):
            return None
        makes, uses = await services.recipes_for_item(session, item)
        comps = await services.components_of(session, [r.id for r in makes])
        made = [(r, comps[r.id], await services.makers_of(session, r)) for r in makes]
        return render_string("crafters/_item_panel.html", request, item=item, makes=made, uses=uses)

    async def item_fields(session, item):
        makes, _ = await services.recipes_for_item(session, item)
        out = []
        for r in makes[:2]:
            makers = await services.makers_of(session, r)
            names = ", ".join(c.character.name for c in makers[:6]) or "nobody recorded yet"
            out.append((f"Crafted ({r.skill}{f' {r.trivial}' if r.trivial else ''})", names))
        return out

    async def card(request, session):
        if not request.state.viewer.can("crafters.view"):
            return None
        recent = (
            await session.execute(
                select(CraftSkill, Character)
                .join(Character, Character.id == CraftSkill.character_id)
                .where(Character.status == "active")
                .order_by(CraftSkill.updated_at.desc())
                .limit(5)
            )
        ).all()
        return render_string("crafters/_card.html", request, recent=recent)

    async def search(session, q, viewer):
        if not viewer.can("crafters.view"):
            return []
        hits: list[SearchHit] = []
        ql = q.strip().lower()
        for s in services.skill_names():
            if ql and ql in s.lower():
                hits.append(
                    SearchHit(
                        "Tradeskill",
                        s,
                        f"/crafters/whocan?q={quote(s)}",
                        "Who has it, highest first",
                        score=2.5 if s.lower().startswith(ql) else 1.5,
                    )
                )
        rows = (
            (
                await session.execute(
                    select(Recipe)
                    .where(Recipe.result_name.ilike(f"%{q.strip()}%"))
                    .order_by(Recipe.result_name)
                    .limit(8)
                )
            )
            .scalars()
            .all()
        )
        for r in rows:
            hits.append(
                SearchHit(
                    "Recipe",
                    r.result_name,
                    f"/crafters/recipes/{r.id}",
                    f"{r.skill}{f' · trivial {r.trivial}' if r.trivial else ''}",
                    score=2 if r.result_name.lower().startswith(ql) else 1,
                )
            )
        return hits

    hall.ext.add("member.panels", member_panel, module="crafters", order=40)
    hall.ext.add("item.panels", item_panel, module="crafters", order=40)
    hall.ext.add("item.discord_fields", item_fields, module="crafters", order=40)
    hall.ext.add("hall.cards", card, module="crafters", order=40)
    hall.ext.add("search.providers", search, module="crafters")
