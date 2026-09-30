"""The Notice Board: buy/sell listings and requests for help, since the game has no auction house."""

from mmgu.core.modules import Job, Module, NavItem, SettingField
from mmgu.core.permissions import Permission


def _router():
    from mmgu.modules.board.routes import router

    return router


def _setup(hall):
    from mmgu.modules.board import hooks

    hooks.register(hall)


async def _bot_setup(bot):
    from mmgu.modules.board.bot import setup

    await setup(bot)


async def _expire():
    from mmgu.modules.board.services import expire_job

    await expire_job()


MODULE = Module(
    id="board",
    name="The Notice Board",
    plain_name="Requests & trade",
    description=(
        "Post what you want to sell or buy (WTS / WTB) and ask guildmates for crafting, ports, buffs "
        "or a corpse recovery. Matching buyers and sellers are told about each other."
    ),
    nav=[NavItem("The Notice Board", "Requests & trade", "/board", icon="pin", permission="board.view", order=30)],
    permissions=[
        Permission("board.view", "See the Notice Board", "recruit", module="board"),
        Permission("board.post", "Post listings and requests, claim requests", "recruit", module="board"),
        Permission("board.moderate", "Close or edit anyone's posts", "officer", module="board"),
    ],
    settings=[
        SettingField(
            "listing_days",
            "Days a listing stays up",
            "int",
            7,
            "WTS/WTB listings close by themselves after this many days unless the poster renews them.",
        )
    ],
    jobs=[Job("expire_listings", 600, _expire)],
    channels={"board": "Notice Board posts (WTS, WTB, requests)"},
    router=_router,
    setup=_setup,
    bot_setup=_bot_setup,
)
