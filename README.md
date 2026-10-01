# Hallkeeper

A guild hall for **Monsters & Memories** (and other EverQuest-style MMOs): a Discord bot and a web app
that keep track of the things guilds used to keep in spreadsheets and forum threads.

- **The Archive** · item catalog. Screenshot an item's inspect window, drop it in Discord or the web hall,
  and it's cataloged with stats, classes and where it drops. `/item <name>` posts the card anywhere.
- **The Vault** · guild bank. Monsters & Memories has no guild bank, so the hall tracks what the guild's
  bank characters hold, who deposited what, and withdrawal requests bankers approve.
- **The Notice Board** · WTS / WTB listings and requests for items or services (crafting, ports, buffs,
  corpse runs), with claim → done → thanks.
- **The Crafters' Hall** · who has which tradeskill at what level, recipes, and "who can make X?".
- **The Muster Roll** · members, mains, alts and bank mules; class makeup; paste `/who` to update levels.
- **The War Table** · events with sign-ups by role, attendance, loot history and a calendar feed.
- **The Watch** · named-spawn timers from time-of-death reports, window alerts, camp check-ins.
- **The Beacon** · OBS overlays for streamers: item cards, discoveries, spawn windows and events, live.
- **Add-ons** (off by default): AI screenshot reading (local Ollama or Claude), a companion that imports
  loot from the game's log files, and an MCP server so Claude can help run the hall.

Everything anyone changes is in an audit log. Anything a machine suggests (AI reads, imports, Claude) waits
for a person to approve it.

## Quick start

```bash
git clone https://github.com/kevroy314/mmgu.git && cd mmgu
cp .env.example .env         # fill it in; see docs/discord.md
docker compose up -d         # http://localhost:8420
```

With an NVIDIA GPU, add local screenshot reading: `docker compose --profile ai up -d`, then
`docker compose --profile ai-setup run --rm ollama-pull` once.

To try it without Discord: set `MMGU_AUTH_MODES=dev` and `MMGU_DEV_LOGIN=true`, then pick any name at
the login page. Never do this on a hall others can reach.

## Docs

- [Getting started site](https://kevroy314.github.io/mmgu/)
- [Hosting](docs/hosting.md): home PC, Tailscale Funnel, Cloudflare Tunnel, Google Cloud
- [Discord setup](docs/discord.md) and the command list
- [Architecture](docs/architecture.md): modules, events, extension slots, auth
- [Adapting after launch](docs/adapting.md): game patches, new systems, new modules
- [Add-ons](docs/addons.md) and [the game's terms of service](docs/terms-of-service.md)

## Development

```bash
uv venv --python 3.12 && uv pip install -e ".[dev,anthropic,mcp]"
MMGU_AUTH_MODES=dev MMGU_DEV_LOGIN=true MMGU_RUN_BOT=false .venv/bin/mmgu serve
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests && .venv/bin/ruff format src tests
```

New system? `mmgu new-module <id> --name "The Stables" --plain "Mount tracker"`, then
`mmgu makemigrations <id> -m initial`.

## License

[PolyForm Noncommercial 1.0.0](LICENSE). Free to use, modify and share for any noncommercial purpose,
including running it for your guild. Selling it or using it commercially needs separate permission.

Hallkeeper is a fan project and is not affiliated with Niche Worlds Cult or Monsters & Memories.
Game names and terms belong to their owners.
