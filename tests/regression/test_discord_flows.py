"""Drive the Discord bot like guild members would, without Discord."""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from mmgu.core.hall import hall
from mmgu.core.models import Identity, RoleGrant
from mmgu.db import session_scope
from tests.regression.discord_harness import (
    FakeAttachment,
    FakeBot,
    FakeInteraction,
    modal_data,
    press,
    run,
    sample_args,
    user,
)
from tests.regression.world import enable_all_modules, png, seed_world

LEADER, MEMBER, OTHER = user(9001, "Lyra"), user(9002, "Brannoc"), user(9003, "Sable")


async def _grant(discord_user, role: str) -> None:
    async with session_scope() as s:
        mid = (
            await s.execute(
                select(Identity.member_id).where(
                    Identity.provider == "discord", Identity.subject == str(discord_user.id)
                )
            )
        ).scalar_one()
        s.add(RoleGrant(member_id=mid, role=role, source="manual"))


@pytest.fixture
async def bot(app, make_client):
    lead = await make_client()
    await seed_world(lead)
    b = await FakeBot().load()
    for u, role in ((LEADER, "leader"), (MEMBER, "member"), (OTHER, "member")):
        await run(b.command("whoami"), FakeInteraction(u))  # first contact creates the member
        await _grant(u, role)
    b.web = lead
    return b


async def _scalar(sql: str, **params):
    async with session_scope() as s:
        return (await s.execute(text(sql), params)).scalar_one_or_none()


# ----- broad safety nets --------------------------------------------------------------------
@pytest.mark.parametrize("flavour", ["plausible", "junk"])
async def test_every_command_answers_without_crashing(bot, flavour):
    for cmd in bot.all_commands():
        inter = FakeInteraction(LEADER)
        if hasattr(cmd, "_params"):
            kwargs = sample_args(cmd, flavour)
            await run(cmd, inter, **kwargs)
        else:  # context menu on a message
            target = SimpleNamespace(id=1, attachments=[FakeAttachment(png())], content="", author=MEMBER)
            await run(cmd, inter, target=target)
        assert inter.replied, f"/{cmd.qualified_name} ({flavour}) left the user with no reply"


async def test_every_button_survives_stale_or_bogus_ids(bot):
    for key, handler in bot.handlers.items():
        for args in (["999999"], ["999999", "going"], ["0", "0", "0"], []):
            inter = FakeInteraction(LEADER, data={"custom_id": f"mmgu:{key}", "components": []})
            try:
                await handler(inter, list(args))
            except (IndexError, ValueError):
                pass  # malformed ids only come from forged requests; the dispatcher replies (tested below)
            else:
                assert inter.replied or inter.edited, f"{key}{args} gave no feedback"


async def test_members_without_rank_are_told_why(bot):
    stranger = user(9100, "Wanderer")
    await run(bot.command("whoami"), FakeInteraction(stranger))
    async with session_scope() as s:
        mid = (await s.execute(select(Identity.member_id).where(Identity.subject == "9100"))).scalar_one()
        s.add(RoleGrant(member_id=mid, role="guest", source="manual"))
    inter = await run(
        bot.command("bank request"), FakeInteraction(stranger), **sample_args(bot.command("bank request"))
    )
    assert inter.said and "rank" in inter.said[-1].text.lower()


# ----- end-to-end flows ---------------------------------------------------------------------
async def test_catalog_screenshot_then_save_and_report_drop(bot):
    from mmgu.modules.archive.reading import ReaderResult

    async def stub_reader(image: bytes):
        return ReaderResult(
            {
                "name": "Cloak of Flickering Shadows",
                "slots": ["BACK"],
                "flags": ["magic"],
                "stats": {"agi": 4},
                "classes": ["ALL"],
                "races": ["ALL"],
            },
            source="stub:test",
        )

    hall.ext.add("archive.readers", stub_reader, module="archive")
    try:
        inter = await run(
            bot.command("catalog"),
            FakeInteraction(MEMBER),
            screenshot=FakeAttachment(png()),
            creature="a shade",
            zone="Evershade Weald",
        )
        preview = inter.said[-1]
        assert "Cloak of Flickering Shadows" in preview.text and "AGI" in preview.text
        save = next(c for c in preview.custom_ids() if ":archive:save:" in c)
        inter = await press(bot, save, FakeInteraction(MEMBER))
        assert inter.edited and "Saved" in inter.said[-1].text
    finally:
        hall.ext._slots["archive.readers"] = [c for c in hall.ext._slots["archive.readers"] if c.fn is not stub_reader]
    item_id = await _scalar("select id from archive_items where name='Cloak of Flickering Shadows'")
    assert item_id and await _scalar("select count(*) from archive_drop_reports where item_id=:i", i=item_id) == 1

    inter = await run(bot.command("item"), FakeInteraction(OTHER), name="cloak of flickering shadows")
    card = inter.said[-1]
    assert "Evershade Weald" in card.text and "AGI: +4" in card.text
    dropform = next(c for c in card.custom_ids() if ":archive:dropform:" in c)
    inter = await press(bot, dropform, FakeInteraction(OTHER))
    assert inter.modal is not None
    submit = FakeInteraction(OTHER, data=modal_data(inter.modal, {"creature": "a wraith", "zone": "Scarwood"}))
    await press(bot, inter.modal.custom_id, submit)
    assert await _scalar("select count(*) from archive_drop_reports where item_id=:i", i=item_id) == 2


