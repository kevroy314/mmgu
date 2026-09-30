"""How the War Table plugs into the rest of the hall: panels, cards, search, Discord upkeep and the job."""

from __future__ import annotations

import logging

from sqlalchemy import select

from mmgu.core.bus import HallEvent
from mmgu.core.extensions import SearchHit
from mmgu.core.hall import hall
from mmgu.core.web import render_string
from mmgu.db import session_scope

log = logging.getLogger(__name__)


def _discord_ready() -> bool:
    return hall.discord is not None and hall.discord.available


async def refresh_discord(event_id: int) -> None:
    """Re-render the posted event embed so its counts stay current."""
    if not _discord_ready():
        return
    from mmgu.modules.events.bot import event_embed, event_view
    from mmgu.modules.events.models import Event

    async with session_scope() as session:
        ev = await session.get(Event, event_id)
        if ev is None or not ev.discord_message_id:
            return
        emb = await event_embed(session, ev)
        channel_id, message_id = ev.discord_channel_id, ev.discord_message_id
        v = event_view(ev)
    await hall.discord.edit(channel_id, message_id, embed=emb, view=v)


async def announce_pending() -> None:
    """Post events that start soon and haven't been posted yet to the events channel."""
    if not _discord_ready() or not hall.discord.channel_id("events"):
        return
    from mmgu.modules.events import services
    from mmgu.modules.events.bot import event_embed, event_view

    try:
        days = int(hall.setting("events", "announce_days") or 7)
    except (TypeError, ValueError):
        days = 7
    async with session_scope() as session:
        for ev in await services.to_announce(session, days=days):
            emb = await event_embed(session, ev)
            msg = await hall.discord.send("events", embed=emb, view=event_view(ev))
            if msg is None:
                return
            ev.discord_channel_id, ev.discord_message_id = str(msg.channel.id), str(msg.id)


async def run_job() -> None:
    from mmgu.modules.events import services

    async with session_scope() as session:
        await services.tick(session)
    await announce_pending()


def register(hall) -> None:
    from mmgu.modules.events import services
    from mmgu.modules.events.models import Event, Signup

    # ----- page panels --------------------------------------------------------------------------
    async def item_panel(request, session, item):
        viewer = request.state.viewer
        if not viewer.can("loot.view"):
            return None
        rows = await services.loot_history(session, item_id=item.id, limit=50)
        if not rows:
            return None
        return render_string("events/_item_panel.html", request, rows=rows, item=item)

    async def member_panel(request, session, member):
        viewer = request.state.viewer
        see_loot, see_events = viewer.can("loot.view"), viewer.can("events.view")
        if not (see_loot or see_events):
            return None
        rates = {}
        if see_events:
            for days in (30, 60, 90):
                rates[days] = (await services.attendance_rates(session, days, member_id=member.id)).get(member.id)
        loot = await services.loot_history(session, member_id=member.id, limit=8) if see_loot else []
        return render_string("events/_member_panel.html", request, member=member, rates=rates, loot=loot)

    async def card(request, session):
        viewer = request.state.viewer
        if not viewer.can("events.view"):
            return None
        events = (await services.upcoming(session, limit=3))[:3]
        counts = await services.signup_counts(session, [e.id for e in events])
        mine = {}
        if events and viewer.id:
            mine = {
                s.event_id: s
                for s in (
                    await session.execute(
                        select(Signup).where(Signup.member_id == viewer.id, Signup.event_id.in_([e.id for e in events]))
                    )
                ).scalars()
            }
        return render_string(
            "events/_card.html", request, events=events, counts=counts, mine=mine, labels=services.SIGNUP_LABEL
        )

    async def search(session, q, viewer):
        if not viewer.can("events.view") or len(q.strip()) < 2:
            return []
        return [
            SearchHit(
                "Event",
                e.title,
                f"/events/{e.id}",
                f"{e.kind} · {e.starts_at:%b %d %H:%M} UTC" + (f" · {e.zone}" if e.zone else ""),
                score=2 if e.title.lower().startswith(q.lower()) else 1,
            )
            for e in await services.search_events(session, q)
        ]

    # ----- Discord upkeep -----------------------------------------------------------------------
    async def on_change(e: HallEvent) -> None:
        eid = e.data.get("event_id")
        if eid:
            await refresh_discord(int(eid))

    async def on_reminder(e: HallEvent) -> None:
        if not _discord_ready():
            return
        from mmgu.bot.helpers import web_url

        async with session_scope() as session:
            ev = await session.get(Event, e.data["event_id"])
            if ev is None:
                return
            going = await services.going_signups(session, ev.id)
            text = (
                f"**{ev.title}** starts <t:{_unix(ev.starts_at)}:R>"
                + (f" in {ev.zone}" if ev.zone else "")
                + f". {len(going)} going. {web_url(f'/events/{ev.id}')}"
            )
        await hall.discord.send("events", text)

    hall.ext.add("item.panels", item_panel, module="events", order=40)
    hall.ext.add("member.panels", member_panel, module="events", order=40)
    hall.ext.add("hall.cards", card, module="events", order=5)
    hall.ext.add("search.providers", search, module="events")
    hall.bus.subscribe("events.updated", on_change)
    hall.bus.subscribe("events.signup", on_change)
    hall.bus.subscribe("events.reminder", on_reminder)


def _unix(naive_utc) -> int:
    from datetime import UTC

    return int(naive_utc.replace(tzinfo=UTC).timestamp())
