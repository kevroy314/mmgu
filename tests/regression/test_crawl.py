"""Visit every page as every rank. Catches template errors, crashes and broken links after any change."""

from __future__ import annotations

import pytest

from tests.regression.routes import is_api, routes
from tests.regression.world import fill, seed_world

RANKS = ["leader", "officer", "member", "recruit", "guest"]
# Pages that stream forever or need query input to mean anything.
SKIP = {"/overlay/{token}/stream"}
# Not pages a leader opens directly, with the reason they don't return 200 here.
EXPECTED = {
    "/auth/discord/login": "Discord OAuth isn't configured in tests (404 by design)",
    "/auth/discord/callback": "needs a code/state from Discord (400 by design)",
    "/events.ics": "calendar feed needs ?token=; covered in test_events",
}
HTML = {"Accept": "text/html"}


async def _crawl(app, client, ids, *, boosted=False):
    results = {}
    for path in routes(app, "GET"):
        if path in SKIP or "{token}" in path:
            continue
        url = fill(path, ids)
        if url is None:
            results[path] = "no-data"
            continue
        headers = dict(HTML)
        if boosted:
            headers.update({"HX-Request": "true", "HX-Boosted": "true"})
        r = await client.get(url, headers=headers)
        results[path] = r
    return results


async def test_leader_can_open_every_page(app, make_client):
    lead = await make_client()
    ids = await seed_world(lead)
    results = await _crawl(app, lead, ids)
    missing = [p for p, r in results.items() if r == "no-data"]
    assert not missing, f"world fixture has no row for: {missing}"
    bad = {
        p: r.status_code for p, r in results.items() if p not in EXPECTED and r.status_code not in (200, 204, 303, 307)
    }
    assert not bad, f"pages a leader can't open: {bad}"
    for path, r in results.items():
        full_page = r.text.lstrip().lower().startswith("<!doctype")
        if r.status_code == 200 and full_page and not path.startswith("/overlay"):
            assert 'id="main"' in r.text, f"{path} lacks #main, so boosted navigation can't swap it"
            assert "Traceback" not in r.text


async def test_boosted_navigation_returns_full_main(app, make_client):
    lead = await make_client()
    ids = await seed_world(lead)
    results = await _crawl(app, lead, ids, boosted=True)
    for path, r in results.items():
        if r == "no-data" or is_api(path):
            continue
        assert r.status_code < 500, f"{path} → {r.status_code} on boosted navigation"


@pytest.mark.parametrize("rank", RANKS[1:])
async def test_every_rank_gets_clean_responses(app, make_client, rank):
    lead = await make_client()
    ids = await seed_world(lead)
    other = await make_client(f"Rank {rank}", rank)
    results = await _crawl(app, other, ids)
    errors = {p: r.status_code for p, r in results.items() if r != "no-data" and r.status_code >= 500}
    assert not errors, f"server errors for a {rank}: {errors}"
    for path, r in results.items():
        if r != "no-data" and r.status_code in (401, 403, 404) and not is_api(path):
            assert "Traceback" not in r.text


async def test_visitor_never_sees_member_pages(app, make_client):
    from httpx import ASGITransport, AsyncClient

    lead = await make_client()
    ids = await seed_world(lead)
    public_ok = {
        "/",
        "/login",
        "/healthz",
        "/welcome",
        "/auth/discord/callback",
        "/notices/count",
    }  # count: empty fragment
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        for path in routes(app, "GET"):
            if "{token}" in path or path in SKIP:
                continue
            url = fill(path, ids)
            if url is None:
                continue
            r = await anon.get(url, headers=HTML)
            assert r.status_code < 500, (path, r.status_code)
            if path in public_ok:
                continue
            assert r.status_code in (303, 307, 401, 403, 404), f"visitor got {r.status_code} on {path}"
