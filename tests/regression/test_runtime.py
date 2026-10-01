"""The real entry points: `mmgu serve`, the scheduler, scaffolding and demo data."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from mmgu.core.hall import hall
from mmgu.core.modules import Job


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_mmgu_serve_migrates_and_answers():
    """Boots like the Docker image does: fresh data dir, migrations, web server, no Discord."""
    port = _free_port()
    data = tempfile.mkdtemp(prefix="mmgu-serve-")
    env = {
        **os.environ,
        "MMGU_DATA_DIR": data,
        "MMGU_PORT": str(port),
        "MMGU_HOST": "127.0.0.1",
        "MMGU_RUN_BOT": "false",
        "MMGU_AUTH_MODES": "dev",
        "MMGU_DEV_LOGIN": "true",
        "MMGU_DATABASE_URL": "",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "mmgu.cli", "serve"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                r = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1)
                if r.status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        else:
            proc.terminate()
            pytest.fail("server never came up:\n" + (proc.communicate(timeout=10)[0] or "")[-3000:])
        assert "archive" in r.json()["modules"]
        assert httpx.get(f"http://127.0.0.1:{port}/login", timeout=5).status_code == 200
        assert Path(data, "mmgu.db").exists()
    finally:
        proc.terminate()
        out = proc.communicate(timeout=20)[0] or ""
    assert "Traceback" not in out, out[-3000:]


async def test_scheduler_runs_enabled_jobs_and_survives_failures(app):
    from mmgu.core.scheduler import run_scheduler

    calls: list[str] = []

    async def ok():
        calls.append("ok")

    async def bad():
        calls.append("bad")
        raise RuntimeError("job bug")

    saved = list(hall.jobs)
    hall.jobs[:] = [
        ("archive", Job("ok", 1, ok)),
        ("archive", Job("bad", 1, bad)),
        ("not_a_module", Job("never", 1, ok)),
    ]
    try:
        task = asyncio.create_task(run_scheduler(hall, tick_seconds=0.05))
        await asyncio.sleep(0.2)
        task.cancel()
    finally:
        hall.jobs[:] = saved
    assert "ok" in calls and "bad" in calls
    assert calls.count("ok") == 1, "a job ran more often than its interval allows"


async def test_demo_seed_fills_an_empty_hall_once(app):
    from sqlalchemy import func, select

    from mmgu.core.models import Member
    from mmgu.db import session_scope
    from mmgu.demo import seed
    from mmgu.modules.archive.models import Item

    await seed()
    async with session_scope() as s:
        members = (await s.execute(select(func.count()).select_from(Member))).scalar_one()
        items = (await s.execute(select(func.count()).select_from(Item))).scalar_one()
    assert members == 3 and items == 2
    await seed()  # refuses to add to a hall that has members
    async with session_scope() as s:
        assert (await s.execute(select(func.count()).select_from(Member))).scalar_one() == 3


def test_new_module_scaffold_is_valid(tmp_path, monkeypatch):
    import mmgu.cli as cli

    (tmp_path / "modules").mkdir()
    monkeypatch.setattr(cli, "PKG", tmp_path)
    root = cli.new_module("stables", "The Stables", "Mount tracker")
    for f in ("module.py", "routes.py", "models.py", "bot.py"):
        compile((root / f).read_text(), f, "exec")
    assert (root / "templates" / "stables" / "index.html").exists()
    with pytest.raises(SystemExit):
        cli.new_module("stables", "x", "y")
    with pytest.raises(SystemExit):
        cli.new_module("Bad Name", "x", "y")


async def test_cli_token_works_against_the_api(app, make_client):
    from httpx import ASGITransport, AsyncClient

    from mmgu.cli import _create_token

    lead = await make_client()
    me = (await lead.get("/roster/me")).headers["location"]
    member_id = int(me.rsplit("/", 1)[1])
    raw = await _create_token(member_id, "cli-test")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/roster", headers={"Authorization": f"Bearer {raw}"})
        assert r.status_code == 200 and "characters" in r.json()
