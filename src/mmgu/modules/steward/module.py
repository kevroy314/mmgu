"""The Steward's Office: settings, modules and add-ons, permissions, members and the audit log."""

from mmgu.core.modules import Module, NavItem


def _router():
    from mmgu.modules.steward.routes import router

    return router


MODULE = Module(
    id="steward",
    name="Steward's Office",
    plain_name="Admin",
    description="Leader and officer tools: settings, add-ons, permissions, members, audit log.",
    kind="core",
    nav=[
        NavItem("Steward's Office", "Admin & settings", "/steward", icon="key", permission="members.manage", order=900)
    ],
    router=_router,
)
