"""Command line: ``mmgu serve``, ``mmgu migrate``, ``mmgu makemigrations``, ``mmgu new-module`` and friends."""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from pathlib import Path

from mmgu.core.hall import hall

PKG = Path(__file__).resolve().parent


def _boot() -> None:
    if not hall.booted:
        hall.boot()


def alembic_config():
    from alembic.config import Config

    _boot()
    cfg = Config()
    cfg.set_main_option("script_location", str(PKG / "alembic"))
    locations = [str(PKG / "core" / "migrations")]
    for m in hall.modules.values():
        if m.package_dir and (m.package_dir / "migrations").is_dir():
            locations.append(str(m.package_dir / "migrations"))
    cfg.set_main_option("version_locations", " ".join(locations))
    cfg.set_main_option("path_separator", "space")
    return cfg


def migrate() -> None:
    from alembic import command

    command.upgrade(alembic_config(), "heads")


def makemigrations(module_id: str, message: str) -> None:
    from alembic import command
    from alembic.script import ScriptDirectory

    cfg = alembic_config()
    if module_id == "core":
        path = PKG / "core" / "migrations"
    else:
        m = hall.modules[module_id]
        assert m.package_dir is not None
        path = m.package_dir / "migrations"
    path.mkdir(exist_ok=True)
    locs = cfg.get_main_option("version_locations") or ""
    if str(path) not in locs.split(" "):
        cfg.set_main_option("version_locations", f"{locs} {path}".strip())
    cfg.attributes["only_module"] = module_id
    script = ScriptDirectory.from_config(cfg)
    has_branch = any(module_id in (rev.branch_labels or ()) for rev in script.walk_revisions())
    if has_branch:
        command.revision(cfg, message=message, autogenerate=True, head=f"{module_id}@head")
    else:
        depends = None
        if module_id != "core":
            core_heads = [r.revision for r in script.walk_revisions() if "core" in (r.branch_labels or ())]
            depends = "core@head" if core_heads else None
        command.revision(
            cfg,
            message=message,
            autogenerate=True,
            head="base",
            branch_label=module_id,
            version_path=str(path),
            depends_on=depends,
        )


MODULE_TEMPLATE = '''"""{name}: {plain}."""

from mmgu.core.modules import Module, NavItem
from mmgu.core.permissions import Permission


def _router():
    from mmgu.modules.{id}.routes import router

    return router


async def _bot_setup(bot):
    from mmgu.modules.{id}.bot import setup

    await setup(bot)


MODULE = Module(
    id="{id}",
    name="{name}",
    plain_name="{plain}",
    description="Describe what this room of the hall is for.",
    nav=[NavItem("{name}", "{plain}", "/{id}", icon="scroll", permission="{id}.view", order=500)],
    permissions=[
        Permission("{id}.view", "See {plain}", "recruit", module="{id}"),
        Permission("{id}.edit", "Change {plain}", "member", module="{id}"),
    ],
    router=_router,
    bot_setup=_bot_setup,
)
'''

ROUTES_TEMPLATE = """from fastapi import APIRouter, Depends, Request

from mmgu.core.auth import Viewer, require
from mmgu.core.web import render

router = APIRouter()


@router.get("/{id}")
async def index(request: Request, viewer: Viewer = Depends(require("{id}.view"))):
    return render(request, "{id}/index.html")
"""

MODELS_TEMPLATE = """from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from mmgu.db import Base, Timestamped


class Thing(Base, Timestamped):
    __tablename__ = "{id}_things"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("core_members.id"), nullable=True)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
"""

BOT_TEMPLATE = """import discord
from discord import app_commands

from mmgu.bot.helpers import reply


async def setup(bot):
    @app_commands.command(name="{id}", description="{plain}")
    async def cmd(interaction: discord.Interaction) -> None:
        await reply(interaction, "Hello from {name}.")

    bot.add_command(cmd)
"""

TEMPLATE_HTML = """{{% extends "core/base.html" %}}
{{% block title %}}{name}{{% endblock %}}
{{% block content %}}
<header class="page-head">
  <p class="eyebrow">{plain}</p>
  <h1>{name}</h1>
</header>
<p>This room is new. Build it out in <code>src/mmgu/modules/{id}</code>.</p>
{{% endblock %}}
"""


def new_module(module_id: str, name: str, plain: str) -> Path:
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,30}", module_id):
        raise SystemExit("module id must be lowercase letters, digits and underscores")
    root = PKG / "modules" / module_id
    if root.exists():
        raise SystemExit(f"{root} already exists")
    (root / "templates" / module_id).mkdir(parents=True)
    fmt = {"id": module_id, "name": name, "plain": plain}
    (root / "__init__.py").write_text("")
    (root / "module.py").write_text(MODULE_TEMPLATE.format(**fmt))
    (root / "routes.py").write_text(ROUTES_TEMPLATE.format(**fmt))
    (root / "models.py").write_text(MODELS_TEMPLATE.format(**fmt))
    (root / "bot.py").write_text(BOT_TEMPLATE.format(**fmt))
    (root / "templates" / module_id / "index.html").write_text(TEMPLATE_HTML.format(**fmt))
    return root


async def _create_token(member_id: int, name: str) -> str:
    from mmgu.core.auth import hash_token, new_token
    from mmgu.core.models import ApiToken
    from mmgu.db import init_engine, session_scope

    init_engine(hall.settings.db_url)
    raw = new_token()
    async with session_scope() as session:
        session.add(ApiToken(member_id=member_id, name=name, prefix=raw[:9], token_hash=hash_token(raw)))
    return raw


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="mmgu", description="Hallkeeper guild hall")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="migrate the database, then run the web app, bot and scheduler")
    sub.add_parser("migrate", help="apply database migrations")
    mk = sub.add_parser("makemigrations", help="generate a migration for one module's tables")
    mk.add_argument("module")
    mk.add_argument("-m", "--message", default="changes")
    nm = sub.add_parser("new-module", help="scaffold a new module (a new room of the hall)")
    nm.add_argument("id")
    nm.add_argument("--name", required=True, help='in-world name, e.g. "The Stables"')
    nm.add_argument("--plain", required=True, help='what it is, e.g. "Mount tracker"')
    tk = sub.add_parser("token", help="create an API token for a member (for the MCP server or companions)")
    tk.add_argument("member_id", type=int)
    tk.add_argument("--name", default="cli")
    sub.add_parser("mcp", help="run the MCP server over stdio (needs MMGU_API_URL and MMGU_API_TOKEN)")
    sub.add_parser("seed-demo", help="fill an empty hall with clearly-marked example data")
    args = p.parse_args(argv)

    if args.cmd == "serve":
        migrate()
        from mmgu.main import main as serve_main

        serve_main()
    elif args.cmd == "migrate":
        migrate()
    elif args.cmd == "makemigrations":
        makemigrations(args.module, args.message)
    elif args.cmd == "new-module":
        root = new_module(args.id, args.name, args.plain)
        print(f"Created {root}. Next: edit models.py, then run `mmgu makemigrations {args.id} -m 'initial'`.")
    elif args.cmd == "token":
        _boot()
        print(asyncio.run(_create_token(args.member_id, args.name)))
    elif args.cmd == "mcp":
        from mmgu.addons.mcp_server.server import run_stdio

        run_stdio()
    elif args.cmd == "seed-demo":
        _boot()
        from mmgu.demo import seed

        asyncio.run(seed())
    else:  # pragma: no cover
        p.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
