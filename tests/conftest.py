"""Test fixtures: a fresh hall with a temp SQLite database and dev login."""

from __future__ import annotations

import os
import re
import tempfile

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

_DATA = tempfile.mkdtemp(prefix="mmgu-test-")
os.environ.update(
    {
        "MMGU_DATA_DIR": _DATA,
        "MMGU_AUTH_MODES": "dev,header",
        "MMGU_DEV_LOGIN": "true",
        "MMGU_PROXY_SECRET": "test-proxy-secret",
        "MMGU_BOOTSTRAP_LEADERS": "email:leader@example.test",
        "MMGU_RUN_BOT": "false",
        "MMGU_SECRET_KEY": "test-secret",
        "MMGU_GUILD_NAME": "Test Guild",
        "MMGU_DATABASE_URL": os.environ.get("MMGU_TEST_DATABASE_URL", ""),
        "MMGU_BASE_URL": "http://localhost:8420",
    }
)


@pytest_asyncio.fixture
async def app():
    from mmgu.app import create_app
    from mmgu.core import store
    from mmgu.core.hall import hall
    from mmgu.db import Base, engine

    application = create_app()
    async with engine().begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    store.reset_cache()
    async with application.router.lifespan_context(application):
        yield application
    await hall.bus.drain()


class Client:
    """An httpx client that logs in through dev login and handles the CSRF token."""

    def __init__(self, http: AsyncClient):
        self.http = http
        self.csrf = ""

    async def login(self, name: str = "Leader Lyra", rank: str = "leader") -> Client:
        await self.refresh_csrf("/login")
        r = await self.http.post("/auth/dev", data={"name": name, "rank": rank, "next": "/", "csrf": self.csrf})
        assert r.status_code in (303, 204), r.text
        await self.refresh_csrf("/me")
        return self

    async def refresh_csrf(self, path: str = "/") -> None:
        r = await self.http.get(path)
        m = re.search(r'"X-CSRF-Token": "([^"]+)"', r.text)
        if m:
            self.csrf = m.group(1)

    async def get(self, url, **kw):
        return await self.http.get(url, **kw)

    async def post(self, url, data=None, files=None, **kw):
        data = dict(data or {})
        data["csrf"] = self.csrf
        return await self.http.post(url, data=data, files=files, **kw)

    async def post_json(self, url, json, **kw):
        return await self.http.post(url, json=json, headers={"X-CSRF-Token": self.csrf}, **kw)


@pytest_asyncio.fixture
async def make_client(app):
    clients = []

    async def _make(name: str = "Leader Lyra", rank: str = "leader") -> Client:
        http = AsyncClient(transport=ASGITransport(app=app), base_url="http://test", follow_redirects=False)
        clients.append(http)
        return await Client(http).login(name, rank)

    yield _make
    for c in clients:
        await c.aclose()
