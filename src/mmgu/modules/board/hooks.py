"""How the Notice Board plugs into the rest of the hall."""

from __future__ import annotations

from sqlalchemy import or_, select

from mmgu.core.bus import HallEvent
from mmgu.core.extensions import SearchHit
from mmgu.core.web import render_string
from mmgu.db import session_scope, utcnow


def register(hall) -> None:
    from mmgu.modules.board import services
    from mmgu.modules.board.models import BoardRequest, Listing

    async def item_panel(request, session, item):
        if not request.state.viewer.can("board.view"):
            return None
        rows = await services.listings_for_item(session, item_id=item.id, name=item.name)
        return render_string("board/_item_panel.html", request, item=item, rows=rows)

    async def discord_fields(session, item):
        rows = await services.listings_for_item(session, item_id=item.id, name=item.name)
        out = []
        wts = [li for li, _ in rows if li.kind == "WTS"]
        wtb = [li for li, _ in rows if li.kind == "WTB"]
        if wts:
            priced = [li for li in wts if services.price_value(li.price) is not None]
            low = min(priced, key=lambda li: services.price_value(li.price)) if priced else None
            out.append(("WTS", f"{len(wts)} selling" + (f" · lowest {low.price}" if low else "")))
        if wtb:
            out.append(("WTB", f"{len(wtb)} buying"))
        return out

    async def member_panel(request, session, member):
        if not request.state.viewer.can("board.view"):
            return None
        listings = await services.list_listings(session, author_id=member.id, limit=10)
        reqs = await services.list_requests(session, status="active", author_id=member.id, limit=10)
        helped, thanked = await services.helped_counts(session, member.id)
        if not listings and not reqs and not helped and member.id != request.state.viewer.id:
            return None
        return render_string(
            "board/_member_panel.html",
            request,
            member=member,
            listings=listings,
            reqs=reqs,
            helped=helped,
            thanked=thanked,
        )

    async def card(request, session):
        if not request.state.viewer.can("board.view"):
            return None
        listings = await services.list_listings(session, limit=5)
        reqs = await services.list_requests(session, status="open", limit=5)
        return render_string(
            "board/_card.html", request, listings=listings, reqs=reqs, counts=await services.counts(session)
        )

    async def search(session, q, viewer):
        if not viewer.can("board.view") or len(q.strip()) < 2:
            return []
        like = f"%{q.strip()}%"
        hits = []
        rows = (
            (
                await session.execute(
                    select(Listing)
                    .where(Listing.status == "open", Listing.expires_at > utcnow(), Listing.title.ilike(like))
                    .order_by(Listing.created_at.desc())
                    .limit(6)
                )
            )
            .scalars()
            .all()
        )
        for li in rows:
            hits.append(
                SearchHit(
                    "Listing",
                    f"{li.kind} {li.title}",
                    services.listing_url(li),
                    " · ".join(x for x in (f"×{li.qty}" if li.qty > 1 else "", li.price or "", li.server) if x),
                    score=1.8,
                )
            )
        reqs = (
            (
                await session.execute(
                    select(BoardRequest)
                    .where(
                        BoardRequest.status.in_(("open", "claimed")),
                        or_(BoardRequest.title.ilike(like), BoardRequest.item_name.ilike(like)),
                    )
                    .order_by(BoardRequest.created_at.desc())
                    .limit(5)
                )
            )
            .scalars()
            .all()
        )
        for r in reqs:
            hits.append(SearchHit("Request", r.title, services.request_url(r), r.category, score=1.4))
        return hits

    async def announce(e: HallEvent) -> None:
        if hall.discord is None or not hall.discord.available:
            return
        from mmgu.modules.board.bot import post_embed_and_view

        model = Listing if e.data.get("kind") == "listing" else BoardRequest
        async with session_scope() as session:
            obj = await session.get(model, e.data["id"])
            if obj is None:
                return
            emb, view = await post_embed_and_view(session, obj)
            msg = await hall.discord.send("board", embed=emb, view=view)
            if msg is not None:
                obj.discord_channel_id = str(msg.channel.id)
                obj.discord_message_id = str(msg.id)

    async def refresh(e: HallEvent) -> None:
        if hall.discord is None or not hall.discord.available:
            return
        from mmgu.modules.board.bot import post_embed_and_view

        model = Listing if e.data.get("kind") == "listing" else BoardRequest
        async with session_scope() as session:
            obj = await session.get(model, e.data["id"])
            if obj is None or not obj.discord_message_id:
                return
            emb, view = await post_embed_and_view(session, obj)
            ch, mid = obj.discord_channel_id, obj.discord_message_id
        await hall.discord.edit(ch, mid, embed=emb, view=view)

    hall.bus.subscribe("board.posted", announce)
    hall.bus.subscribe("board.updated", refresh)
    hall.ext.add("item.panels", item_panel, module="board", order=30)
    hall.ext.add("item.discord_fields", discord_fields, module="board", order=30)
    hall.ext.add("member.panels", member_panel, module="board", order=30)
    hall.ext.add("hall.cards", card, module="board", order=30)
    hall.ext.add("search.providers", search, module="board")
