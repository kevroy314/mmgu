"""Lets web routes and event handlers talk to Discord without caring whether the bot is running."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from mmgu.core import store

if TYPE_CHECKING:
    import discord

    from mmgu.bot.client import HallBot
    from mmgu.core.hall import Hall

log = logging.getLogger(__name__)


class DiscordBridge:
    def __init__(self, hall: Hall) -> None:
        self.hall = hall
        self.bot: HallBot | None = None

    @property
    def available(self) -> bool:
        return self.bot is not None and self.bot.is_ready()

    def channel_id(self, key: str) -> int | None:
        raw = store.get(f"channels.{key}") or store.get("channels.default")
        try:
            return int(raw) if raw else None
        except (TypeError, ValueError):
            return None

    async def send(
        self, channel_key: str, content: str | None = None, *, embed: discord.Embed | None = None, view: Any = None
    ) -> discord.Message | None:
        if not self.available:
            return None
        cid = self.channel_id(channel_key)
        if not cid:
            return None
        try:
            channel = self.bot.get_channel(cid) or await self.bot.fetch_channel(cid)
            kwargs: dict[str, Any] = {"content": content, "embed": embed}
            if view is not None:
                kwargs["view"] = view
            return await channel.send(**kwargs)
        except Exception:  # noqa: BLE001
            log.exception("could not post to Discord channel %s (%s)", channel_key, cid)
            return None

    async def edit(self, channel_id: int | str | None, message_id: int | str | None, **kwargs: Any) -> None:
        if not self.available or not channel_id or not message_id:
            return
        try:
            channel = self.bot.get_channel(int(channel_id)) or await self.bot.fetch_channel(int(channel_id))
            msg = await channel.fetch_message(int(message_id))
            await msg.edit(**kwargs)
        except Exception:  # noqa: BLE001
            log.warning("could not edit Discord message %s/%s", channel_id, message_id)

    async def dm(self, member_id: int, content: str | None = None, *, embed: discord.Embed | None = None) -> bool:
        if not self.available:
            return False
        from mmgu.core.models import Identity
        from mmgu.db import session_scope

        async with session_scope() as session:
            subject = (
                await session.execute(
                    select(Identity.subject).where(Identity.member_id == member_id, Identity.provider == "discord")
                )
            ).scalar_one_or_none()
        if not subject:
            return False
        try:
            user = self.bot.get_user(int(subject)) or await self.bot.fetch_user(int(subject))
            await user.send(content=content, embed=embed)
            return True
        except Exception:  # noqa: BLE001
            log.info("could not DM member %s (DMs closed?)", member_id)
            return False

    async def reload_commands(self) -> None:
        if self.bot is not None:
            await self.bot.reload_commands()
