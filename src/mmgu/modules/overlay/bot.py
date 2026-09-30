"""/stream say and /stream item: put things on the guild streamer's overlay from Discord."""

from __future__ import annotations

import discord
from discord import app_commands

from mmgu.bot.helpers import guard, reply
from mmgu.core.hall import hall
from mmgu.db import session_scope
from mmgu.modules.overlay import services


async def _item_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    if not hall.is_enabled("archive"):
        return []
    from mmgu.modules.archive.bot import item_autocomplete

    return await item_autocomplete(interaction, current)


async def setup(bot) -> None:
    stream = app_commands.Group(name="stream", description="Show things on the guild stream overlay")

    @stream.command(name="say", description="Show a message on stream")
    @app_commands.describe(title="Headline, e.g. Welcome raiders!", message="The message (up to 280 characters)")
    async def say(
        interaction: discord.Interaction,
        title: app_commands.Range[str, 1, services.TEXT_TITLE_MAX],
        message: app_commands.Range[str, 1, services.TEXT_BODY_MAX],
    ) -> None:
        async with session_scope() as session:
            viewer = await guard(interaction, session, "overlay.push")
            if viewer is None:
                return
        try:
            await services.push_text(viewer, title, message)
        except services.OverlayError as e:
            await reply(interaction, str(e))
            return
        await reply(interaction, f"On stream: **{title}**")

    @stream.command(name="item", description="Show an item's inspect window on stream")
    @app_commands.describe(name="Item name (start typing)")
    @app_commands.autocomplete(name=_item_autocomplete)
    async def item(interaction: discord.Interaction, name: str) -> None:
        if not hall.is_enabled("archive"):
            await reply(interaction, "The Archive is closed, so there are no items to show.")
            return
        from mmgu.modules.archive.services import find_by_name, suggest_names

        async with session_scope() as session:
            viewer = await guard(interaction, session, "overlay.push")
            if viewer is None:
                return
            found = await find_by_name(session, name)
            if found is None:
                sugg = await suggest_names(session, name, 5)
                hint = ("Did you mean: " + ", ".join(n for _, n in sugg) + "?") if sugg else ""
                await reply(interaction, f"**{name}** isn't in the Archive. {hint}".strip())
                return
            item_id, item_name = found.id, found.name
        await services.push_item(viewer, item_id)
        await reply(interaction, f"On stream: **{item_name}**")

    bot.add_command(stream)
