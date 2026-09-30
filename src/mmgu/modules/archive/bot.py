"""/item, /drops, /catalog and the right-click "Catalog item" command."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from sqlalchemy.ext.asyncio import AsyncSession

from mmgu.bot.helpers import GOLD, button, cid, clip, embed, guard, reply, view, web_url
from mmgu.core.audit import record
from mmgu.core.hall import hall
from mmgu.core.models import Proposal
from mmgu.core.uploads import UploadError, save_image
from mmgu.db import session_scope, utcnow
from mmgu.modules.archive import services
from mmgu.modules.archive.models import Item
from mmgu.modules.archive.reading import read_screenshot, reader_available

log = logging.getLogger(__name__)
IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp")


async def item_embed(session: AsyncSession, item: Item) -> discord.Embed:
    drops = await services.drop_summary(session, item.id)
    e = embed(
        item.name,
        f"```\n{services.item_text(item) or 'No stats recorded'}\n```",
        url=web_url(f"/archive/items/{item.id}"),
        color=GOLD,
    )
    if drops:
        lines = []
        for d in drops[:6]:
            line = d["creature"] or "?"
            if d["zone"]:
                line += f" · {d['zone']}"
            if d["count"] > 1:
                line += f" ×{d['count']}"
            lines.append(line)
        e.add_field(name="Drops from", value=clip("\n".join(lines), 1024), inline=False)
    for c in hall.ext.get("item.discord_fields", hall.enabled_ids):
        try:
            for name, value in await c.fn(session, item):
                e.add_field(name=name, value=clip(value, 1024), inline=True)
        except Exception:  # noqa: BLE001
            log.exception("item.discord_fields contribution from %s failed", c.module)
    if item.status == "stale":
        e.description = "⚠ Marked outdated: a patch may have changed this item.\n" + (e.description or "")
    return e


def item_buttons(item: Item) -> discord.ui.View:
    items = [
        button("Open in web", url=web_url(f"/archive/items/{item.id}")),
        button("Report a drop", cid("archive", "dropform", item.id)),
    ]
    if hall.is_enabled("overlay"):
        items.append(button("Show on stream", cid("archive", "stream", item.id)))
    if hall.is_enabled("vault"):
        items.append(button("Request from bank", cid("vault", "reqform", item.id)))
    return view(*items)


async def item_autocomplete(_interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    async with session_scope() as session:
        names = await services.suggest_names(session, current, 25)
    return [app_commands.Choice(name=clip(n, 100), value=clip(n, 100)) for _, n in names]


async def place_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    async with session_scope() as session:
        creatures, zones = await services.known_places(session)
    ns = interaction.namespace
    pool = zones if getattr(ns, "zone", None) == current and current else creatures + zones
    cur = current.lower()
    return [app_commands.Choice(name=clip(p, 100), value=clip(p, 100)) for p in pool if cur in p.lower()][:25]


class DropModal(discord.ui.Modal, title="Report a drop"):
    creature = discord.ui.TextInput(label="Dropped by (creature)", required=False, max_length=120)
    zone = discord.ui.TextInput(label="Zone", required=False, max_length=120)
    note = discord.ui.TextInput(label="Note (optional)", required=False, max_length=200)

    def __init__(self, item_id: int):
        super().__init__(custom_id=cid("archive", "dropsubmit", item_id))
        self.item_id = item_id

    async def on_submit(self, interaction: discord.Interaction) -> None:  # handled by the component router
        pass


async def _start_catalog(
    interaction: discord.Interaction, attachment: discord.Attachment, creature: str | None, zone: str | None
) -> None:
    if not (attachment.content_type or "").startswith(IMAGE_TYPES):
        await reply(interaction, "That isn't a PNG, JPG or WebP image.")
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    async with session_scope() as session:
        viewer = await guard(interaction, session, "archive.catalog")
        if viewer is None:
            return
        try:
            up = await save_image(session, await attachment.read(), uploaded_by=viewer.id)
        except UploadError as e:
            await reply(interaction, str(e))
            return
        prop = await read_screenshot(session, viewer, up, creature=creature, zone=zone)
        await record(
            session, "archive.screenshot_read", actor_id=viewer.id, entity=prop, via="discord", summary=prop.summary
        )
        fields = prop.payload.get("item") or {}
        pid = prop.id
        existing = await services.find_by_name(session, fields["name"]) if fields.get("name") else None
    review = web_url(f"/archive/new?proposal={pid}")
    if prop.source == "manual" or not fields.get("name"):
        msg = (
            "I saved the screenshot, but there's no screenshot reader turned on"
            if not reader_available()
            else "I couldn't read that screenshot"
        ) + f". Fill in the item here: {review}"
        await reply(interaction, msg)
        return
    try:
        preview = services.item_text(Item(**{k: v for k, v in services.clean_fields(fields).items()}))
    except services.ArchiveError:
        preview = ""
    e = embed(f"Read: {fields.get('name')}", f"```\n{preview or 'No stats found'}\n```", url=review)
    if existing:
        e.add_field(
            name="Already cataloged",
            value=f"Saving adds your screenshot and drop to **{existing.name}**.",
            inline=False,
        )
    if creature or zone:
        e.add_field(name="Drop", value=f"{creature or '?'} · {zone or '?'}", inline=False)
    e.set_footer(text=f"Read by {prop.source}. Check it against your screenshot before saving.")
    await reply(
        interaction,
        embed=e,
        view=view(
            button("Save to Archive", cid("archive", "save", pid), style=discord.ButtonStyle.success),
            button("Fix on the web", url=review),
            button("Discard", cid("archive", "discard", pid), style=discord.ButtonStyle.danger),
        ),
    )


async def setup(bot) -> None:
    @app_commands.command(name="item", description="Look up an item in the guild Archive")
    @app_commands.describe(name="Item name (start typing)", private="Only show it to me")
    @app_commands.autocomplete(name=item_autocomplete)
    async def item_cmd(interaction: discord.Interaction, name: str, private: bool = False) -> None:
        async with session_scope() as session:
            viewer = await guard(interaction, session, "archive.view")
            if viewer is None:
                return
            item = await services.find_by_name(session, name)
            if item is None:
                sugg = await services.suggest_names(session, name, 5)
                hint = (
                    ("Did you mean: " + ", ".join(n for _, n in sugg) + "?")
                    if sugg
                    else "Nobody has cataloged it yet. Right-click a screenshot → Apps → Catalog item."
                )
                await reply(interaction, f"**{name}** isn't in the Archive. {hint}")
                return
            e = await item_embed(session, item)
        await reply(interaction, embed=e, view=item_buttons(item), ephemeral=private)

    @app_commands.command(name="drops", description="What drops from a creature or in a zone")
    @app_commands.autocomplete(creature=place_autocomplete, zone=place_autocomplete)
    async def drops_cmd(interaction: discord.Interaction, creature: str | None = None, zone: str | None = None) -> None:
        if not creature and not zone:
            await reply(interaction, "Give a creature, a zone, or both.")
            return
        async with session_scope() as session:
            viewer = await guard(interaction, session, "archive.view")
            if viewer is None:
                return
            rows = await services.loot_table(session, creature=creature or "", zone=zone or "")
        title = " · ".join(x for x in (creature, zone) if x)
        if not rows:
            await reply(interaction, f"No drops recorded for {title} yet.")
            return
        lines = [
            f"**{i.name}**{' · ' + ' '.join(i.slots) if i.slots else ''}{f' (×{n})' if n > 1 else ''}"
            for i, n in rows[:30]
        ]
        from urllib.parse import urlencode

        await reply(
            interaction,
            embed=embed(
                f"Loot: {title}",
                "\n".join(lines),
                url=web_url("/archive/loot?" + urlencode({"creature": creature or "", "zone": zone or ""})),
            ),
            ephemeral=False,
        )

    @app_commands.command(name="catalog", description="Add an item to the Archive from a screenshot")
    @app_commands.describe(
        screenshot="Screenshot of the item's inspect window", creature="What dropped it", zone="Where"
    )
    @app_commands.autocomplete(creature=place_autocomplete, zone=place_autocomplete)
    async def catalog_cmd(
        interaction: discord.Interaction,
        screenshot: discord.Attachment,
        creature: str | None = None,
        zone: str | None = None,
    ) -> None:
        await _start_catalog(interaction, screenshot, creature, zone)

    @app_commands.context_menu(name="Catalog item")
    async def catalog_menu(interaction: discord.Interaction, message: discord.Message) -> None:
        images = [a for a in message.attachments if (a.content_type or "").startswith(IMAGE_TYPES)]
        if not images:
            await reply(interaction, "That message has no screenshot attached.")
            return
        await _start_catalog(interaction, images[0], None, None)

    @bot.on_component("archive:save")
    async def on_save(interaction: discord.Interaction, args: list[str]) -> None:
        pid = int(args[0])
        async with session_scope() as session:
            viewer = await guard(interaction, session, "archive.catalog")
            if viewer is None:
                return
            prop = await session.get(Proposal, pid)
            if prop is None or prop.status != "pending":
                await reply(interaction, "That one was already saved or discarded.")
                return
            fields = prop.payload.get("item") or {}
            try:
                item = await services.create_item(session, viewer, fields, via="discord", upload_id=prop.upload_id)
                verb = "cataloged"
            except services.DuplicateItem as dup:
                item = dup.item
                if prop.upload_id:
                    await services.add_image(session, viewer, item, prop.upload_id)
                verb = "was already cataloged; your screenshot was added to"
            except services.ArchiveError as e:
                await reply(
                    interaction, f"{e} Fix it on the web: {web_url(prop.payload.get('review_url', '/proposals'))}"
                )
                return
            await services.add_drop(
                session,
                viewer,
                item,
                creature=prop.payload.get("creature"),
                zone=prop.payload.get("zone"),
                source="screenshot",
                via="discord",
            )
            prop.status, prop.decided_by, prop.decided_at = "accepted", viewer.id, utcnow()
            prop.result_ref = f"archive.item:{item.id}"
            item_id, item_name = item.id, item.name
        await interaction.response.edit_message(
            content=f"Saved. **{item_name}** {verb} the Archive. {web_url(f'/archive/items/{item_id}')}",
            embed=None,
            view=view(button("Report where it dropped", cid("archive", "dropform", item_id))),
        )

    @bot.on_component("archive:discard")
    async def on_discard(interaction: discord.Interaction, args: list[str]) -> None:
        async with session_scope() as session:
            viewer = await guard(interaction, session, "archive.catalog")
            if viewer is None:
                return
            prop = await session.get(Proposal, int(args[0]))
            if prop is not None and prop.status == "pending":
                prop.status, prop.decided_by, prop.decided_at = "rejected", viewer.id, utcnow()
        await interaction.response.edit_message(content="Discarded.", embed=None, view=None)

    @bot.on_component("archive:dropform")
    async def on_dropform(interaction: discord.Interaction, args: list[str]) -> None:
        await interaction.response.send_modal(DropModal(int(args[0])))

    @bot.on_component("archive:dropsubmit")
    async def on_dropsubmit(interaction: discord.Interaction, args: list[str]) -> None:
        values = {
            c["components"][0]["custom_id"]: c["components"][0].get("value")
            for c in interaction.data.get("components", [])
        }
        vals = list(values.values())
        creature, zone, note = (vals + [None, None, None])[:3]
        async with session_scope() as session:
            viewer = await guard(interaction, session, "archive.catalog")
            if viewer is None:
                return
            item = await session.get(Item, int(args[0]))
            if item is None:
                await reply(interaction, "That item is gone.")
                return
            rep = await services.add_drop(session, viewer, item, creature=creature, zone=zone, note=note, via="discord")
            name = item.name
        await reply(
            interaction,
            f"Recorded: **{name}** from {creature or '?'} in {zone or '?'}."
            if rep
            else "Give at least a creature or a zone.",
        )

    @bot.on_component("archive:stream")
    async def on_stream(interaction: discord.Interaction, args: list[str]) -> None:
        async with session_scope() as session:
            viewer = await guard(interaction, session, "overlay.push")
            if viewer is None:
                return
        await hall.bus.emit("overlay.push", actor_id=viewer.id, kind="item", item_id=int(args[0]))
        await reply(interaction, "On stream.")

    bot.add_command(item_cmd)
    bot.add_command(drops_cmd)
    bot.add_command(catalog_cmd)
    bot.add_command(catalog_menu)
