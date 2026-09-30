"""/event create, /events, /loot award, /loot history, and the Going / Maybe / Can't buttons."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import discord
from discord import app_commands
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmgu.bot.helpers import BLOOD, GOLD, TIDE, button, cid, clip, embed, guard, reply, view, web_url
from mmgu.core.hall import hall
from mmgu.core.models import Character, Identity, Member
from mmgu.db import session_scope
from mmgu.modules.events import services
from mmgu.modules.events.models import Event
from mmgu.modules.events.when import WhenError, parse_when

log = logging.getLogger(__name__)
STATUS_COLOR = {"scheduled": GOLD, "active": TIDE, "done": 0x8A8170, "cancelled": BLOOD}


def ts(value: datetime, style: str = "F") -> str:
    """A Discord timestamp: every reader sees it in their own time zone."""
    return f"<t:{int(value.replace(tzinfo=UTC).timestamp())}:{style}>"


async def event_embed(session: AsyncSession, ev: Event) -> discord.Embed:
    ros = await services.roster(session, ev)
    e = embed(
        f"{ev.kind}: {ev.title}",
        clip(ev.description, 1500) if ev.description else None,
        url=web_url(f"/events/{ev.id}"),
        color=STATUS_COLOR.get(ev.status, GOLD),
    )
    when = f"{ts(ev.starts_at)} ({ts(ev.starts_at, 'R')})"
    if ev.ends_at:
        when += f"\nuntil {ts(ev.ends_at, 't')}"
    e.add_field(name="When", value=when, inline=False)
    if ev.zone:
        e.add_field(name="Where", value=clip(ev.zone, 1024), inline=True)
    if ev.leader_id:
        leader = await session.get(Member, ev.leader_id)
        if leader:
            e.add_field(name="Leader", value=leader.display_name, inline=True)
    if ev.min_level or ev.max_level:
        e.add_field(name="Levels", value=f"{ev.min_level or 1}–{ev.max_level or hall.game.level_cap}", inline=True)
    going = f"{ros.going}" + (f"/{ev.capacity}" if ev.capacity else "")
    lines = []
    for g in ros.groups:
        if not g.rows and not g.want:
            continue
        names = ", ".join(
            (
                f"{r.character.name} ({hall.game.class_abbr(r.character.class_name)})"
                if r.character
                else r.member.display_name
            )
            + (" (late)" if r.signup.status == "late" else "")
            for r in g.rows
            if not r.bench
        )
        count = f"{g.have}/{g.want}" if g.want else f"{g.have}"
        warn = " ⚠" if g.short else ""
        lines.append(f"**{g.label} {count}**{warn}" + (f": {names}" if names else ""))
    e.add_field(
        name=f"Going ({going})", value=clip("\n".join(lines) or "Nobody yet. Be the first!", 1024), inline=False
    )
    if ros.bench:
        e.add_field(
            name=f"Bench ({len(ros.bench)})",
            value=clip(", ".join(r.character.name if r.character else r.member.display_name for r in ros.bench), 1024),
            inline=False,
        )
    if ros.maybe:
        e.add_field(
            name=f"Maybe ({len(ros.maybe)})",
            value=clip(", ".join(r.character.name if r.character else r.member.display_name for r in ros.maybe), 1024),
            inline=False,
        )
    status_text = {"active": "Happening now", "done": "Finished", "cancelled": "CANCELLED"}.get(ev.status)
    e.set_footer(text=(f"{status_text} · " if status_text else "") + f"{hall.app_name} · {hall.guild_name}")
    return e


def event_view(ev: Event) -> discord.ui.View:
    closed = ev.status in ("done", "cancelled")
    return view(
        button("Going", cid("events", "rsvp", ev.id, "going"), style=discord.ButtonStyle.success, disabled=closed),
        button("Maybe", cid("events", "rsvp", ev.id, "maybe"), disabled=closed),
        button("Can't", cid("events", "rsvp", ev.id, "cant"), style=discord.ButtonStyle.danger, disabled=closed),
        button("Open", url=web_url(f"/events/{ev.id}")),
    )


class CreateModal(discord.ui.Modal, title="Schedule an event"):
    ev_title = discord.ui.TextInput(
        label="Title", custom_id="title", max_length=120, placeholder="Scarwood group night"
    )
    when = discord.ui.TextInput(
        label="When",
        custom_id="when",
        max_length=80,
        placeholder="tomorrow 8pm · saturday 20:00 CDT · in 3 hours",
    )
    zone = discord.ui.TextInput(label="Zone or meeting place", custom_id="zone", required=False, max_length=120)
    kind = discord.ui.TextInput(
        label="Kind: Raid, Group, Crafting, Social or Other",
        custom_id="kind",
        required=False,
        max_length=20,
        default="Raid",
    )
    description = discord.ui.TextInput(
        label="Details",
        custom_id="description",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1500,
    )

    def __init__(self) -> None:
        super().__init__(custom_id=cid("events", "create"))

    async def on_submit(self, interaction: discord.Interaction) -> None:  # handled by the component router
        pass


def _modal_values(interaction: discord.Interaction) -> dict[str, str]:
    out = {}
    for row in (interaction.data or {}).get("components", []):
        for c in row.get("components", []):
            out[c.get("custom_id")] = (c.get("value") or "").strip()
    return out


async def _member_for_discord_user(session: AsyncSession, user: discord.abc.User) -> Member | None:
    mid = (
        await session.execute(
            select(Identity.member_id).where(Identity.provider == "discord", Identity.subject == str(user.id))
        )
    ).scalar_one_or_none()
    return await session.get(Member, mid) if mid else None


async def item_autocomplete(_interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    async with session_scope() as session:
        names = await services.item_names(session, current, 25)
    return [app_commands.Choice(name=clip(n, 100), value=clip(n, 100)) for n in names]


async def character_autocomplete(_interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    from mmgu.modules.roster.services import list_characters

    async with session_scope() as session:
        rows = await list_characters(session, q=current, mules=False, limit=25)
    return [
        app_commands.Choice(
            name=clip(f"{c.name} · {c.level or '?'} {c.class_name or ''}" + (f" · {m.display_name}" if m else ""), 100),
            value=c.name,
        )
        for c, m in rows
    ]


async def event_autocomplete(_interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    async with session_scope() as session:
        events = await services.upcoming(session, limit=10) + await services.past(session, limit=15)
    cur = current.lower()
    return [
        app_commands.Choice(name=clip(f"{e.title} · {e.starts_at:%b %d}", 100), value=str(e.id))
        for e in events
        if cur in e.title.lower()
    ][:25]


async def setup(bot) -> None:
    event_group = app_commands.Group(name="event", description="Schedule guild events")

    @event_group.command(name="create", description="Schedule a raid, group night or crafting party")
    async def event_create(interaction: discord.Interaction) -> None:
        async with session_scope() as session:
            viewer = await guard(interaction, session, "events.manage")
            if viewer is None:
                return
        await interaction.response.send_modal(CreateModal())

    @bot.on_component("events:create")
    async def on_create(interaction: discord.Interaction, _args: list[str]) -> None:
        vals = _modal_values(interaction)
        async with session_scope() as session:
            viewer = await guard(interaction, session, "events.manage")
            if viewer is None:
                return
            tz = services.member_tz(viewer.member)
            try:
                starts = parse_when(vals.get("when", ""), tz=tz)
            except WhenError as e:
                note = "" if tz else " Times without a zone are read as UTC; set your time zone on your hall profile."
                await reply(interaction, f"{e}{note}\n\nYour title was: {vals.get('title', '')}")
                return
            if starts <= datetime.now(UTC).replace(tzinfo=None):
                await reply(interaction, f"{ts(starts)} is in the past. Try again with a future time.")
                return
            try:
                events = await services.create_event(
                    session,
                    viewer,
                    {
                        "title": vals.get("title"),
                        "kind": vals.get("kind") or "Raid",
                        "starts_at": starts,
                        "zone": vals.get("zone"),
                        "description": vals.get("description"),
                        "discord_channel_id": str(interaction.channel_id or ""),
                    },
                    via="discord",
                )
            except services.EventError as e:
                await reply(interaction, str(e))
                return
            ev = events[0]
            emb = await event_embed(session, ev)
            v = event_view(ev)
            event_id = ev.id
        try:
            await interaction.response.send_message(embed=emb, view=v)
            msg = await interaction.original_response()
            channel_id, message_id = str(msg.channel.id), str(msg.id)
        except discord.HTTPException:
            log.warning("could not post event %s where it was created", event_id)
            channel_id = message_id = None
        async with session_scope() as session:
            ev = await session.get(Event, event_id)
            ev.discord_channel_id, ev.discord_message_id = channel_id, message_id

    @app_commands.command(name="events", description="What's coming up on the War Table")
    async def events_cmd(interaction: discord.Interaction) -> None:
        async with session_scope() as session:
            viewer = await guard(interaction, session, "events.view")
            if viewer is None:
                return
            events = await services.upcoming(session, limit=10)
            counts = await services.signup_counts(session, [e.id for e in events])
            mine = {}
            for e in events:
                s = await services.my_signup(session, e.id, viewer.id)
                if s:
                    mine[e.id] = services.SIGNUP_LABEL[s.status]
        if not events:
            await reply(
                interaction,
                f"Nothing scheduled yet. Officers can add one with `/event create` or at {web_url('/events')}",
            )
            return
        lines = []
        for e in events:
            c = counts[e.id]
            line = (
                f"**[{e.title}]({web_url(f'/events/{e.id}')})** · {e.kind}\n{ts(e.starts_at)} ({ts(e.starts_at, 'R')})"
            )
            if e.zone:
                line += f" · {e.zone}"
            line += f" · {c.get('going', 0) + c.get('late', 0)} going"
            if e.id in mine:
                line += f" · you: **{mine[e.id]}**"
            if e.status == "active":
                line = "🔴 " + line
            lines.append(line)
        await reply(interaction, embed=embed("Coming up", "\n\n".join(lines), url=web_url("/events")))

    @bot.on_component("events:rsvp")
    async def on_rsvp(interaction: discord.Interaction, args: list[str]) -> None:
        event_id, status = int(args[0]), args[1]
        async with session_scope() as session:
            viewer = await guard(interaction, session, "events.signup")
            if viewer is None:
                return
            ev = await session.get(Event, event_id)
            if ev is None:
                await reply(interaction, "That event was removed.")
                return
            try:
                s = await services.signup(session, viewer, ev, status=status, via="discord")
            except services.EventError as e:
                msg = str(e)
                if "Muster Roll" in msg:
                    msg = (
                        "You don't have a character on the roster yet. "
                        "Add your main with `/char add`, then press the button again."
                    )
                await reply(interaction, msg)
                return
            char = await session.get(Character, s.character_id) if s.character_id else None
            title = ev.title
        label = services.SIGNUP_LABEL[s.status]
        who = f" as **{char.name}** ({services.role_label(s.role).lower()})" if char and s.status != "cant" else ""
        await reply(
            interaction,
            f"You're **{label}** for {title}{who}. "
            f"Change character or role on the web: {web_url(f'/events/{event_id}#signup')}",
        )

    loot_group = app_commands.Group(name="loot", description="Loot awards")

    @loot_group.command(name="award", description="Record who received an item")
    @app_commands.describe(
        item="Item name (Archive items are suggested)",
        character="Character who received it",
        method="How it was decided",
        points="Points spent, if your guild uses points",
        event="Event it dropped at (defaults to the one happening now)",
        note="Anything worth remembering",
    )
    @app_commands.autocomplete(item=item_autocomplete, character=character_autocomplete, event=event_autocomplete)
    @app_commands.choices(method=[app_commands.Choice(name=m, value=m) for m in services.LOOT_METHODS])
    async def loot_award(
        interaction: discord.Interaction,
        item: str,
        character: str,
        method: app_commands.Choice[str] | None = None,
        points: float | None = None,
        event: str | None = None,
        note: str | None = None,
    ) -> None:
        async with session_scope() as session:
            viewer = await guard(interaction, session, "loot.award")
            if viewer is None:
                return
            event_id = int(event) if event and event.isdigit() else None
            if event_id is None and not event:
                active = [e for e in await services.upcoming(session, limit=20) if e.status == "active"]
                if len(active) == 1:
                    event_id = active[0].id
            try:
                a = await services.award_loot(
                    session,
                    viewer,
                    item=item,
                    character=character,
                    event_id=event_id,
                    method=method.value if method else "Roll",
                    points=points,
                    note=note,
                    via="discord",
                )
            except services.EventError as e:
                await reply(interaction, str(e))
                return
            ev = await session.get(Event, a.event_id) if a.event_id else None
            text = (
                f"**{a.item_name}** → **{a.character_name}** ({a.method}"
                + (f", {a.points:g} pts" if a.points is not None else "")
                + ")"
                + (f" at {ev.title}" if ev else "")
                + ("" if a.item_id else " · not in the Archive yet")
            )
        await reply(interaction, text, ephemeral=False)

    @loot_group.command(name="history", description="Who received what")
    @app_commands.describe(member="Only this member's loot", item="Only this item")
    @app_commands.autocomplete(item=item_autocomplete)
    async def loot_history(
        interaction: discord.Interaction, member: discord.Member | None = None, item: str | None = None
    ) -> None:
        async with session_scope() as session:
            viewer = await guard(interaction, session, "loot.view")
            if viewer is None:
                return
            member_id = None
            if member is not None:
                m = await _member_for_discord_user(session, member)
                if m is None:
                    await reply(interaction, f"{member.display_name} hasn't used the hall yet, so they have no loot.")
                    return
                member_id = m.id
            rows = await services.loot_history(session, member_id=member_id, item=item or "", limit=15)
        if not rows:
            await reply(interaction, "No loot recorded" + (" for that" if member or item else " yet") + ".")
            return
        lines = [
            f"**{a.item_name}** → {a.character_name} · {a.method}"
            + (f" {a.points:g}pts" if a.points is not None else "")
            + (f" · {e.title}" if e else "")
            + f" · {ts(a.awarded_at, 'd')}"
            for a, e, _m in rows
        ]
        title = "Loot" + (f": {member.display_name}" if member else "") + (f": {item}" if item else "")
        from urllib.parse import urlencode

        q = urlencode({k: v for k, v in (("member", member_id or ""), ("item", item or "")) if v})
        await reply(
            interaction, embed=embed(title, clip("\n".join(lines), 4000), url=web_url("/loot" + (f"?{q}" if q else "")))
        )

    bot.add_command(event_group)
    bot.add_command(events_cmd)
    bot.add_command(loot_group)
