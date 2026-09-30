"""Claude Integration: an MCP server so Claude can look things up in the hall (and suggest changes)."""

from mmgu.core.modules import Module


def _router():
    from mmgu.addons.mcp_server.routes import router

    return router


MODULE = Module(
    id="mcp_server",
    name="Claude Integration",
    plain_name="MCP server",
    description=(
        "Lets members ask Claude (Claude Code or Claude Desktop) about the hall: items, the roster, the bank, "
        "events and spawn timers. Claude works through the member's own API token, so it can only see and do "
        "what they can, and any other change it wants becomes a suggestion for an officer to approve. "
        "Setup: /addons/claude."
    ),
    kind="addon",
    default_enabled=False,
    admin_url="/addons/claude",
    tos_risk="none",
    router=_router,
)
