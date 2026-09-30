"""Example data for trying Hallkeeper locally (`mmgu seed-demo`). Everything is marked as an example.

Only seeds an empty hall; never run it on a real guild's hall.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select

from mmgu.core import store
from mmgu.core.auth import build_viewer, find_or_create_member, sync_roles
from mmgu.core.hall import hall
from mmgu.core.models import Member
from mmgu.db import init_engine, session_scope

log = logging.getLogger(__name__)

PEOPLE = [
    ("Example Leader", "leader", [("Aldric", "Paladin", 42, "Human", True)]),
    (
        "Example Banker",
        "member",
        [("Brisa", "Enchanter", 35, "High Elf", True), ("Coffer", "Bard", 5, "Halfling", False)],
    ),
    ("Example Crafter", "member", [("Dunmore", "Fighter", 30, "Dwarf", True)]),
]

ITEMS = [
    {
        "name": "Rusty Scimitar",
        "slots": ["PRIMARY", "SECONDARY"],
        "skill": "1HS",
        "damage": 5,
        "delay": 36,
        "weight": 7.0,
        "size": "MEDIUM",
        "classes": ["ALL"],
        "races": ["ALL"],
        "item_type": "Weapon",
        "_drop": ("a decaying skeleton", "Scarwood"),
    },
    {
        "name": "Example Cloak of Tides",
        "slots": ["BACK"],
        "flags": ["magic"],
        "ac": 4,
        "stats": {"wis": 3, "mana": 10},
        "weight": 1.0,
        "size": "SMALL",
        "classes": ["Cleric", "Druid", "Shaman"],
        "races": ["ALL"],
        "item_type": "Armor",
        "description": "Example item for the demo hall.",
        "_drop": ("an example sea hag", "Blacktide Bay"),
    },
]


async def seed() -> None:
    from mmgu.modules.archive import services as archive
    from mmgu.modules.roster import services as roster

    init_engine(hall.settings.db_url)
    async with session_scope() as session:
        await store.load_all(session)
        if (await session.execute(select(func.count()).select_from(Member))).scalar_one():
            log.warning("The hall already has members; not adding example data.")
            return
        viewers = []
        for name, rank, chars in PEOPLE:
            m, _ = await find_or_create_member(session, "dev", name.lower(), display_name=name)
            m.onboarded = True
            await sync_roles(session, m.id, {rank} | ({"banker"} if "Banker" in name else set()), "dev")
            v = await build_viewer(session, m, "dev")
            viewers.append(v)
            for cname, cls, lvl, race, main in chars:
                await roster.save_character(session, v, name=cname, class_name=cls, level=lvl, race=race, is_main=main)
        for data in ITEMS:
            drop = data.pop("_drop")
            item = await archive.create_item(session, viewers[0], data)
            await archive.add_drop(session, viewers[0], item, creature=drop[0], zone=drop[1])
    log.info("Example hall ready. Log in with dev login as 'Example Leader'.")
