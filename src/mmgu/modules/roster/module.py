"""The Muster Roll: members, their characters (mains, alts, bank mules) and officer notes."""

from mmgu.core.modules import Module, NavItem, SettingField
from mmgu.core.permissions import Permission


def _router():
    from fastapi import APIRouter

    from mmgu.modules.roster.routes import api, router

    combined = APIRouter()
    combined.include_router(router)
    combined.include_router(api)
    return combined


def _setup(hall):
    from mmgu.modules.roster import hooks

    hooks.register(hall)


async def _bot_setup(bot):
    from mmgu.modules.roster.bot import setup

    await setup(bot)


MODULE = Module(
    id="roster",
    name="The Muster Roll",
    plain_name="Roster",
    description="Who's in the guild, their mains and alts, levels and classes. Everything else builds on this.",
    nav=[NavItem("The Muster Roll", "Roster", "/roster", icon="banner", permission="members.view", order=60)],
    permissions=[
        Permission("roster.import_who", "Update the roster from pasted /who output", "officer", module="roster"),
    ],
    settings=[
        SettingField(
            "default_server",
            "Guild's home server",
            "text",
            "",
            "New characters default to this server, e.g. Estaire or Trem.",
        )
    ],
    router=_router,
    setup=_setup,
    bot_setup=_bot_setup,
)
