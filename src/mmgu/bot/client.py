"""The Discord bot. Each enabled module adds its own slash commands and button handlers."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands

from mmgu.core.hall import Hall

log = logging.getLogger(__name__)

ComponentHandler = Callable[[discord.Interaction, list[str]], Awaitable[None]]


class HallBot(commands.Bot):
    def __init__(self, hall: Hall) -> None:
        intents = discord.Intents.default()
        intents.members = True  # needed to keep hall ranks in sync with Discord roles
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)
        self.hall = hall
        self.component_handlers: dict[str, ComponentHandler] = {}
        gid = hall.settings.discord_guild_id
        self.guild_obj = discord.Object(int(gid)) if gid else None
        self.tree.on_error = self._on_tree_error

    # ----- registration helpers used by modules ------------------------------------------------
    def add_command(self, command: app_commands.Command | app_commands.Group | app_commands.ContextMenu) -> None:  # type: ignore[override]
        self.tree.add_command(command, guild=self.guild_obj, override=True)

    def on_component(self, key: str) -> Callable[[ComponentHandler], ComponentHandler]:
        """Register a button/select handler for custom ids shaped ``mmgu:<key>:<arg>:<arg>``."""

        def deco(fn: ComponentHandler) -> ComponentHandler:
            self.component_handlers[key] = fn
            return fn

        return deco

    # ----- lifecycle ---------------------------------------------------------------------------
    async def setup_hook(self) -> None:
        await self._register_modules()
        await self._sync()

    async def _register_modules(self) -> None:
        from mmgu.bot import core_commands

        self.component_handlers.clear()
        await core_commands.setup(self)
        for m in self.hall.enabled_modules():
            if m.bot_setup is None:
                continue
            try:
                result = m.bot_setup(self)
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001
                log.exception("bot setup failed for module %s", m.id)

    async def _sync(self) -> None:
        try:
            if self.guild_obj:
                synced = await self.tree.sync(guild=self.guild_obj)
            else:
                synced = await self.tree.sync()
            log.info("synced %d Discord commands", len(synced))
        except Exception:  # noqa: BLE001
            log.exception("command sync failed")

    async def reload_commands(self) -> None:
        self.tree.clear_commands(guild=self.guild_obj)
        await self._register_modules()
        await self._sync()

    async def on_ready(self) -> None:
        log.info("Discord bot ready as %s", self.user)

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if (
            interaction.type != discord.InteractionType.component
            and interaction.type != discord.InteractionType.modal_submit
        ):
            return
        cid = (interaction.data or {}).get("custom_id", "")
        if not cid.startswith("mmgu:"):
            return
        parts = cid.split(":")
        # mmgu:<module>:<action>:<args...>
        key = ":".join(parts[1:3])
        handler = self.component_handlers.get(key)
        if handler is None:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "That button belongs to a part of the hall that's closed now.", ephemeral=True
                )
            return
        try:
            await handler(interaction, parts[3:])
        except Exception:  # noqa: BLE001
            log.exception("component handler %s failed", key)
            await safe_reply(interaction, "Something went wrong handling that. The steward has been told (check logs).")

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if {r.id for r in before.roles} == {r.id for r in after.roles}:
            return
        from mmgu.bot.helpers import sync_discord_member

        await sync_discord_member(after)

    async def _on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        log.exception("slash command failed", exc_info=error)
        await safe_reply(interaction, "Something went wrong with that command. Check the bot logs for details.")


async def safe_reply(interaction: discord.Interaction, text: str, **kwargs) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True, **kwargs)
        else:
            await interaction.response.send_message(text, ephemeral=True, **kwargs)
    except Exception:  # noqa: BLE001
        log.warning("could not reply to interaction")


async def run_bot(hall: Hall) -> None:
    token = hall.settings.discord_token
    if not token:
        log.info("MMGU_DISCORD_TOKEN is not set; running the web app without the Discord bot")
        return
    bot = HallBot(hall)
    assert hall.discord is not None
    hall.discord.bot = bot
    async with bot:
        await bot.start(token)
