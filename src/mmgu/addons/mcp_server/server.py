"""Hallkeeper's MCP server: lets Claude (Code or Desktop) look things up in your hall.

It is a thin client over the hall's JSON API. It never opens the database, so the hall's own
permissions apply to everything it does: it can only see and do what the token's owner can.

    MMGU_API_URL=https://hall.example.org MMGU_API_TOKEN=mmgu_... mmgu mcp

Or over HTTP on this computer (for clients that prefer a URL)::

    python -m mmgu.addons.mcp_server.server --http --port 8765    # then connect to http://127.0.0.1:8765/mcp

Tools are read-mostly. The only writes are the ones a member could make in the web app (report a drop,
post a Notice Board request, report a time of death) and ``propose_change``, which files a suggestion
that a person must approve in the web app.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import httpx

INSTRUCTIONS = (
    "Tools for a Monsters & Memories guild's Hallkeeper site: the item catalog (the Archive), members and "
    "characters (the Roster), the guild bank (the Vault), the Notice Board, crafters, events, and spawn timers "
    "(the Watch). Look things up freely. To change anything that has no dedicated tool, use propose_change: "
    "it files a suggestion that an officer approves in the web app. Never claim a change was made when you "
    "only proposed it."
)


class HallApi:
    """A small async client for the hall's JSON API."""

    def __init__(self, base_url: str, token: str, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "hallkeeper-mcp"},
            timeout=30.0,
            transport=transport,
        )

    @classmethod
    def from_env(cls) -> HallApi:
        url = os.environ.get("MMGU_API_URL", "").strip()
        token = os.environ.get("MMGU_API_TOKEN", "").strip()
        if not url or not token:
            raise SystemExit(
                "Set MMGU_API_URL (your hall's address) and MMGU_API_TOKEN (a personal API token from your "
                "profile page in the hall)."
            )
        return cls(url, token)

    async def call(self, method: str, path: str, **kw: Any) -> tuple[Any, str | None]:
        """Returns (json, None) on success or (None, plain error message)."""
        if "params" in kw:
            kw["params"] = {k: v for k, v in kw["params"].items() if v not in (None, "")}
        try:
            r = await self.client.request(method, path, **kw)
        except httpx.HTTPError as e:
            return None, f"Couldn't reach the hall at {self.base_url} ({e.__class__.__name__})."
        if r.status_code == 401:
            return None, "The hall rejected the API token. Create a new one on your profile page."
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = None
            if r.status_code == 404 and not detail:
                detail = "Not found. That part of the hall may be closed."
            return None, f"The hall said no ({r.status_code}): {detail or r.text[:200]}"
        try:
            return r.json(), None
        except ValueError:
            return None, "The hall answered with something that isn't JSON. Is MMGU_API_URL the hall's address?"

    async def aclose(self) -> None:
        await self.client.aclose()


def _url(api: HallApi, path: str) -> str:
    return api.base_url + path


def _when(iso: Any) -> str:
    s = str(iso or "")
    return s.replace("T", " ")[:16] + (" UTC" if s else "")


# ----- tools (plain async functions so they're easy to test) -------------------------------------
async def search_items(
    api: HallApi,
    query: str = "",
    slot: str = "",
    cls: str = "",
    stat: str = "",
    zone: str = "",
    creature: str = "",
) -> str:
    data, err = await api.call(
        "GET",
        "/api/archive/items",
        params={"q": query, "slot": slot, "cls": cls, "stat": stat, "zone": zone, "creature": creature},
    )
    if err:
        return err
    items = data.get("items") or []
    if not items:
        return "No items in the Archive match that."
    lines = []
    for it in items[:25]:
        summary = " | ".join((it.get("text") or "").splitlines()[:4])
        lines.append(f"#{it['id']} {it['name']}: {summary}")
    more = f"\n({len(items) - 25} more; narrow the search.)" if len(items) > 25 else ""
    return "\n".join(lines) + more


