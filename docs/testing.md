# Testing

```bash
.venv/bin/python -m pytest -q                 # everything (~3–4 minutes)
.venv/bin/python -m pytest -q tests/regression # the cross-cutting regression suite
.venv/bin/python -m pytest -q --cov            # with the coverage floor CI enforces
MMGU_TEST_DATABASE_URL=postgresql+asyncpg://user:pw@host/db .venv/bin/python -m pytest -q   # on Postgres
```

## Layout

- `tests/test_<module>.py`: behaviour of one module (forms, rules, its JSON API).
- `tests/regression/`: checks that cover **every** route, command and module automatically, so a new
  module is tested the moment it exists:

| File | Catches |
|---|---|
| `test_crawl.py` | Any page that errors or renders badly for any rank (leader → visitor), boosted or not |
| `test_route_security.py` | POSTs that accept visitors, skip CSRF, let recruits into admin, or 500 on bad input |
| `test_permissions_matrix.py` | Rank/duty rules drifting from the documented model; officers creating leaders |
| `test_migrations.py` | A model changed without `mmgu makemigrations`; migration branches out of order for Postgres |
| `test_discord_contract.py` | Commands that would make Discord reject the whole command sync; buttons with no handler |
| `test_discord_flows.py` | Every command and button answering (with real and junk input), plus the main Discord flows |
| `test_module_contracts.py` | Table/permission/template naming, add-on ToS rules, unknown extension slots |
| `test_gamepack.py` | Typos in the hand-edited game pack after a patch |
| `test_escaping.py` | User text rendered as HTML anywhere |
| `test_static_assets.py` | Missing stylesheets/scripts, JavaScript syntax errors |
| `test_runtime.py` | `mmgu serve` booting from scratch, the scheduler, scaffolding, demo data, CLI tokens |

`tests/regression/world.py` seeds one of everything through the real web forms. When you add a module
with new path parameters (`/stables/{mount_id}`), add the parameter to `PARAM_SQL` and a seeding step to
`seed_world`; `test_crawl` fails with "world fixture has no row for" until you do.

CI runs lint, the suite on SQLite with a coverage floor, the suite on Postgres, and a Docker build that
must boot, migrate and serve.
