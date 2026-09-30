"""How the Watch plugs into the rest of the hall."""

from __future__ import annotations

from mmgu.core.bus import HallEvent
from mmgu.core.extensions import SearchHit
from mmgu.core.web import render_string
from mmgu.db import session_scope


def register(hall) -> None:
    from mmgu.modules.watch import services
    from mmgu.modules.watch.models import Timer

    async def card(request, session):
        viewer = request.state.viewer
        if not viewer.can("watch.view"):
            return None
        states = await services.timer_states(session, viewer.id)
        upcoming = [s for s in states if s.status in ("open", "waiting")][:3]
        camps = await services.active_camps(session)
        if not states and not camps:
            return None
        return render_string("watch/_card.html", request, upcoming=upcoming, camps=camps)

    async def search(session, q, viewer):
        if not viewer.can("watch.view"):
            return []
        from sqlalchemy import or_, select

        like = f"%{q.strip()}%"
        rows = (
            (
                await session.execute(
                    select(Timer).where(or_(Timer.creature.ilike(like), Timer.zone.ilike(like))).limit(8)
                )
            )
            .scalars()
            .all()
        )
        states = {s.timer.id: s for s in await services.timer_states(session, timer_ids=[t.id for t in rows])}
        hits = []
        for t in rows:
            s = states.get(t.id)
            sub = " · ".join(x for x in (t.zone, s.label if s else "switched off") if x)
            hits.append(
                SearchHit(
                    "Spawn timer",
                    t.creature,
                    f"/watch/timers/{t.id}",
                    sub,
                    score=2 if t.creature.lower().startswith(q.strip().lower()) else 1,
                )
            )
        return hits

    async def announce_open(e: HallEvent) -> None:
        if hall.discord is None or not hall.discord.available:
            return
        from mmgu.bot.helpers import TIDE, button, cid, embed, view, web_url

        async with session_scope() as session:
            t = await session.get(Timer, e.data["timer_id"])
            if t is None:
                return
            s = await services.timer_state(session, t)
        if s.window_end is None:
            return
        emb = embed(
            f"{t.creature}: spawn window open",
            (f"**{t.zone}**\n" if t.zone else "")
            + (f"Closes <t:{_ts(s.window_end)}:R>" if s.window_end > s.window_start else "Should be up now.")
            + (f"\n{t.notes}" if t.notes else ""),
            url=web_url(f"/watch/timers/{t.id}"),
            color=TIDE,
        )
        await hall.discord.send(
            "watch",
            embed=emb,
            view=view(
                button("ToD now", cid("watch", "tod", t.id)),
                button("Watch", cid("watch", "watch", t.id)),
                button("Open the Watch", url=web_url("/watch")),
            ),
        )

    hall.bus.subscribe("watch.window_open", announce_open)
    hall.ext.add("hall.cards", card, module="watch", order=30)
    hall.ext.add("search.providers", search, module="watch")


def _ts(d) -> int:
    """Unix seconds for a naive-UTC datetime (Discord's <t:...> timestamps)."""
    from datetime import UTC

    return int(d.replace(tzinfo=UTC).timestamp())
