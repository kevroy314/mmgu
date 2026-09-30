"""The War Table: events, sign-ups, attendance and loot history."""

from mmgu.core.modules import Job, Module, NavItem, SettingField
from mmgu.core.permissions import Permission


def _router():
    from mmgu.modules.events.routes import router

    return router


def _setup(hall):
    from mmgu.modules.events import hooks

    hooks.register(hall)


async def _bot_setup(bot):
    from mmgu.modules.events.bot import setup

    await setup(bot)


async def _tick():
    from mmgu.modules.events.hooks import run_job

    await run_job()


MODULE = Module(
    id="events",
    name="The War Table",
    plain_name="Events & loot",
    description=(
        "Schedule raids and group nights, take sign-ups and attendance, and keep an open record of who got what."
    ),
    requires=["roster"],
    nav=[NavItem("The War Table", "Events & loot", "/events", icon="swords", permission="events.view", order=50)],
    permissions=[
        Permission("events.view", "See events and who's going", "recruit", module="events"),
        Permission("events.signup", "Sign up for events and check in", "recruit", module="events"),
        Permission(
            "events.manage",
            "Create and edit events, take attendance",
            "officer",
            duties=("raidlead",),
            module="events",
        ),
        Permission("loot.view", "See the loot history", "recruit", module="events"),
        Permission("loot.award", "Record loot awards", "officer", duties=("raidlead",), module="events"),
    ],
    settings=[
        SettingField(
            "reminder_minutes",
            "Reminder lead time (minutes)",
            "int",
            30,
            "How long before an event starts to remind everyone signed up (and post in the events channel).",
        ),
        SettingField(
            "auto_close_hours",
            "Close events after (hours)",
            "int",
            6,
            "Events are marked done this many hours after their end time (or start time if there's no end).",
        ),
        SettingField(
            "announce_days",
            "Announce in Discord (days ahead)",
            "int",
            7,
            "New events are posted to the events channel once they're this close. Weekly repeats post one at a time.",
        ),
    ],
    jobs=[Job("tick", 60, _tick)],
    channels={"events": "Event announcements"},
    router=_router,
    setup=_setup,
    bot_setup=_bot_setup,
)
