"""/char and /roster commands."""

from __future__ import annotations

import discord
from discord import app_commands

from mmgu.bot.helpers import embed, guard, reply, web_url
from mmgu.core.hall import hall
from mmgu.db import session_scope
from mmgu.modules.roster import services


async def class_autocomplete(_interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    cur = current.lower()
    return [
        app_commands.Choice(name=c["name"], value=c["name"])
        for c in hall.game.classes
        if cur in c["name"].lower() or cur == c.get("abbr", "").lower()
    ][:25]


async def setup(bot) -> None:
    char = app_commands.Group(name="char", description="Your characters on the Muster Roll")

    @char.command(name="add", description="Add or update one of your characters")
    @app_commands.describe(name="Character name", class_name="Class", level="Level", main="Is this your main?")
    @app_commands.rename(class_name="class")
    @app_commands.autocomplete(class_name=class_autocomplete)
    async def char_add(
        interaction: discord.Interaction,
        name: str,
        class_name: str,
        level: app_commands.Range[int, 1, 100],
        main: bool = False,
    ) -> None:
        async with session_scope() as session:
            viewer = await guard(interaction, session, "hall.view")
            if viewer is None:
                return
            existing = await services.find_character(session, name, services.default_server())
            try:
                ch = await services.save_character(
                    session,
                    viewer,
                    name=name,
                    class_name=class_name,
                    level=level,
                    is_main=main,
                    race=existing.race if existing else None,
                    character=existing if existing and existing.member_id in (None, viewer.id) else None,
                    via="discord",
                )
            except services.RosterError as e:
                await reply(interaction, str(e))
                return
        await reply(
            interaction,
            f"**{ch.name}** is on the Muster Roll: level {ch.level} {ch.class_name}{' (main)' if ch.is_main else ''}.",
        )

    @char.command(name="level", description="Update a character's level")
    async def char_level(interaction: discord.Interaction, name: str, level: app_commands.Range[int, 1, 100]) -> None:
        async with session_scope() as session:
            viewer = await guard(interaction, session, "hall.view")
            if viewer is None:
                return
            ch = await services.find_character(session, name)
            if ch is None:
                await reply(interaction, f"No character called {name} on the roll. Add it with `/char add`.")
                return
            if not services.can_edit(viewer, ch):
                await reply(interaction, "That isn't your character.")
                return
            old = ch.level
            ch.level = level
        await reply(interaction, f"{ch.name}: level {old or '?'} → **{level}**. Grats!", ephemeral=False)

    @char.command(name="list", description="List someone's characters (yours by default)")
    async def char_list(interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        from sqlalchemy import select

        from mmgu.core.models import Identity

        async with session_scope() as session:
            viewer = await guard(interaction, session, "members.view")
            if viewer is None:
                return
            target_id = viewer.id
            if member is not None:
                target_id = (
                    await session.execute(
                        select(Identity.member_id).where(
                            Identity.provider == "discord", Identity.subject == str(member.id)
                        )
                    )
                ).scalar_one_or_none()
                if target_id is None:
                    await reply(interaction, f"{member.display_name} hasn't used the hall yet.")
                    return
            chars = await services.characters_of(session, target_id)
        who = member.display_name if member else "You"
        if not chars:
            await reply(
                interaction,
                f"{who} have no characters on the roll yet."
                if not member
                else f"{who} has no characters on the roll yet.",
            )
            return
        lines = [
            f"{'★ ' if c.is_main else ''}**{c.name}** · {c.level or '?'} {c.class_name or ''}"
            f"{' · bank' if c.is_bank_mule else ''}"
            for c in chars
        ]
        await reply(
            interaction,
            embed=embed(f"{who}: characters", "\n".join(lines), url=web_url(f"/roster/members/{target_id}")),
        )

    @app_commands.command(name="roster", description="Who plays a class, or a role, in the guild")
    @app_commands.rename(class_name="class")
    @app_commands.autocomplete(class_name=class_autocomplete)
    @app_commands.choices(
        role=[app_commands.Choice(name=r["label"], value=r["key"]) for r in hall.game.event_roles if r["key"] != "any"]
        or [app_commands.Choice(name="Any", value="")]
    )
    async def roster_cmd(
        interaction: discord.Interaction,
        class_name: str | None = None,
        role: app_commands.Choice[str] | None = None,
        min_level: int | None = None,
    ) -> None:
        async with session_scope() as session:
            viewer = await guard(interaction, session, "members.view")
            if viewer is None:
                return
            rows = await services.list_characters(
                session,
                class_name=class_name or "",
                role=role.value if role else "",
                min_level=min_level,
                mules=False,
                limit=40,
            )
        if not rows:
            await reply(interaction, "Nobody on the roll matches that.")
            return
        lines = [
            f"**{c.name}** {c.level or '?'} {hall.game.class_abbr(c.class_name)}"
            f"{' · ' + m.display_name if m and m.display_name != c.name else ''}"
            for c, m in rows
        ]
        title = "Roster" + (f": {class_name}" if class_name else "") + (f" ({role.name})" if role else "")
        await reply(interaction, embed=embed(title, "\n".join(lines)[:4000], url=web_url("/roster")))

    bot.add_command(char)
    bot.add_command(roster_cmd)
