"""The Vault: the guild bank, kept on bank mule characters because the game has no guild bank."""

from mmgu.core.modules import Module, NavItem
from mmgu.core.permissions import Permission


def _router():
    from mmgu.modules.vault.routes import router

    return router


def _setup(hall):
    from mmgu.modules.vault import hooks

    hooks.register(hall)


async def _bot_setup(bot):
    from mmgu.modules.vault.bot import setup

    await setup(bot)


MODULE = Module(
    id="vault",
    name="The Vault",
    plain_name="Guild bank",
    description=(
        "Track what the guild's bank mules hold, log deposits and withdrawals, "
        "and let members request items from the bank."
    ),
    nav=[NavItem("The Vault", "Guild bank", "/vault", icon="chest", permission="vault.view", order=20)],
    permissions=[
        Permission("vault.view", "See what the guild bank holds", "member", module="vault"),
        Permission("vault.request", "Request items from the guild bank", "member", module="vault"),
        Permission(
            "vault.manage",
            "Record deposits and withdrawals, update bank inventories, approve requests",
            "officer",
            duties=("banker",),
            module="vault",
        ),
    ],
    channels={"vault": "Guild bank requests"},
    router=_router,
    setup=_setup,
    bot_setup=_bot_setup,
)
