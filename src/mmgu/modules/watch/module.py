"""The Watch: named-creature respawn timers, time-of-death reports, window alerts and camp check-ins."""

from mmgu.core.modules import Job, Module, NavItem, SettingField
from mmgu.core.permissions import Permission


def _router():
    from mmgu.modules.watch.routes import router

    return router


def _setup(hall):
    from mmgu.modules.watch import hooks

    hooks.register(hall)


async def _bot_setup(bot):
    from mmgu.modules.watch.bot import setup

    await setup(bot)


async def _tick():
    from mmgu.modules.watch.services import run_tick

    await run_tick()


MODULE = Module(
    id="watch",
    name="The Watch",
    plain_name="Spawn timers",
    description="Track named creatures' respawn windows from reported times of death, get pinged when a window "
    "opens, and see who is holding which camp.",
    nav=[NavItem("The Watch", "Spawn timers", "/watch", icon="hourglass", permission="watch.view", order=55)],
    permissions=[
        Permission("watch.view", "See spawn timers and camps", "recruit", module="watch"),
        Permission("watch.report", "Report times of death and check in at camps", "recruit", module="watch"),
        Permission(
            "watch.manage",
            "Add and edit tracked creatures, remove any report",
            "officer",
            duties=("raidlead",),
            module="watch",
        ),
    ],
    settings=[
        SettingField(
            "alert_minutes_before",
            "Early warning (minutes)",
            "int",
            10,
            "Watchers get a heads-up this many minutes before a window opens. 0 turns it off.",
        ),
        SettingField(
            "camp_hours",
            "Camp check-ins expire after (hours)",
            "int",
            4,
            "A camp nobody marks as done is cleared after this long.",
        ),
    ],
    jobs=[Job("windows", 30, _tick)],
    channels={"watch": "Spawn window alerts"},
    router=_router,
    setup=_setup,
    bot_setup=_bot_setup,
)