async def test_catalog_without_reader_points_to_the_web_form(bot):
    inter = await run(bot.command("catalog"), FakeInteraction(MEMBER), screenshot=FakeAttachment(png((9, 9, 9))))
    assert "/archive/new?proposal=" in inter.said[-1].text


async def test_unknown_item_suggests_close_names(bot):
    inter = await run(bot.command("item"), FakeInteraction(MEMBER), name="rusty scimtar")
    assert "Rusty Scimitar" in inter.said[-1].text


async def test_bank_request_approve_and_fulfil(bot):
    item = await _scalar("select name from archive_items order by id limit 1")
    await run(bot.command("bank request"), FakeInteraction(MEMBER), item=item, qty=1, reason="raid")
    rid = await _scalar("select max(id) from vault_requests")
    assert await _scalar("select status from vault_requests where id=:r", r=rid) == "pending"
    inter = await press(bot, f"mmgu:vault:approve:{rid}", FakeInteraction(LEADER))
    assert inter.replied
    status = await _scalar("select status from vault_requests where id=:r", r=rid)
    assert status in ("approved", "fulfilled"), status
    banker_only = await press(bot, f"mmgu:vault:approve:{rid}", FakeInteraction(OTHER))
    assert banker_only.replied


async def test_board_request_claim_and_done(bot):
    cmd = bot.command("request")
    kwargs = sample_args(cmd)
    kwargs.update(
        {k: v for k, v in {"title": "Need a port to Night Harbor", "details": "anytime"}.items() if k in cmd._params}
    )
    await run(cmd, FakeInteraction(MEMBER), **kwargs)
    rid = await _scalar("select max(id) from board_requests")
    await press(bot, f"mmgu:board:claim:{rid}", FakeInteraction(OTHER))
    assert await _scalar("select status from board_requests where id=:r", r=rid) == "claimed"
    await press(bot, f"mmgu:board:done:{rid}", FakeInteraction(OTHER))
    assert await _scalar("select status from board_requests where id=:r", r=rid) == "done"


async def test_time_of_death_from_discord_opens_a_window(bot):
    creature = await _scalar("select creature from watch_timers order by id limit 1")
    before = await _scalar("select count(*) from watch_deaths")
    cmd = bot.command("tod")
    # 90 minutes ago: well outside the 2-minute window in which duplicate kill reports are merged
    await run(cmd, FakeInteraction(MEMBER), **{**sample_args(cmd), "creature": creature, "minutes_ago": 90})
    assert await _scalar("select count(*) from watch_deaths") == before + 1
    inter = await run(bot.command("timers"), FakeInteraction(MEMBER))
    assert creature.split("<")[0] in inter.said[-1].text


async def test_char_add_then_level_up(bot):
    await run(
        bot.command("char add"), FakeInteraction(OTHER), name="Sablethorn", class_name="Rogue", level=10, main=True
    )
    inter = await run(bot.command("char level"), FakeInteraction(OTHER), name="Sablethorn", level=11)
    assert "11" in inter.said[-1].text
    inter = await run(bot.command("char level"), FakeInteraction(MEMBER), name="Sablethorn", level=50)
    assert "isn't your character" in inter.said[-1].text


async def test_link_merges_a_discord_member_into_the_web_login(bot):
    web = bot.web
    r = await web.post("/me/link-code")
    assert r.status_code == 303
    code = re.search(r"/link ([A-Z0-9]{6})", (await web.get("/me")).text).group(1)
    newcomer = user(9200, "Webby")
    await run(bot.command("item"), FakeInteraction(newcomer), name="Rusty Scimitar")  # creates a Discord-only member
    inter = await run(bot.command("link"), FakeInteraction(newcomer), code=code)
    assert "Linked" in inter.said[-1].text
    owner = await _scalar("select member_id from core_identities where provider='discord' and subject='9200'")
    web_member = await _scalar("select member_id from core_identities where provider='dev' order by id limit 1")
    assert owner == web_member
    inter = await run(bot.command("link"), FakeInteraction(newcomer), code=code)
    assert "expired" in inter.said[-1].text


async def test_closed_module_buttons_explain_instead_of_failing(app, make_client):
    lead = await make_client()
    await enable_all_modules(lead)
    await lead.post("/steward/modules/board", {"enable": "0"})
    b = await FakeBot().load()
    assert not any(k.startswith("board:") for k in b.handlers)


async def test_dispatcher_replies_when_a_handler_raises(app):
    import discord

    from mmgu.bot.client import HallBot

    b = HallBot(hall)

    async def boom(interaction, args):
        raise RuntimeError("bug")

    b.component_handlers["test:boom"] = boom
    inter = FakeInteraction(MEMBER, data={"custom_id": "mmgu:test:boom:1"})
    inter.type = discord.InteractionType.component
    await b.on_interaction(inter)
    assert inter.said and "went wrong" in inter.said[-1].text
    inter = FakeInteraction(MEMBER, data={"custom_id": "mmgu:gone:away:1"})
    inter.type = discord.InteractionType.component
    await b.on_interaction(inter)
    assert "closed" in inter.said[-1].text
    await b.close()
