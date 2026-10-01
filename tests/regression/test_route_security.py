"""Every state-changing route refuses visitors, forged requests and ranks without permission."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from tests.regression.routes import routes
from tests.regression.world import fill, seed_world

# POSTs that are intentionally reachable without a session (they authorise another way).
OPEN_POSTS = {
    "/auth/dev": "dev login; returns 404 unless MMGU_DEV_LOGIN is on (tests turn it on)",
    "/logout": "harmless for visitors",
}
ADMIN_POSTS_PREFIX = ("/steward/",)


async def _urls(app, ids):
    for path in routes(app, "POST"):
        url = fill(path, ids)
        if url is not None:
            yield path, url


async def test_visitors_cannot_change_anything(app, make_client):
    lead = await make_client()
    ids = await seed_world(lead)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        for path, url in [x async for x in _urls(app, ids)]:
            if path in OPEN_POSTS:
                continue
            r = await anon.post(url, data={})
            assert r.status_code in (401, 403, 404), f"visitor POST {path} → {r.status_code}"


async def test_session_posts_without_csrf_are_rejected(app, make_client):
    lead = await make_client()
    ids = await seed_world(lead)
    for path, url in [x async for x in _urls(app, ids)]:
        if path.startswith(("/auth/", "/overlay/")) or path.startswith("/api/"):
            continue  # OAuth/overlay have their own checks; API calls use bearer tokens (see below)
        r = await lead.http.post(url, data={"x": "1"})  # session cookie, no csrf field or header
        assert r.status_code == 403, f"{path} accepted a POST without a CSRF token ({r.status_code})"


async def test_api_session_posts_need_csrf_but_tokens_dont(app, make_client):
    lead = await make_client()
    await seed_world(lead)
    r = await lead.http.post("/api/proposals", json={"kind": "note", "summary": "no csrf"})
    assert r.status_code == 403
    r = await lead.get("/me")
    import re

    raw = re.search(r"(mmgu_[A-Za-z0-9_-]{20,})", (await lead.get("/me")).text)
    if raw is None:  # token value is only shown once; make a fresh one
        await lead.post("/me/tokens", {"name": "t"})
        raw = re.search(r"(mmgu_[A-Za-z0-9_-]{20,})", (await lead.get("/me")).text)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as bot:
        r = await bot.post(
            "/api/proposals",
            json={"kind": "note", "summary": "via token"},
            headers={"Authorization": f"Bearer {raw.group(1)}"},
        )
        assert r.status_code == 200, r.text
        r = await bot.get("/api/archive/items", headers={"Authorization": "Bearer mmgu_not-a-real-token"})
        assert r.status_code == 401


async def test_recruits_cannot_use_the_stewards_office(app, make_client):
    lead = await make_client()
    ids = await seed_world(lead)
    rookie = await make_client("Rookie", "recruit")
    for path, url in [x async for x in _urls(app, ids)]:
        if path.startswith(ADMIN_POSTS_PREFIX):
            r = await rookie.post(url, {"enable": "1", "ack": "yes", "rank": "leader"})
            assert r.status_code == 403, f"recruit POST {path} → {r.status_code}"


async def test_no_post_crashes_on_empty_or_junk_input(app, make_client):
    """Bad input must produce a plain 4xx message, never a 500."""
    lead = await make_client()
    ids = await seed_world(lead)
    for path, url in [x async for x in _urls(app, ids)]:
        if path in ("/logout",) or path.startswith("/steward/modules/"):
            continue  # logging out or closing a room would hide crashes in the routes tested after it
        for body in ({}, {"name": "x" * 5000, "qty": "-9", "level": "abc", "when": "yesterday-ish", "kind": "???"}):
            if path.startswith("/api/"):
                r = await lead.post_json(url, body)
            else:
                r = await lead.post(url, body)
            assert "part of the hall is closed" not in r.text, f"{path}: its module got closed mid-test"
            assert r.status_code < 500, f"{path} crashed with {r.status_code} on {list(body)}: {r.text[:200]}"
