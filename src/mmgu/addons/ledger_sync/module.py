"""Ledger Sync: import loot and kills from the game's own log files, via a script members run at home."""

from mmgu.core.modules import Module, SettingField


def _router():
    from mmgu.addons.ledger_sync.routes import router

    return router


MODULE = Module(
    id="ledger_sync",
    name="Ledger Sync",
    plain_name="Import from game log files",
    description=(
        "Members run a small script on their own PC that reads the game's Ledger files and sends loot and "
        "kills here: loot becomes drop reports, kills of tracked creatures update spawn timers. "
        "Setup and recent imports: /addons/ledger."
    ),
    kind="addon",
    requires=["archive"],
    default_enabled=False,
    admin_url="/addons/ledger",
    tos_risk="high",
    tos_notice=(
        "This works with a companion script that reads the game's own log files (the Ledger) on members' "
        "computers. The Monsters & Memories user agreement forbids software that reads or mines information the "
        "game produces, so running it likely breaks the current rules and could put members' accounts at risk. "
        "It never reads game memory or network traffic and never changes game files. Turning this on is your "
        "guild's decision; running the script is each member's own choice and responsibility."
    ),
    settings=[
        SettingField(
            "propose_unknown_items",
            "Suggest new Archive items for unknown loot",
            "bool",
            True,
            "When someone loots an item the Archive doesn't have, add it to the suggestions for review.",
        ),
        SettingField(
            "max_kill_age_hours",
            "Ignore kills older than (hours)",
            "int",
            72,
            "Older kills from a game log don't update spawn timers.",
        ),
    ],
    router=_router,
)
