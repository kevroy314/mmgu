"""/bank search, /bank request, /bank requests, and the buttons on posted bank requests."""

from __future__ import annotations

from urllib.parse import quote_plus

import discord
from discord import app_commands
from sqlalchemy.ext.asyncio import AsyncSession

from mmgu.bot.helpers import BLOOD, EMBER, GOLD, TIDE, button, cid, clip, embed, guard, reply, view, web_url
from mmgu.core.models import Character, Member
from mmgu.db import session_scope
from mmgu.modules.vault import services
from mmgu.modules.vault.models import Request

STATUS_COLOR = {"pending": EMBER, "approved": GOLD, "fulfilled": TIDE, "denied": BLOOD, "cancelled": BLOOD}
STATUS_LABEL = {
    "pending": "Waiting for a banker",
    "approved": "Approved, waiting to be handed over",
    "fulfilled": "Handed over",
    "denied": "Declined",
    "cancelled": "Cancelled",
}


def _modal_values(interaction: discord.Interaction) -> list[str | None]:
    return [c["components"][0].get("value") for c in (interaction.data or {}).get("components", [])]


async def request_embed(session: AsyncSession, req: Request) -> discord.Embed:
    requester = await session.get(Member, req.requester_id) if req.requester_id else None
    e = embed(
        f"Bank request: {req.qty} × {req.name}",
        req.reason or None,
        url=web_url(services.request_url(req)),
        color=STATUS_COLOR.get(req.status, GOLD),
    )
    e.add_field(name="Asked by", value=requester.display_name if requester else "?", inline=True)
    e.add_field(name="Status", value=STATUS_LABEL.get(req.status, req.status), inline=True)
    held = await services.holders_of(session, item_id=req.item_id, name=req.name)
    if req.status in services.OPEN_STATUSES:
        e.add_field(
            name="In the Vault",
            value=", ".join(f"{c.name} ({h.qty})" for h, c in held[:5]) if held else "None on the bank mules",
            inline=False,
        )
    if req.decision_note:
        e.add_field(name="Note", value=clip(req.decision_note, 1000), inline=False)
    return e


def request_buttons(req: Request) -> discord.ui.View:
    items = [button("Open", url=web_url(services.request_url(req)))]
    if req.status == "pending":
        items.insert(0, button("Approve", cid("vault", "approve", req.id), style=discord.ButtonStyle.success))
        items.insert(1, button("Deny", cid("vault", "deny", req.id), style=discord.ButtonStyle.danger))
    if req.status in services.OPEN_STATUSES:
        items.insert(-1, button("Hand over", cid("vault", "fulfil", req.id), style=discord.ButtonStyle.primary))
    return view(*items)


