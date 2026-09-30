"""/crafter set and /whocan."""

from __future__ import annotations

from urllib.parse import quote

import discord
from discord import app_commands
from sqlalchemy import select

from mmgu.bot.helpers import clip, embed, guard, reply, web_url
from mmgu.core.models import Character, Identity
from mmgu.db import session_scope
from mmgu.modules.crafters import services


async def skill_autocomplete(_interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    cur = current.lower().strip()
    names = services.skill_names()
    hits = [n for n in names if n.lower().startswith(cur)] + [
        n for n in names if cur in n.lower() and not n.lower().startswith(cur)
    ]
    return [app_commands.Choice(name=n, value=n) for n in hits[:25]]


async def my_character_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    async with session_scope() as session:
        member_id = (
            await session.execute(
                select(Identity.member_id).where(
                    Identity.provider == "discord", Identity.subject == str(interaction.user.id)
                )
            )
        ).scalar_one_or_none()
        if member_id is None:
            return []
        rows = (
            (
                await session.execute(
                    select(Character.name)
                    .where(Character.member_id == member_id, Character.status == "active")
                    .order_by(Character.is_main.desc(), Character.name)
                )
            )
            .scalars()
            .all()
        )
    cur = current.lower()
    return [app_commands.Choice(name=n, value=n) for n in rows if cur in n.lower()][:25]


async def setup(bot) -> None:
    crafter = app_commands.Group(name="crafter", description="Your tradeskills in the Crafters' Hall")

    @crafter.command(name="set", description="Record one of your characters' tradeskill level")
    @app_commands.describe(
        character="Your character",
        skill="Tradeskill",
        level="Skill level",
        specialty="What they're known for, e.g. 'can make Bronze weapons'",
    )
    @app_commands.autocomplete(character=my_character_autocomplete, skill=skill_autocomplete)
    async def crafter_set(
        interaction: discord.Interaction,
        character: str,
        skill: str,
        level: app_commands.Range[int, 0, 1000],
        specialty: str | None = None,
    ) -> None:
        from mmgu.modules.roster.services import characters_of, find_character

        async with session_scope() as session:
            viewer = await guard(interaction, session, "crafters.edit")
            if viewer is None:
                return
            mine = [c for c in await characters_of(session, viewer.id) if c.name.lower() == character.lower()]
            ch = mine[0] if mine else await find_character(session, character)
            if ch is None:
                await reply(interaction, f"No character called {character} on the roll. Add it with `/char add`.")
                return
            try:
                row = await services.set_skill(session, viewer, ch, skill, level, specialty, via="discord")
            except services.CraftError as e:
                await reply(interaction, str(e))
                return
            name = ch.name
            skill_name = services.canonical_skill(skill)
        if row is None:
            await reply(interaction, f"Removed {skill_name} from {name}.")
            return
        await reply(
            interaction,
            f"**{name}**: {row.skill} **{row.level}**" + (f" · {row.specialty}" if row.specialty else "") + ".",
        )

    @app_commands.command(name="whocan", description="Who in the guild can make an item, or has a tradeskill")
    @app_commands.describe(what="An item name or a tradeskill", private="Only show it to me")
    async def whocan_cmd(interaction: discord.Interaction, what: str, private: bool = False) -> None:
        async with session_scope() as session:
            viewer = await guard(interaction, session, "crafters.view")
            if viewer is None:
                return
            res = await services.who_can(session, what)
            url = web_url(f"/crafters/whocan?q={quote(what)}")
            if res.empty:
                await reply(
                    interaction,
                    f"Nobody found for **{what}**. If you know the recipe, add it: {web_url('/crafters/recipes/new')}",
                )
                return
            e = embed(f"Who can make {res.skill or what}?", url=url)
            if res.skill:
                lines = [f"**{c.character.name}** {c.row.level}" for c in res.skill_crafters[:25]]
                e.description = clip("\n".join(lines) or f"Nobody has recorded {res.skill} yet.", 4000)
            for r, _comps, crafters in res.recipes[:8]:
                names = ", ".join(f"{c.character.name} ({c.row.level})" for c in crafters[:10])
                title = f"{r.result_name} · {r.skill}{f' {r.trivial}' if r.trivial else ''}"
                e.add_field(name=clip(title, 256), value=clip(names or "Nobody skilled enough yet", 1024), inline=False)
            if res.specialists:
                e.add_field(
                    name="Specialties",
                    value=clip("\n".join(f"{c.character.name}: {c.row.specialty}" for c in res.specialists[:8]), 1024),
                    inline=False,
                )
        await reply(interaction, embed=e, ephemeral=private)

    bot.add_command(crafter)
    bot.add_command(whocan_cmd)
