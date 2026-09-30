"""How the Vault plugs into the rest of the hall."""

from __future__ import annotations

from urllib.parse import quote_plus

from sqlalchemy import select

from mmgu.core.bus import HallEvent
from mmgu.core.extensions import SearchHit
from mmgu.core.models import Character
from mmgu.core.web import render_string
from mmgu.db import session_scope


def register(hall) -> None:
    from mmgu.modules.vault import services
    from mmgu.modules.vault.models import Request, Transaction

    async def item_panel(request, session, item):
        viewer = request.state.viewer
        if not viewer.can("vault.view"):
            return None
        rows = await services.holders_of(session, item_id=item.id, name=item.name)
        return render_string(
            "vault/_item_panel.html",
            request,
            item=item,
            rows=rows,
            total=sum(h.qty for h, _ in rows),
        )

    async def discord_fields(session, item):
        total = await services.held_qty(session, item_id=item.id, name=item.name)
        if not total:
            return []
        rows = await services.holders_of(session, item_id=item.id, name=item.name)
        where = ", ".join(f"{c.name} ({h.qty})" for h, c in rows[:4])
        return [("In the Vault", f"{total} · {where}")]

    async def card(request, session):
        viewer = request.state.viewer
        if not viewer.can("vault.view"):
            return None
        pending = await services.list_requests(session, status="pending", limit=5) if viewer.can("vault.manage") else []
        mine = await services.list_requests(session, status="open", requester_id=viewer.id, limit=3)
        deposits = (
            await session.execute(
                select(Transaction, Character)
                .join(Character, Character.id == Transaction.character_id)
                .where(Transaction.kind == "deposit")
                .order_by(Transaction.created_at.desc())
                .limit(5)
            )
        ).all()
        return render_string("vault/_card.html", request, pending=pending, mine=mine, deposits=deposits)

    async def search(session, q, viewer):
        if not viewer.can("vault.view") or len(q.strip()) < 2:
            return []
        tots = services.totals(await services.holdings(session, q=q))
        return [
            SearchHit(
                "In the Vault",
                t["name"],
                "/vault?q=" + quote_plus(t["name"]),
                f"{t['qty']} held on " + ", ".join(c.name for c, _ in t["mules"][:3]),
                score=2.5 if t["name"].lower().startswith(q.lower()) else 1.5,
            )
            for t in tots[:6]
        ]

    async def announce(e: HallEvent) -> None:
        if hall.discord is None or not hall.discord.available:
            return
        from mmgu.modules.vault.bot import request_buttons, request_embed

        async with session_scope() as session:
            req = await session.get(Request, e.data["request_id"])
            if req is None:
                return
            emb = await request_embed(session, req)
            msg = await hall.discord.send("vault", embed=emb, view=request_buttons(req))
            if msg is not None:
                req.discord_channel_id = str(msg.channel.id)
                req.discord_message_id = str(msg.id)

    async def refresh(e: HallEvent) -> None:
        if hall.discord is None or not hall.discord.available:
            return
        from mmgu.modules.vault.bot import request_buttons, request_embed

        async with session_scope() as session:
            req = await session.get(Request, e.data["request_id"])
            if req is None or not req.discord_message_id:
                return
            emb = await request_embed(session, req)
            ch, mid, view = req.discord_channel_id, req.discord_message_id, request_buttons(req)
        await hall.discord.edit(ch, mid, embed=emb, view=view)

    hall.bus.subscribe("vault.request_created", announce)
    hall.bus.subscribe("vault.request_updated", refresh)
    hall.ext.add("item.panels", item_panel, module="vault", order=20)
    hall.ext.add("item.discord_fields", discord_fields, module="vault", order=20)
    hall.ext.add("hall.cards", card, module="vault", order=20)
    hall.ext.add("search.providers", search, module="vault")
