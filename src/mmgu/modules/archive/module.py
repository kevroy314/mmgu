"""The Archive: the guild's item catalog, built from screenshots and drop reports."""

from mmgu.core.modules import Module, NavItem
from mmgu.core.permissions import Permission


def _router():
    from mmgu.modules.archive.routes import router

    return router


def _setup(hall):
    from mmgu.modules.archive import hooks

    hooks.register(hall)


async def _bot_setup(bot):
    from mmgu.modules.archive.bot import setup

    await setup(bot)


MODULE = Module(
    id="archive",
    name="The Archive",
    plain_name="Item catalog",
    description="Catalog items from screenshots, record where they drop, and look them up from Discord.",
    nav=[NavItem("The Archive", "Item catalog", "/archive", icon="scroll", permission="archive.view", order=10)],
    permissions=[
        Permission("archive.view", "See the Archive", "recruit", module="archive"),
        Permission("archive.catalog", "Catalog items and report drops", "recruit", module="archive"),
        Permission("archive.edit", "Edit any item's details", "member", module="archive"),
        Permission(
            "archive.moderate",
            "Merge duplicates, remove drop reports, mark items outdated",
            "officer",
            module="archive",
        ),
    ],
    channels={"archive": "Item discoveries"},
    router=_router,
    setup=_setup,
    bot_setup=_bot_setup,
)