async def get_item(api: HallApi, item: str) -> str:
    item = str(item).strip()
    item_id = item.lstrip("#")
    if not item_id.isdigit():
        data, err = await api.call("GET", "/api/archive/items", params={"q": item})
        if err:
            return err
        found = data.get("items") or []
        exact = [i for i in found if i["name"].lower() == item.lower()]
        if not found:
            return f"No item called {item!r} in the Archive."
        item_id = str((exact or found)[0]["id"])
    data, err = await api.call("GET", f"/api/archive/items/{item_id}")
    if err:
        return err
    out = [data["name"], data.get("text") or ""]
    drops = data.get("drops") or []
    if drops:
        out.append(
            "Drops from: "
            + "; ".join(f"{d.get('creature') or '?'} in {d.get('zone') or '?'} ({d.get('count', 1)}x)" for d in drops)
        )
    else:
        out.append("No drops recorded yet.")
    out.append(data.get("url") or "")
    return "\n".join(x for x in out if x)


async def report_drop(api: HallApi, item: str, creature: str = "", zone: str = "", note: str = "") -> str:
    data, err = await api.call(
        "POST",
        "/api/archive/drops",
        json={"item": item, "creature": creature, "zone": zone, "note": note, "source": "claude"},
    )
    if err:
        return err
    if data.get("status") == "proposed":
        return (
            f"{item} isn't in the Archive yet, so it was filed as a suggestion (#{data.get('proposal_id')}) for review."
        )
    if data.get("status") == "ignored":
        return "Nothing recorded: give the creature, the zone, or both."
    return f"Drop recorded for {item}."


async def roster(api: HallApi, query: str = "", cls: str = "", role: str = "", min_level: int | None = None) -> str:
    data, err = await api.call(
        "GET", "/api/roster", params={"q": query, "cls": cls, "role": role, "min_level": min_level}
    )
    if err:
        return err
    chars = data.get("characters") or []
    if not chars:
        return "No characters match."
    lines = []
    for c in chars[:60]:
        tags = [t for t, on in (("main", c.get("main")), ("bank mule", c.get("bank_mule"))) if on]
        lines.append(
            f"{c['name']}: level {c.get('level') or '?'} {c.get('race') or ''} {c.get('class') or ''}".rstrip()
            + f" ({c.get('member') or 'no member'}{', ' + ', '.join(tags) if tags else ''})"
        )
    return "\n".join(lines) + (f"\n({len(chars) - 60} more.)" if len(chars) > 60 else "")


async def vault_holdings(api: HallApi, query: str = "") -> str:
    data, err = await api.call("GET", "/api/vault/holdings", params={"q": query})
    if err:
        return err
    rows = data.get("holdings") or []
    if not rows:
        return "The guild bank has nothing matching that."
    return "\n".join(f"{h['qty']} x {h['item']} (on {h.get('character') or '?'})" for h in rows[:80])


async def vault_requests(api: HallApi, status: str = "pending") -> str:
    data, err = await api.call("GET", "/api/vault/requests", params={"status": status})
    if err:
        return err
    rows = data.get("requests") or []
    if not rows:
        return f"No {status} bank requests."
    return "\n".join(
        f"#{r['id']} {r.get('qty', 1)} x {r['item']} for {r.get('requester')}, {r.get('status')}, "
        f"asked {_when(r.get('created_at'))}"
        for r in rows[:60]
    )


async def board_listings(api: HallApi, kind: str = "", query: str = "") -> str:
    data, err = await api.call("GET", "/api/board/listings", params={"kind": kind.upper(), "q": query})
    if err:
        return err
    rows = data.get("listings") or []
    if not rows:
        return "No trade listings match."
    return "\n".join(
        f"#{r['id']} [{r.get('kind')}] {r.get('title') or r.get('item')}"
        + (f" for {r['price']}" if r.get("price") else "")
        + f" by {r.get('author')} ({r.get('status')})"
        for r in rows[:60]
    )


async def board_requests(api: HallApi, status: str = "open") -> str:
    data, err = await api.call("GET", "/api/board/requests", params={"status": status})
    if err:
        return err
    rows = data.get("requests") or []
    if not rows:
        return f"No {status} requests on the Notice Board."
    return "\n".join(
        f"#{r['id']} [{r.get('category')}] {r.get('title')} by {r.get('author')} ({r.get('status')}"
        + (f", claimed by {r['claimed_by']}" if r.get("claimed_by") else "")
        + ")"
        for r in rows[:60]
    )


