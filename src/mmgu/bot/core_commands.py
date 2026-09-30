"""Commands that belong to the hall itself: /hall, /link, /whoami."""

from __future__ import annotations

import discord
from discord import app_commands
from sqlalchemy import delete, select

from mmgu.bot.helpers import embed, reply, viewer_for, web_url
from mmgu.core.audit import record
from mmgu.core.hall import hall
from mmgu.core.members import merge_members
from mmgu.core.models import Identity, LinkCode
from mmgu.core.permissions import RANK_LABEL
from mmgu.db import session_scope, utcnow


async def setup(bot) -> None:
    @app_commands.command(name="hall", description="Show what the guild hall bot can do and link to the web hall")
    async def hall_cmd(interaction: discord.Interaction) -> None:
        lines = []
        for m in hall.enabled_modules():
            if m.kind == "core":
                continue
            lines.append(f"**{m.name}** · {m.plain_name}")
        e = embed(
            f"{hall.app_name} · {hall.guild_name}", "\n".join(lines) or "No rooms are open yet.", url=web_url("/")
        )
        e.add_field(name="Web hall", value=web_url("/"), inline=False)
        await reply(interaction, embed=e)

    @app_commands.command(name="whoami", description="See your rank and duties in the guild hall")
    async def whoami(interaction: discord.Interaction) -> None:
        async with session_scope() as session:
            viewer = await viewer_for(interaction, session)
        duties = ", ".join(sorted(viewer.duties)) or "none"
        await reply(
            interaction,
            f"You are **{viewer.name}**, rank **{RANK_LABEL.get(viewer.rank, viewer.rank)}**, duties: {duties}.",
        )

    @app_commands.command(name="link", description="Link this Discord account to your web hall login")
    @app_commands.describe(code="The code shown on your web profile page")
    async def link(interaction: discord.Interaction, code: str) -> None:
        async with session_scope() as session:
            row = await session.get(LinkCode, code.strip().upper())
            if row is None or row.expires_at < utcnow():
                await reply(
                    interaction, "That code has expired or doesn't exist. Get a fresh one from your web profile."
                )
                return
            target_id = row.member_id
            await session.execute(delete(LinkCode).where(LinkCode.code == row.code))
            ident = (
                await session.execute(
                    select(Identity).where(Identity.provider == "discord", Identity.subject == str(interaction.user.id))
                )
            ).scalar_one_or_none()
            if ident is not None and ident.member_id != target_id:
                # This Discord account already had its own hall member (from using the bot). Fold it in.
                await merge_members(session, ident.member_id, target_id)
            elif ident is None:
                session.add(
                    Identity(
                        member_id=target_id,
                        provider="discord",
                        subject=str(interaction.user.id),
                        label=interaction.user.name,
                    )
                )
            await record(
                session,
                "member.linked_discord",
                actor_id=target_id,
                via="discord",
                entity_type="Member",
                entity_id=target_id,
                summary="Linked a Discord account",
            )
        await reply(interaction, "Linked. Your Discord commands and your web hall now share one identity.")

    bot.add_command(hall_cmd)
    bot.add_command(whoami)
    bot.add_command(link)
