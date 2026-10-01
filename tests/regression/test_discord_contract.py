"""Discord's limits and our button wiring, checked without connecting to Discord.

Discord rejects the whole command sync if one command breaks a rule, which would silently take every
slash command offline. These tests catch that before deploy.
"""

from __future__ import annotations

import re
from pathlib import Path

import discord
import pytest
from discord import app_commands

from mmgu.bot.client import HallBot
from mmgu.core.hall import hall

NAME = re.compile(r"^[-_\w]{1,32}$")
SRC = Path(__file__).resolve().parents[2] / "src" / "mmgu"


@pytest.fixture
async def bot(app, make_client):
    lead = await make_client()
    from tests.regression.world import enable_all_modules

    await enable_all_modules(lead)
    b = HallBot(hall)
    await b._register_modules()
    yield b
    await b.close()


def _walk(cmds):
    for c in cmds:
        yield c
        if isinstance(c, app_commands.Group):
            yield from _walk(c.commands)


def test_command_names_descriptions_and_options(bot):
    top = bot.tree.get_commands(guild=bot.guild_obj)
    slash = [c for c in top if not isinstance(c, app_commands.ContextMenu)]
    menus = [c for c in top if isinstance(c, app_commands.ContextMenu)]
    assert len(slash) <= 100, "Discord allows at most 100 slash commands per app"
    assert len([m for m in menus if m.type == discord.AppCommandType.message]) <= 5
    names = [c.name for c in slash]
    assert len(names) == len(set(names)), f"duplicate slash command names: {names}"
    for c in _walk(slash):
        assert NAME.match(c.name) and c.name == c.name.lower(), f"bad command name {c.name!r}"
        assert 1 <= len(c.description) <= 100, f"/{c.qualified_name}: description must be 1–100 chars"
        if isinstance(c, app_commands.Group):
            assert len(c.commands) <= 25
            continue
        assert len(c.parameters) <= 25
        required_done = False
        for p in c.parameters:
            assert NAME.match(p.display_name) and p.display_name == p.display_name.lower(), (
                f"/{c.qualified_name}: bad option name {p.display_name!r}"
            )
            assert 1 <= len(p.description or "x") <= 100, f"/{c.qualified_name} {p.display_name}: description too long"
            assert len(p.choices) <= 25
            if not p.required:
                required_done = True
            assert not (p.required and required_done), f"/{c.qualified_name}: required option after optional"
    for m in menus:
        assert 1 <= len(m.name) <= 32


def test_expected_core_commands_exist(bot):
    names = {c.qualified_name for c in _walk(bot.tree.get_commands(guild=bot.guild_obj))}
    for expected in [
        "hall",
        "whoami",
        "link",
        "item",
        "drops",
        "catalog",
        "Catalog item",
        "char add",
        "roster",
        "bank search",
        "bank request",
        "wts",
        "wtb",
        "request",
        "board",
        "whocan",
        "tod",
        "timers",
        "events",
        "loot award",
    ]:
        assert expected in names, f"missing Discord command: {expected}"


def test_every_button_id_in_code_has_a_handler(bot):
    """Each cid("module", "action", ...) used anywhere must be routed, or the button silently does nothing."""
    used = set()
    for py in SRC.rglob("*.py"):
        for mod, action in re.findall(r'cid\(\s*"([a-z_]+)"\s*,\s*"([a-z_]+)"', py.read_text()):
            used.add(f"{mod}:{action}")
    assert used, "found no button ids; did the cid() helper get renamed?"
    missing = sorted(u for u in used if u not in bot.component_handlers)
    assert not missing, f"buttons with no handler: {missing}"


def test_custom_ids_fit_discords_limit():
    from mmgu.bot.helpers import cid

    assert len(cid("archive", "dropsubmit", 2**31)) <= 100
    with pytest.raises(ValueError):
        cid("x", "y", "z" * 120)