async def item_autocomplete(_interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    from mmgu.modules.archive.services import suggest_names

    async with session_scope() as session:
        names = [n for _, n in await suggest_names(session, current, 25)]
        held = [t["name"] for t in services.totals(await services.holdings(session, q=current))]
    seen: list[str] = []
    for n in held + names:
        if n not in seen:
            seen.append(n)
    return [app_commands.Choice(name=clip(n, 100), value=clip(n, 100)) for n in seen[:25]]


class RequestModal(discord.ui.Modal, title="Request from the guild bank"):
    qty = discord.ui.TextInput(label="How many?", default="1", max_length=6)
    reason = discord.ui.TextInput(
        label="What's it for? (optional)", required=False, max_length=500, style=discord.TextStyle.paragraph
    )

    def __init__(self, item_id: int, item_name: str):
        super().__init__(custom_id=cid("vault", "reqsubmit", item_id), title=clip(f"Request: {item_name}", 45))

    async def on_submit(self, interaction: discord.Interaction) -> None:  # handled by the component router
        pass


class DenyModal(discord.ui.Modal, title="Decline this request"):
    note = discord.ui.TextInput(
        label="Reason (sent to them)", required=False, max_length=300, style=discord.TextStyle.paragraph
    )

    def __init__(self, request_id: int):
        super().__init__(custom_id=cid("vault", "denysubmit", request_id))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        pass


async def _refresh_message(interaction: discord.Interaction, req_id: int) -> None:
    """Update the posted request card after a button press (if the button was on it)."""
    async with session_scope() as session:
        req = await session.get(Request, req_id)
        if req is None:
            return
        emb = await request_embed(session, req)
        v = request_buttons(req)
    msg = interaction.message
    if msg is not None and msg.embeds and msg.embeds[0].title and msg.embeds[0].title.startswith("Bank request"):
        try:
            await msg.edit(embed=emb, view=v)
        except discord.HTTPException:
            pass


async def setup(bot) -> None:
    bank = app_commands.Group(name="bank", description="The guild bank (the Vault)")

    @bank.command(name="search", description="Which bank mule holds an item, and how many")
    @app_commands.describe(item="Item name, or part of it")
    @app_commands.autocomplete(item=item_autocomplete)
    async def bank_search(interaction: discord.Interaction, item: str) -> None:
        async with session_scope() as session:
            viewer = await guard(interaction, session, "vault.view")
            if viewer is None:
                return
            tots = services.totals(await services.holdings(session, q=item))
        if not tots:
            await reply(interaction, f"Nothing matching **{item}** in the guild bank.")
            return
        lines = [
            f"**{t['name']}** × {t['qty']} · " + ", ".join(f"{c.name} ({n})" for c, n in t["mules"][:4])
            for t in tots[:20]
        ]
        if len(tots) > 20:
            lines.append(f"…and {len(tots) - 20} more")
        items = [button("Open the Vault", url=web_url("/vault?q=" + quote_plus(item)))]
        if len(tots) == 1 and tots[0]["item_id"]:
            items.insert(0, button("Request it", cid("vault", "reqform", tots[0]["item_id"])))
        await reply(interaction, embed=embed(f"In the Vault: {item}", "\n".join(lines)), view=view(*items))

    @bank.command(name="request", description="Ask the bankers for an item from the guild bank")
    @app_commands.describe(item="Item name (start typing)", qty="How many", reason="What it's for")
    @app_commands.autocomplete(item=item_autocomplete)
    async def bank_request(
        interaction: discord.Interaction,
        item: str,
        qty: app_commands.Range[int, 1, 10000] = 1,
        reason: str | None = None,
    ) -> None:
        async with session_scope() as session:
            viewer = await guard(interaction, session, "vault.request")
            if viewer is None:
                return
            try:
                req = await services.create_request(session, viewer, name=item, qty=qty, reason=reason, via="discord")
            except services.VaultError as e:
                await reply(interaction, str(e))
                return
            held = await services.held_qty(session, item_id=req.item_id, name=req.name)
            rid, name, n = req.id, req.name, req.qty
        stock = f"The bank holds {held}." if held else "Heads up: none is recorded on the bank mules right now."
        await reply(
            interaction,
            f"Asked the bankers for **{n} × {name}**. {stock} You'll get a DM when they decide.",
            view=view(button("Track it", url=web_url(f"/vault/requests/{rid}"))),
        )

    @bank.command(name="requests", description="Bank requests waiting for a banker")
    async def bank_requests(interaction: discord.Interaction) -> None:
        async with session_scope() as session:
            viewer = await guard(interaction, session, "vault.manage")
            if viewer is None:
                return
            rows = await services.list_requests(session, status="open", limit=15)
            lines = []
            for r, m in rows:
                held = await services.held_qty(session, item_id=r.item_id, name=r.name)
                lines.append(
                    f"`#{r.id}` **{r.qty} × {r.name}** for {m.display_name if m else '?'}"
                    f" · {r.status} · bank has {held}"
                )
        if not lines:
            await reply(interaction, "No open bank requests. All caught up.")
            return
        await reply(
            interaction,
            embed=embed("Open bank requests", "\n".join(lines), url=web_url("/vault/requests")),
        )

    async def _decide(interaction: discord.Interaction, req_id: int, action: str, note: str | None = None) -> None:
        async with session_scope() as session:
            viewer = await guard(interaction, session, "vault.manage")
            if viewer is None:
                return
            req = await session.get(Request, req_id)
            if req is None:
                await reply(interaction, "That request is gone.")
                return
            try:
                if action == "approve":
                    await services.approve_request(session, viewer, req, via="discord")
                else:
                    await services.deny_request(session, viewer, req, note, via="discord")
            except services.VaultError as e:
                await reply(interaction, str(e))
                return
            text = f"{'Approved' if action == 'approve' else 'Declined'} {req.qty} × {req.name}."
        await reply(interaction, text)
        await _refresh_message(interaction, req_id)

    @bot.on_component("vault:approve")
    async def on_approve(interaction: discord.Interaction, args: list[str]) -> None:
        await _decide(interaction, int(args[0]), "approve")

    @bot.on_component("vault:deny")
    async def on_deny(interaction: discord.Interaction, args: list[str]) -> None:
        async with session_scope() as session:
            if await guard(interaction, session, "vault.manage") is None:
                return
        await interaction.response.send_modal(DenyModal(int(args[0])))

    @bot.on_component("vault:denysubmit")
    async def on_deny_submit(interaction: discord.Interaction, args: list[str]) -> None:
        note = (_modal_values(interaction) + [None])[0]
        await _decide(interaction, int(args[0]), "deny", note)

    @bot.on_component("vault:fulfil")
    async def on_fulfil(interaction: discord.Interaction, args: list[str]) -> None:
        req_id = int(args[0])
        async with session_scope() as session:
            viewer = await guard(interaction, session, "vault.manage")
            if viewer is None:
                return
            req = await session.get(Request, req_id)
            if req is None or req.status not in services.OPEN_STATUSES:
                await reply(interaction, "That request is already closed.")
                return
            holders = await services.holders_of(session, item_id=req.item_id, name=req.name)
            name, qty = req.name, req.qty
        if not holders:
            await reply(
                interaction,
                f"No bank mule holds {name}. Deposit it first on the web, or decline the request.",
                view=view(button("Open request", url=web_url(f"/vault/requests/{req_id}"))),
            )
            return
        select = discord.ui.Select(
            custom_id=cid("vault", "fulfilpick", req_id),
            placeholder="Withdraw from which mule?",
            options=[
                discord.SelectOption(
                    label=clip(c.name, 100),
                    value=str(c.id),
                    description=f"holds {h.qty}" + ("" if h.qty >= qty else f" (short of {qty})"),
                )
                for h, c in holders[:25]
            ],
        )
        await reply(interaction, f"Hand over {qty} × **{name}**. Pick the mule it comes from:", view=view(select))

    @bot.on_component("vault:fulfilpick")
    async def on_fulfil_pick(interaction: discord.Interaction, args: list[str]) -> None:
        req_id = int(args[0])
        values = (interaction.data or {}).get("values") or []
        async with session_scope() as session:
            viewer = await guard(interaction, session, "vault.manage")
            if viewer is None:
                return
            req = await session.get(Request, req_id)
            mule = await session.get(Character, int(values[0])) if values else None
            if req is None or mule is None:
                await reply(interaction, "That request or mule is gone.")
                return
            try:
                tx = await services.fulfil_request(session, viewer, req, mule, via="discord")
            except services.VaultError as e:
                await reply(interaction, str(e))
                return
            text = f"Done. Withdrew {abs(tx.delta)} × {req.name} from {mule.name} ({tx.qty_after} left)."
            emb = await request_embed(session, req)
            ch, mid, v = req.discord_channel_id, req.discord_message_id, request_buttons(req)
        await interaction.response.edit_message(content=text, view=None)
        from mmgu.core.hall import hall

        if hall.discord is not None:
            await hall.discord.edit(ch, mid, embed=emb, view=v)

    @bot.on_component("vault:reqform")
    async def on_reqform(interaction: discord.Interaction, args: list[str]) -> None:
        from mmgu.modules.archive.models import Item

        item_id = int(args[0])
        async with session_scope() as session:
            if await guard(interaction, session, "vault.request") is None:
                return
            item = await session.get(Item, item_id)
            name = item.name if item else "item"
        if item is None:
            await reply(interaction, "That item isn't in the Archive any more.")
            return
        await interaction.response.send_modal(RequestModal(item_id, name))

    @bot.on_component("vault:reqsubmit")
    async def on_reqsubmit(interaction: discord.Interaction, args: list[str]) -> None:
        qty, reason = (_modal_values(interaction) + [None, None])[:2]
        async with session_scope() as session:
            viewer = await guard(interaction, session, "vault.request")
            if viewer is None:
                return
            try:
                req = await services.create_request(
                    session, viewer, name="", item_id=int(args[0]), qty=qty or 1, reason=reason, via="discord"
                )
            except services.VaultError as e:
                await reply(interaction, str(e))
                return
            rid, name, n = req.id, req.name, req.qty
        await reply(
            interaction,
            f"Asked the bankers for **{n} × {name}**. You'll get a DM when they decide.",
            view=view(button("Track it", url=web_url(f"/vault/requests/{rid}"))),
        )

    bot.add_command(bank)
