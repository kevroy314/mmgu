# Architecture

```
            Discord (gateway, outbound)          Browsers / OBS
                     │                                  │
┌────────────────────┴──────────────────────────────────┴─────────────────┐
│ hallkeeper (one Python process)                                          │
│                                                                          │
│   discord.py bot ──┐                     ┌── FastAPI + Jinja + HTMX      │
│                    ├── module services ──┤                               │
│   scheduler jobs ──┘         │           └── JSON API (/api/..., tokens) │
│                              │                                           │
│        event bus · extension slots · permissions · audit log             │
│                              │                                           │
│              SQLAlchemy (async) → SQLite (WAL, FTS5) | Postgres          │
└──────────────────────────────┬───────────────────────────────────────────┘
                               │ optional
          Ollama (vision model) · Claude API · Litestream · Tailscale/Cloudflare
```

- **One process** runs the web app, the Discord bot and the scheduler in one asyncio loop. They share
  the same services, so a Discord command and a web form do exactly the same thing.
- **Modules** (`src/mmgu/modules/*`, `src/mmgu/addons/*`) each own their tables (prefixed with the
  module id), routes, templates, Discord commands, jobs and migrations. Core (`src/mmgu/core`) holds
  members, identities, roles, characters, settings, the audit log, proposals and uploads.
- **Services first**: logic lives in each module's `services.py`; routes, bot commands and the JSON API
  are thin wrappers.
- **Permissions** are keyed strings (`vault.manage`) with a default minimum rank plus duties
  (Banker, Crafter, Streamer, Raid Leader). Leaders override them in the Steward's Office.
- **Machines propose, people decide**: AI readers, companion imports and Claude (through MCP) create
  `Proposal` rows; a person approves them. Everything is audited.

## Modules

| Module | Room | What it is |
|---|---|---|
| hall | The Hall | Dashboard, login, profile, notices, search, proposals |
| steward | Steward's Office | Settings, modules/add-ons, permissions, ranks, audit log |
| roster | The Muster Roll | Members, characters (mains, alts, bank mules), officer notes, /who import |
| archive | The Archive | Item catalog, screenshots, drop reports, loot tables |
| vault | The Vault | Guild bank on bank-mule characters, deposits, withdrawal requests |
| board | The Notice Board | WTS/WTB listings, item and service requests |
| crafters | The Crafters' Hall | Tradeskill levels, recipes, "who can make X" |
| events | The War Table | Events, sign-ups, attendance, loot history, calendar feed |
| watch | The Watch | Spawn timers, time-of-death reports, camp check-ins |
| overlay | The Beacon | OBS stream overlays |
| screenshot_reader (add-on) | | Reads item/bank screenshots with a local or Claude vision model |
| ledger_sync (add-on) | | Imports loot/kill events from a player-run companion reading game files |
| mcp_server (add-on) | | Lets Claude Code / Claude Desktop use the hall through its API |

## Events on the bus

`archive.item_created`, `archive.item_updated`, `archive.drop_reported`, `roster.character_saved`,
`vault.request_created`, `vault.request_updated`, `vault.holdings_changed`, `board.posted`, `board.updated`,
`crafters.skill_saved`, `events.created`, `events.updated`, `events.starting`, `loot.awarded`,
`watch.tod_reported`, `watch.window_open`, `overlay.push`, `hall.startup`.

## Extension slots

`item.panels`, `item.discord_fields`, `member.panels`, `hall.cards`, `search.providers`,
`archive.readers`, `vault.readers`. See `src/mmgu/core/extensions.py`.

## Auth modes

`discord` (OAuth2 with optional role sync), `header` (trusted reverse proxy + shared secret), `dev`
(local only), and personal API tokens for companions and MCP. New members never get a display name
derived from their email address.
