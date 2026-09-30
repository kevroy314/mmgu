"""How the Archive plugs into the rest of the hall."""

from __future__ import annotations

from sqlalchemy import func, select

from mmgu.core.bus import HallEvent
from mmgu.core.extensions import SearchHit
from mmgu.core.models import Member
from mmgu.core.web import render_string
from mmgu.db import session_scope


def register(hall) -> None:
    from mmgu.modules.archive import services
    from mmgu.modules.archive.models import DropReport, Item

    async def startup(_e: HallEvent) -> None:
        async with session_scope() as session:
            await services.ensure_fts(session)

    async def search(session, q, viewer):
        if not viewer.can("archive.view"):
            return []
        items = await services.search_items(session, q=q, limit=8)
        return [
            SearchHit(
                "Item",
                i.name,
                f"/archive/items/{i.id}",
                " ".join(i.slots or []) or (i.item_type or ""),
                score=3 if i.name.lower().startswith(q.lower()) else 2,
            )
            for i in items
        ]

    async def card(request, session):
        if not request.state.viewer.can("archive.view"):
            return None
        recent = (
            await session.execute(
                select(Item, Member)
                .join(Member, Member.id == Item.first_cataloged_by, isouter=True)
                .order_by(Item.created_at.desc())
                .limit(5)
            )
        ).all()
        total = (await session.execute(select(func.count()).select_from(Item))).scalar_one()
        drops = (await session.execute(select(func.count()).select_from(DropReport))).scalar_one()
        return render_string("archive/_card.html", request, recent=recent, total=total, drops=drops)

    async def announce(e: HallEvent) -> None:
        if hall.discord is None or not hall.discord.available:
            return
        from mmgu.modules.archive.bot import item_embed

        async with session_scope() as session:
            item = await session.get(Item, e.data["item_id"])
            finder = await session.get(Member, item.first_cataloged_by) if item and item.first_cataloged_by else None
            if item is None:
                return
            emb = await item_embed(session, item)
        emb.set_author(name=f"New discovery{' by ' + finder.display_name if finder else ''}")
        await hall.discord.send("archive", embed=emb)

    hall.bus.subscribe("hall.startup", startup)
    hall.bus.subscribe("archive.item_created", announce)
    hall.ext.add("search.providers", search, module="archive")
    hall.ext.add("hall.cards", card, module="archive", order=10)
