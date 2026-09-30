# Adapting the hall after launch

Monsters & Memories is in Early Access. Classes get rebalanced, tradeskills appear, the developers may
add chat logs or an API, and the guild will want things nobody predicted. Hallkeeper is built so most of
those changes are a data edit, and the rest are a new module rather than surgery.

## The game changed

| Change | Where | Restart needed |
|---|---|---|
| New class, race, server, zone, tradeskill, item slot, stat or resist | `src/mmgu/game_packs/monsters-and-memories.yaml` (or a local override at `data/game_pack.yaml`) | Yes |
| An item's stats changed in a patch | Edit the item; the old version stays in its history. Officers can mark items "outdated". | No |
| `/who` output format is finally known | `PATTERNS` in `src/mmgu/core/whoparse.py` | Yes |
| The inspect window looks different | Screenshot Reader add-on prompt, or switch model in its settings | No |

Stats and resists the item form doesn't have columns for are stored as JSON, so adding one to the game
pack makes it appear on the form, in search filters and on item cards without a migration.

## The guild wants a new system

Everything in the hall is a module, including the built-in rooms. A new system is a new module:

```bash
mmgu new-module stables --name "The Stables" --plain "Mount tracker"
# edit src/mmgu/modules/stables/{models,services,routes,bot}.py and its templates
mmgu makemigrations stables -m "initial"
mmgu migrate
```

A module declares its navigation entry, permissions (with sensible default ranks), settings (which appear
in the Steward's Office automatically), Discord channels, scheduled jobs and Discord commands. It never
needs to edit another module:

- **Event bus**: react to what other modules do (`archive.item_created`, `watch.window_open`, …).
  See the event list in `docs/architecture.md`.
- **Extension slots**: add a panel to every item page (`item.panels`), a field to `/item` embeds
  (`item.discord_fields`), a dashboard card (`hall.cards`), search results (`search.providers`), a panel
  on member pages (`member.panels`), or a screenshot reader (`archive.readers`, `vault.readers`).
- **Proposals**: anything machine-generated (AI, imports, Claude) becomes a suggested change a person
  approves, using the core `Proposal` table.

Each module has its own migration branch, so modules can evolve independently and third-party add-ons
can ship their own tables. Leaders turn modules on and off at runtime; Discord commands resync.

## Data that doesn't fit yet

Most tables carry an `extra` or `attributes` JSON column. When the shape of something is still unclear
(a new item property, a camp's spawn notes), store it there first and promote it to a column with a
migration once it has settled.

## Rolling out changes safely

- Every change anyone makes is in the audit log with before/after snapshots.
- `docker compose up -d --build` applies migrations on start. Litestream (or copying `data/`) before
  big upgrades makes rollback a file copy.
- A module that fails to load is shown in the Steward's Office and skipped; the rest of the hall keeps
  running.
