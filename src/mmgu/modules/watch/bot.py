"""/tod, /timers, /watch and /camp."""

from __future__ import annotations

from datetime import UTC

import discord
from discord import app_commands

from mmgu.bot.helpers import clip, embed, guard, reply, web_url
from mmgu.db import session_scope
from mmgu.modules.watch import services
from mmgu.modules.watch.models import Timer


def _ts(d) -> int:
    return int(d.replace(tzinfo=UTC).timestamp())


def state_line(s: services.TimerState) -> str:
    name = f"**{s.timer.creature}**" + (f" · {s.timer.zone}" if s.timer.zone else "")
    if s.status == "waiting":
        return f"{name}\n  opens <t:{_ts(s.window_start)}:R> (<t:{_ts(s.window_start)}:t>)"
    if s.status == "open":
        return f"{name}\n  🟢 window OPEN, closes <t:{_ts(s.window_end)}:R>"
    if s.status == "overdue":
        return f"{name}\n  past window since <t:{_ts(s.window_end)}:R>"
    return f"{name}\n  no time of death yet"


async def creature_autocomplete(_interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    async with session_scope() as session:
        timers = await services.suggest_timers(session, current, 25)
    return [
        app_commands.Choice(
            name=clip(t.creature + (f" ({t.zone})" if t.zone else ""), 100), value=clip(t.creature, 100)
        )
        for t in timers
    ]


async def _report(interaction: discord.Interaction, timer_id: int | None, name: str | None, minutes_ago: int) -> None:
    async with session_scope() as session:
        viewer = await guard(interaction, session, "watch.report")
        if viewer is None:
            return
        t = await session.get(Timer, timer_id) if timer_id else await services.find_timer(session, name or "")
        if t is None:
            await reply(interaction, f"**{name}** isn't tracked. See `/timers`; officers can add it on the web.")
            return
        try:
            died = services.parse_when("ago", minutes_ago)
            await services.report_death(session, viewer, t, died, source="discord", via="discord")
        except services.WatchError as e:
            await reply(interaction, str(e))
            return
        ws, we = services.window(t, died)
        creature = t.creature
    when = "just now" if minutes_ago == 0 else f"{minutes_ago} min ago"
    span = f"<t:{_ts(ws)}:t> – <t:{_ts(we)}:t>" if we > ws else f"<t:{_ts(ws)}:t>"
    await reply(
        interaction,
        f"☠ **{creature}** died {when} ({viewer.name}). Next window {span}, opens <t:{_ts(ws)}:R>.",
        ephemeral=False,
    )


async def _toggle(interaction: discord.Interaction, timer_id: int | None, name: str | None) -> None:
    async with session_scope() as session:
        viewer = await guard(interaction, session, "watch.view")
        if viewer is None:
            return
        t = await session.get(Timer, timer_id) if timer_id else await services.find_timer(session, name or "")
        if t is None:
            await reply(interaction, f"**{name}** isn't tracked. See `/timers`.")
            return
        watching = await services.toggle_watch(session, viewer, t, via="discord")
        creature = t.creature
    await reply(
        interaction,
        f"Watching **{creature}**. I'll DM you when the window opens."
        if watching
        else f"Stopped watching **{creature}**.",
    )


async def setup(bot) -> None:
    @app_commands.command(name="tod", description="Report a tracked creature's time of death")
    @app_commands.describe(
        creature="Tracked creature (start typing)", minutes_ago="How long ago it died (default: now)"
    )
    @app_commands.autocomplete(creature=creature_autocomplete)
    async def tod_cmd(
        interaction: discord.Interaction, creature: str, minutes_ago: app_commands.Range[int, 0, 10080] = 0
    ) -> None:
        await _report(interaction, None, creature, minutes_ago)

    @app_commands.command(name="timers", description="Upcoming spawn windows")
    @app_commands.describe(share="Post it for everyone instead of just you")
    async def timers_cmd(interaction: discord.Interaction, share: bool = False) -> None:
        async with session_scope() as session:
            viewer = await guard(interaction, session, "watch.view")
            if viewer is None:
                return
            states = await services.timer_states(session, viewer.id)
            camps = await services.active_camps(session)
        if not states:
            await reply(interaction, "No creatures are tracked yet. Officers can add them on the Watch page.")
            return
        known = [s for s in states if s.status != "unknown"]
        unknown = [s for s in states if s.status == "unknown"]
        desc = "\n".join(state_line(s) for s in known[:20]) or "No times of death reported yet."
        if unknown:
            desc += "\n\n*No ToD yet:* " + ", ".join(s.timer.creature for s in unknown[:20])
        e = embed("Spawn windows", clip(desc, 4000), url=web_url("/watch"))
        if camps:
            e.add_field(
                name="Camps held",
                value=clip(
                    "\n".join(
                        f"{c.camp} · {c.zone}: {c.character_name or (m.display_name if m else '?')}" for c, m in camps
                    ),
                    1024,
                ),
                inline=False,
            )
        await reply(interaction, embed=e, ephemeral=not share)

    @app_commands.command(name="watch", description="Get (or stop getting) a DM when a creature's window opens")
    @app_commands.autocomplete(creature=creature_autocomplete)
    async def watch_cmd(interaction: discord.Interaction, creature: str) -> None:
        await _toggle(interaction, None, creature)

    camp = app_commands.Group(name="camp", description="Let the guild know which camp you're holding")

    @camp.command(name="start", description="Check in at a camp")
    @app_commands.describe(zone="Zone", camp="Camp name, e.g. 'Ogre hill'", character="Your character (default: main)")
    async def camp_start(interaction: discord.Interaction, zone: str, camp: str, character: str | None = None) -> None:
        async with session_scope() as session:
            viewer = await guard(interaction, session, "watch.report")
            if viewer is None:
                return
            try:
                c = await services.start_camp(session, viewer, zone, camp, character, via="discord")
            except services.WatchError as e:
                await reply(interaction, str(e))
                return
            text = f"⛺ **{c.character_name or viewer.name}** is holding **{c.camp}** in {c.zone}."
        await reply(interaction, text, ephemeral=False)

    @camp.command(name="done", description="You've left your camp")
    async def camp_done(interaction: discord.Interaction) -> None:
        async with session_scope() as session:
            viewer = await guard(interaction, session, "watch.report")
            if viewer is None:
                return
            ended = await services.end_my_camps(session, viewer, via="discord")
            names = ", ".join(f"{c.camp} ({c.zone})" for c in ended)
        await reply(
            interaction, f"{names} is free now." if ended else "You weren't holding a camp.", ephemeral=not ended
        )

    @bot.on_component("watch:tod")
    async def on_tod(interaction: discord.Interaction, args: list[str]) -> None:
        await _report(interaction, int(args[0]), None, 0)

    @bot.on_component("watch:watch")
    async def on_watch(interaction: discord.Interaction, args: list[str]) -> None:
        await _toggle(interaction, int(args[0]), None)

    bot.add_command(tod_cmd)
    bot.add_command(timers_cmd)
    bot.add_command(watch_cmd)
    bot.add_command(camp)
