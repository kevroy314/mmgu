"""Helpers for module bot code: who is this Discord user in the hall, can they do this, nice embeds."""

from __future__ import annotations

import logging
from typing import Any

import discord
from sqlalchemy.ext.asyncio import AsyncSession

from mmgu.core.auth import Viewer, build_viewer, find_or_create_member, sync_roles
from mmgu.core.hall import hall
from mmgu.core.permissions import ALL_ROLES
from mmgu.db import session_scope

log = logging.getLogger(__name__)

GOLD = 0xE6C24A
TIDE = 0x5FB3AC
EMBER = 0xE0925A
BLOOD = 0xE06A5A


def web_url(path: str = "/") -> str:
    return hall.settings.base_url.rstrip("/") + path


def cid(module: str, action: str, *args: Any) -> str:
    """Build a component custom id; the bot dispatches it to the handler registered for module:action."""
    value = ":".join(["mmgu", module, action, *[str(a) for a in args]])
    if len(value) > 100:
        raise ValueError("custom_id too long")
    return value


def embed(
    title: str, description: str | None = None, *, url: str | None = None, color: int = GOLD, footer: str | None = None
) -> discord.Embed:
    e = discord.Embed(title=title[:256], description=(description or "")[:4000] or None, url=url, color=color)
    e.set_footer(text=footer or f"{hall.app_name} · {hall.guild_name}")
    return e


def button(
    label: str,
    custom_id: str | None = None,
    *,
    style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    url: str | None = None,
    emoji: str | None = None,
    disabled: bool = False,
) -> discord.ui.Button:
    if url:
        return discord.ui.Button(label=label, url=url, style=discord.ButtonStyle.link, emoji=emoji)
    return discord.ui.Button(label=label, custom_id=custom_id, style=style, emoji=emoji, disabled=disabled)


def view(*items: discord.ui.Item) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    for it in items:
        v.add_item(it)
    return v


def _mapped_roles(member: discord.Member) -> set[str]:
    mapping = hall.settings.discord_role_pairs
    out = set()
    for r in member.roles:
        role = mapping.get(str(r.id))
        if role in ALL_ROLES:
            out.add(role)
    return out


async def _member_and_viewer(session: AsyncSession, user: discord.abc.User) -> Viewer:
    member, created = await find_or_create_member(
        session,
        "discord",
        str(user.id),
        label=user.name,
        display_name=getattr(user, "display_name", None) or user.name,
        avatar_url=str(user.display_avatar.url) if user.display_avatar else None,
    )
    if isinstance(user, discord.Member) and hall.settings.discord_role_pairs:
        await sync_roles(session, member.id, _mapped_roles(user), "discord")
    if created:
        member.onboarded = True  # Discord users already have a name they chose
    return await build_viewer(session, member, "discord")


async def viewer_for(interaction: discord.Interaction, session: AsyncSession) -> Viewer:
    return await _member_and_viewer(session, interaction.user)


async def guard(interaction: discord.Interaction, session: AsyncSession, perm: str) -> Viewer | None:
    """Return the viewer if they may do ``perm``; otherwise reply privately and return None."""
    viewer = await viewer_for(interaction, session)
    if viewer.can(perm):
        return viewer
    p = hall.perms.perms.get(perm)
    msg = f"Your rank ({viewer.rank}) can't do that yet"
    if p:
        msg += f": *{p.label}* needs {p.min_rank}" + (f" or the {', '.join(p.duties)} duty" if p.duties else "")
    await reply(interaction, msg + ".")
    return None


def module_open(interaction_module: str) -> bool:
    return hall.is_enabled(interaction_module)


async def reply(
    interaction: discord.Interaction, content: str | None = None, *, ephemeral: bool = True, **kwargs: Any
) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content=content, ephemeral=ephemeral, **kwargs)
    else:
        await interaction.response.send_message(content=content, ephemeral=ephemeral, **kwargs)


async def sync_discord_member(member: discord.Member) -> None:
    if not hall.settings.discord_role_pairs:
        return
    async with session_scope() as session:
        await _member_and_viewer(session, member)


def clip(text: str | None, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[: n - 1] + "…"