async def post_board_request(api: HallApi, category: str, title: str, details: str = "") -> str:
    data, err = await api.call(
        "POST", "/api/board/requests", json={"category": category, "title": title, "details": details}
    )
    if err:
        return err
    return f"Posted request #{data.get('id')} on the Notice Board as you."


async def who_can_craft(api: HallApi, query: str) -> str:
    data, err = await api.call("GET", "/api/crafters/whocan", params={"q": query})
    if err:
        return err
    rows = data.get("results") or []
    if not rows:
        return f"Nobody has recorded a skill or recipe matching {query!r}."
    return "\n".join(
        f"{r.get('character')} ({r.get('member')}): {r.get('skill')} {r.get('level') or ''}".rstrip()
        + (f", knows {r['recipe']}" if r.get("recipe") else "")
        for r in rows[:40]
    )


async def upcoming_events(api: HallApi) -> str:
    data, err = await api.call("GET", "/api/events", params={"upcoming": 1})
    if err:
        return err
    rows = data.get("events") or []
    if not rows:
        return "No upcoming events."
    return "\n".join(
        f"#{e['id']} {e.get('title')} ({e.get('kind')}) at {_when(e.get('starts_at'))}"
        + (f" in {e['zone']}" if e.get("zone") else "")
        + f", {e.get('signups', 0)} signed up. {e.get('url') or ''}".rstrip()
        for e in rows[:30]
    )


async def spawn_timers(api: HallApi) -> str:
    data, err = await api.call("GET", "/api/watch/timers")
    if err:
        return err
    rows = data.get("timers") or []
    if not rows:
        return "No spawn timers are tracked."
    return "\n".join(
        f"#{t['id']} {t.get('creature')} ({t.get('zone') or '?'}): {t.get('status')}, "
        f"window {_when(t.get('window_start'))} to {_when(t.get('window_end'))}"
        for t in rows[:60]
    )


async def report_tod(api: HallApi, creature: str, minutes_ago: int = 0) -> str:
    data, err = await api.call("POST", "/api/watch/tod", json={"creature": creature, "minutes_ago": minutes_ago})
    if err:
        return err
    return (
        f"Time of death recorded for {creature}. Next spawn window: {_when(data.get('window_start'))} "
        f"to {_when(data.get('window_end'))}."
    )


async def list_proposals(api: HallApi, status: str = "pending") -> str:
    data, err = await api.call("GET", "/api/proposals", params={"status": status})
    if err:
        return err
    rows = data.get("proposals") or []
    if not rows:
        return f"No {status} suggestions."
    return "\n".join(
        f"#{p['id']} [{p.get('kind')}] {p.get('summary')} (from {p.get('source')}, {_when(p.get('created_at'))})"
        for p in rows[:60]
    )


async def propose_change(api: HallApi, kind: str, summary: str, details: dict | str | None = None) -> str:
    payload = details if isinstance(details, dict) else {"details": details or ""}
    data, err = await api.call(
        "POST",
        "/api/proposals",
        json={"kind": kind or "note", "source": "claude", "summary": summary, "payload": payload},
    )
    if err:
        return err
    return (
        f"Suggestion #{data.get('id')} filed. Nothing has changed yet: an officer reviews it at "
        f"{_url(api, '/proposals')}."
    )


# ----- wiring into an MCP server -----------------------------------------------------------------
def _server_class():
    try:
        from mcp.server.mcpserver import MCPServer  # mcp >= 2

        return MCPServer
    except ImportError:  # pragma: no cover - mcp 1.x
        from mcp.server.fastmcp import FastMCP

        return FastMCP


