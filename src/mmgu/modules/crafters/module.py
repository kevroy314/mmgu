"""The Crafters' Hall: who has which tradeskill at what level, recipes, and "who can make this?"."""

from mmgu.core.modules import Module, NavItem
from mmgu.core.permissions import Permission


def _router():
    from mmgu.modules.crafters.routes import router

    return router


def _setup(hall):
    from mmgu.modules.crafters import hooks

    hooks.register(hall)


async def _bot_setup(bot):
    from mmgu.modules.crafters.bot import setup

    await setup(bot)


MODULE = Module(
    id="crafters",
    name="The Crafters' Hall",
    plain_name="Tradeskills",
    description="Everyone's tradeskill levels and specialties, shared recipes, and who in the guild can make what.",
    nav=[NavItem("The Crafters' Hall", "Tradeskills", "/crafters", icon="anvil", permission="crafters.view", order=40)],
    permissions=[
        Permission("crafters.view", "See tradeskills and recipes", "recruit", module="crafters"),
        Permission("crafters.edit", "Record your own tradeskills and add recipes", "recruit", module="crafters"),
        Permission("crafters.manage", "Edit anyone's tradeskills and any recipe", "officer", module="crafters"),
    ],
    router=_router,
    setup=_setup,
    bot_setup=_bot_setup,
)
