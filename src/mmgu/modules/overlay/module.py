"""The Beacon: browser-source overlays for OBS that show guild activity live on stream."""

from mmgu.core.modules import Job, Module, NavItem
from mmgu.core.permissions import Permission


def _router():
    from mmgu.modules.overlay.routes import router

    return router


def _setup(hall):
    from mmgu.modules.overlay import hooks

    hooks.register(hall)


async def _bot_setup(bot):
    from mmgu.modules.overlay.bot import setup

    await setup(bot)


async def _prune():
    from mmgu.db import session_scope
    from mmgu.modules.overlay import services

    async with session_scope() as session:
        await services.prune(session)


MODULE = Module(
    id="overlay",
    name="The Beacon",
    plain_name="Stream overlay",
    description="Overlays for OBS that show item cards, new discoveries, spawn timers and events live on stream.",
    nav=[
        NavItem("The Beacon", "Stream overlay", "/beacon", icon="beacon", permission="overlay.manage", order=70),
    ],
    admin_url="/beacon",
    permissions=[
        Permission("overlay.push", "Put cards and messages on the stream overlay", "officer", ("streamer",), "overlay"),
        Permission("overlay.manage", "Create and change stream overlays", "leader", ("streamer",), "overlay"),
    ],
    default_enabled=True,
    jobs=[Job("overlay.prune", 3600, _prune)],
    router=_router,
    setup=_setup,
    bot_setup=_bot_setup,
)
