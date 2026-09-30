"""/wts, /wtb, /request and /board, plus the Claim / Done / Close buttons on posted cards."""

from __future__ import annotations

import discord
from discord import app_commands
from sqlalchemy.ext.asyncio import AsyncSession

from mmgu.bot.helpers import BLOOD, EMBER, GOLD, TIDE, button, cid, clip, embed, guard, reply, view, web_url
from mmgu.core.models import Member
from mmgu.db import session_scope
from mmgu.modules.board import services
from mmgu.modules.board.models import BoardRequest, Listing

ARCANE = 0x9FC6F0
REQ_COLOR = {"open": EMBER, "claimed": GOLD, "done": TIDE, "closed": BLOOD}
REQ_LABEL = {"open": "Open, needs a helper", "claimed": "Claimed", "done": "Done", "closed": "Closed"}


async def post_embed_and_view(
    session: AsyncSession, obj: Listing | BoardRequest
) -> tuple[discord.Embed, discord.ui.View]:
    author = await session.get(Member, obj.author_id) if obj.author_id else None
    if isinstance(obj, Listing):
        e = embed(
            f"{obj.kind}: {obj.title}" + (f" × {obj.qty}" if obj.qty > 1 else ""),
            obj.notes or None,
            url=web_url(services.listing_url(obj)),
            color=(TIDE if obj.kind == "WTS" else ARCANE) if obj.status == "open" else BLOOD,
        )
        e.add_field(name="Price", value=obj.price or "Make an offer", inline=True)
        e.add_field(name="Contact", value=author.display_name if author else "?", inline=True)
        if obj.server:
            e.add_field(name="Server", value=obj.server, inline=True)
        if obj.status != "open":
            e.add_field(name="Status", value=obj.status.capitalize(), inline=True)
        items = [button("Open", url=web_url(services.listing_url(obj)))]
        if obj.status == "open":
            items.append(button("Done (traded)", cid("board", "sold", obj.id), style=discord.ButtonStyle.success))
            items.append(button("Take down", cid("board", "lclose", obj.id)))
        return e, view(*items)
    helper = await session.get(Member, obj.claimed_by) if obj.claimed_by else None
    e = embed(
        f"{obj.category}: {obj.title}",
        obj.details or None,
        url=web_url(services.request_url(obj)),
        color=REQ_COLOR.get(obj.status, GOLD),
    )
    e.add_field(name="Asked by", value=author.display_name if author else "?", inline=True)
    status = REQ_LABEL.get(obj.status, obj.status)
    if helper:
        status += f" by {helper.display_name}"
    e.add_field(name="Status", value=status, inline=True)
    if obj.item_name:
        e.add_field(name="Item", value=obj.item_name, inline=True)
    if obj.tradeskill:
        e.add_field(name="Tradeskill", value=obj.tradeskill, inline=True)
    items = []
    if obj.status == "open":
        items.append(button("Claim", cid("board", "claim", obj.id), style=discord.ButtonStyle.primary))
    if obj.status in ("open", "claimed"):
        items.append(button("Done", cid("board", "done", obj.id), style=discord.ButtonStyle.success))
        items.append(button("Close", cid("board", "rclose", obj.id)))
    items.append(button("Open", url=web_url(services.request_url(obj))))
    return e, view(*items)


