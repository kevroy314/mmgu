"""The hall itself: dashboard, login, profile, notices, search and uploads."""

from mmgu.core.modules import Module, NavItem


def _router():
    from mmgu.modules.hall.routes import router

    return router


MODULE = Module(
    id="hall",
    name="The Hall",
    plain_name="Dashboard",
    description="The front door: what's happening in the guild today.",
    kind="core",
    nav=[NavItem("The Hall", "Dashboard", "/", icon="hearth", permission="hall.view", order=0)],
    router=_router,
)