def build_server(api: HallApi):
    """An MCP server whose tools call ``api``. Tool docstrings are what Claude reads."""
    mcp = _server_class()(name="hallkeeper", instructions=INSTRUCTIONS)

    @mcp.tool(name="search_items")
    async def search_items_tool(
        query: str = "", slot: str = "", cls: str = "", stat: str = "", zone: str = "", creature: str = ""
    ) -> str:
        """Search the guild's item catalog. query: words in the name or text. slot: e.g. PRIMARY, FINGER.
        cls: a class name (e.g. Wizard). stat: a stat key to sort by (str, sta, agi, dex, int, wis, cha, hp,
        mana, mr, fr, cr, pr, dr). zone / creature: where it drops."""
        return await search_items(api, query, slot, cls, stat, zone, creature)

    @mcp.tool(name="get_item")
    async def get_item_tool(item: str) -> str:
        """Full details of one item (inspect-window text and where it drops), by id (#12) or exact name."""
        return await get_item(api, item)

    @mcp.tool(name="report_drop")
    async def report_drop_tool(item: str, creature: str = "", zone: str = "", note: str = "") -> str:
        """Record that an item dropped from a creature and/or in a zone. Unknown items become a suggestion."""
        return await report_drop(api, item, creature, zone, note)

    @mcp.tool(name="roster")
    async def roster_tool(query: str = "", cls: str = "", role: str = "", min_level: int | None = None) -> str:
        """List the guild's characters. query: name. cls: class name. role: tank, healer, dps, support.
        min_level: only characters at or above this level."""
        return await roster(api, query, cls, role, min_level)

    @mcp.tool(name="vault_holdings")
    async def vault_holdings_tool(query: str = "") -> str:
        """What the guild bank holds, optionally filtered by item name."""
        return await vault_holdings(api, query)

    @mcp.tool(name="vault_requests")
    async def vault_requests_tool(status: str = "pending") -> str:
        """Members' requests for items from the guild bank (status: pending, approved, fulfilled, declined)."""
        return await vault_requests(api, status)

    @mcp.tool(name="board_listings")
    async def board_listings_tool(kind: str = "", query: str = "") -> str:
        """Trade listings on the Notice Board. kind: WTS (selling) or WTB (buying). query: item or words."""
        return await board_listings(api, kind, query)

    @mcp.tool(name="board_requests")
    async def board_requests_tool(status: str = "open") -> str:
        """Help requests on the Notice Board (crafting, escorts, corpse recovery...)."""
        return await board_requests(api, status)

    @mcp.tool(name="post_board_request")
    async def post_board_request_tool(category: str, title: str, details: str = "") -> str:
        """Post a help request on the Notice Board as the token's owner. Ask the user before posting."""
        return await post_board_request(api, category, title, details)

    @mcp.tool(name="who_can_craft")
    async def who_can_craft_tool(query: str) -> str:
        """Who in the guild can craft something: a tradeskill name or a recipe/item name."""
        return await who_can_craft(api, query)

    @mcp.tool(name="upcoming_events")
    async def upcoming_events_tool() -> str:
        """The guild's upcoming raids and events with times (UTC) and signup counts."""
        return await upcoming_events(api)

    @mcp.tool(name="spawn_timers")
    async def spawn_timers_tool() -> str:
        """Tracked named creatures and their next spawn windows (UTC)."""
        return await spawn_timers(api)

    @mcp.tool(name="report_tod")
    async def report_tod_tool(creature: str, minutes_ago: int = 0) -> str:
        """Record a tracked creature's time of death (now, or minutes_ago). Ask the user before reporting."""
        return await report_tod(api, creature, minutes_ago)

    @mcp.tool(name="list_proposals")
    async def list_proposals_tool(status: str = "pending") -> str:
        """Suggested changes waiting for (or past) human review."""
        return await list_proposals(api, status)

    @mcp.tool(name="propose_change")
    async def propose_change_tool(kind: str, summary: str, details: dict | str | None = None) -> str:
        """Suggest any change the other tools can't make (fix an item's stats, update a character, add a note).
        kind: a short label such as archive.item_fix, roster.update or note. summary: one plain sentence a
        person will read. details: the specifics. Nothing changes until an officer approves it."""
        return await propose_change(api, kind, summary, details)

    return mcp


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="mmgu mcp", description="Hallkeeper MCP server (uses MMGU_API_URL/TOKEN).")
    p.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    p.add_argument("--host", default="127.0.0.1", help="HTTP bind address (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=8765, help="HTTP port (default 8765)")
    args = p.parse_args(argv)
    server = build_server(HallApi.from_env())
    if args.http:
        print(f"Hallkeeper MCP on http://{args.host}:{args.port}/mcp", file=sys.stderr)
        server.run("streamable-http", host=args.host, port=args.port)
    else:
        server.run()


def run_stdio() -> None:
    """Entry point for ``mmgu mcp``."""
    main([])


if __name__ == "__main__":
    main()