async def item_autocomplete(_interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    from mmgu.modules.archive.services import suggest_names

    async with session_scope() as session:
        names = await suggest_names(session, current, 25)
    out = [app_commands.Choice(name=clip(n, 100), value=clip(n, 100)) for _, n in names]
    if current and not any(c.value.lower() == current.lower() for c in out):
        out.insert(0, app_commands.Choice(name=clip(current, 100), value=clip(current, 100)))
    return out[:25]


async def _post_listing(
    interaction: discord.Interaction, kind: str, item: str, price: str | None, qty: int, note: str | None
) -> None:
    async with session_scope() as session:
        viewer = await guard(interaction, session, "board.post")
        if viewer is None:
            return
        try:
            li = await services.create_listing(
                session, viewer, kind=kind, item=item, qty=qty, price=price, notes=note, via="discord"
            )
        except services.BoardError as e:
            await reply(interaction, str(e))
            return
        other = "WTS" if kind == "WTB" else "WTB"
        matches = await services.listings_for_item(
            session, item_id=li.item_id, name=li.title, kind=other, exclude_id=li.id
        )
        bank = await services.vault_holders(session, item_id=li.item_id, name=li.title) if kind == "WTB" else []
        lid, title = li.id, li.title
    text = f"Posted **{kind} {title}** to the Notice Board for {services.listing_days()} days."
    if matches:
        text += f" {len(matches)} {'seller' if kind == 'WTB' else 'buyer'}(s) already listed it; you've both been told."
    if bank:
        text += f" The guild bank holds {sum(h.qty for h, _ in bank)}. Try `/bank request`."
    await reply(interaction, text, view=view(button("Open", url=web_url(f"/board/listings/{lid}"))))


async def setup(bot) -> None:
    @app_commands.command(name="wts", description="Post that you want to sell an item")
    @app_commands.describe(item="Item (start typing)", price="e.g. 5pp or offer", qty="How many", note="Anything else")
    @app_commands.autocomplete(item=item_autocomplete)
    async def wts_cmd(
        interaction: discord.Interaction,
        item: str,
        price: str | None = None,
        qty: app_commands.Range[int, 1, 10000] = 1,
        note: str | None = None,
    ) -> None:
        await _post_listing(interaction, "WTS", item, price, qty, note)

    @app_commands.command(name="wtb", description="Post that you want to buy an item")
    @app_commands.describe(
        item="Item (start typing)", price="What you'll pay, e.g. 5pp", qty="How many", note="Anything else"
    )
    @app_commands.autocomplete(item=item_autocomplete)
    async def wtb_cmd(
        interaction: discord.Interaction,
        item: str,
        price: str | None = None,
        qty: app_commands.Range[int, 1, 10000] = 1,
        note: str | None = None,
    ) -> None:
        await _post_listing(interaction, "WTB", item, price, qty, note)

    @app_commands.command(name="request", description="Ask the guild for help: crafting, a port, buffs, a corpse run…")
    @app_commands.describe(category="What kind of help", title="Short summary", details="Where, when, what you need")
    @app_commands.choices(category=[app_commands.Choice(name=c, value=c) for c in services.CATEGORIES])
    async def request_cmd(
        interaction: discord.Interaction,
        category: app_commands.Choice[str],
        title: str,
        details: str | None = None,
    ) -> None:
        async with session_scope() as session:
            viewer = await guard(interaction, session, "board.post")
            if viewer is None:
                return
            try:
                r = await services.create_request(
                    session, viewer, category=category.value, title=title, details=details, via="discord"
                )
            except services.BoardError as e:
                await reply(interaction, str(e))
                return
            rid = r.id
        await reply(
            interaction,
            f"Posted to the Notice Board: **{title}**. You'll get a DM when someone claims it.",
            view=view(button("Open", url=web_url(f"/board/requests/{rid}"))),
        )

    @app_commands.command(name="board", description="What's open on the Notice Board")
    @app_commands.describe(show="Which posts", search="Filter by item or title")
    @app_commands.choices(
        show=[
            app_commands.Choice(name="Everything", value="all"),
            app_commands.Choice(name="Selling (WTS)", value="wts"),
            app_commands.Choice(name="Buying (WTB)", value="wtb"),
            app_commands.Choice(name="Requests for help", value="requests"),
        ]
    )
    async def board_cmd(
        interaction: discord.Interaction, show: app_commands.Choice[str] | None = None, search: str | None = None
    ) -> None:
        which = show.value if show else "all"
        async with session_scope() as session:
            viewer = await guard(interaction, session, "board.view")
            if viewer is None:
                return
            lines = []
            if which in ("all", "wts", "wtb"):
                kind = which.upper() if which != "all" else None
                for li, m in await services.list_listings(session, kind=kind, q=search or "", limit=15):
                    lines.append(
                        f"`{li.kind}` **{li.title}**{f' ×{li.qty}' if li.qty > 1 else ''}"
                        f"{' · ' + li.price if li.price else ''} · {m.display_name if m else '?'}"
                    )
            if which in ("all", "requests"):
                for r, a, h in await services.list_requests(session, status="active", q=search or "", limit=10):
                    lines.append(
                        f"`{r.category}` **{r.title}** · {a.display_name if a else '?'}"
                        + (f" · claimed by {h.display_name}" if h else "")
                    )
        if not lines:
            await reply(interaction, "Nothing open on the Notice Board" + (f" matching {search}." if search else "."))
            return
        await reply(
            interaction,
            embed=embed("The Notice Board", "\n".join(lines[:25]), url=web_url(f"/board?tab={which}")),
        )

    async def _refresh(interaction: discord.Interaction, obj) -> None:
        async with session_scope() as session:
            fresh = await session.get(type(obj), obj.id)
            emb, v = await post_embed_and_view(session, fresh)
        msg = interaction.message
        if msg is not None:
            try:
                await msg.edit(embed=emb, view=v)
            except discord.HTTPException:
                pass

    async def _listing_action(interaction: discord.Interaction, listing_id: int, action: str) -> None:
        async with session_scope() as session:
            viewer = await guard(interaction, session, "board.post")
            if viewer is None:
                return
            li = await session.get(Listing, listing_id)
            if li is None:
                await reply(interaction, "That listing is gone.")
                return
            try:
                await services.set_listing_status(session, viewer, li, action, via="discord")
            except services.BoardError as e:
                await reply(interaction, str(e))
                return
        await reply(interaction, "Marked as done." if action == "sold" else "Taken down.")
        await _refresh(interaction, li)

    async def _request_action(interaction: discord.Interaction, request_id: int, action: str) -> None:
        async with session_scope() as session:
            viewer = await guard(interaction, session, "board.post")
            if viewer is None:
                return
            r = await session.get(BoardRequest, request_id)
            if r is None:
                await reply(interaction, "That request is gone.")
                return
            try:
                msg = await services.request_action(session, viewer, r, action, via="discord")
            except services.BoardError as e:
                await reply(interaction, str(e))
                return
        await reply(interaction, msg)
        await _refresh(interaction, r)

    @bot.on_component("board:sold")
    async def on_sold(interaction: discord.Interaction, args: list[str]) -> None:
        await _listing_action(interaction, int(args[0]), "sold")

    @bot.on_component("board:lclose")
    async def on_lclose(interaction: discord.Interaction, args: list[str]) -> None:
        await _listing_action(interaction, int(args[0]), "close")

    @bot.on_component("board:claim")
    async def on_claim(interaction: discord.Interaction, args: list[str]) -> None:
        await _request_action(interaction, int(args[0]), "claim")

    @bot.on_component("board:done")
    async def on_done(interaction: discord.Interaction, args: list[str]) -> None:
        await _request_action(interaction, int(args[0]), "done")

    @bot.on_component("board:rclose")
    async def on_rclose(interaction: discord.Interaction, args: list[str]) -> None:
        await _request_action(interaction, int(args[0]), "close")

    bot.add_command(wts_cmd)
    bot.add_command(wtb_cmd)
    bot.add_command(request_cmd)
    bot.add_command(board_cmd)
