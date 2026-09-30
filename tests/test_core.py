from mmgu.core.hall import hall
from mmgu.core.whoparse import parse_who


async def test_boot_and_pages(make_client):
    c = await make_client()
    for path in [
        "/",
        "/me",
        "/steward",
        "/steward/settings",
        "/steward/permissions",
        "/steward/members",
        "/steward/audit",
        "/notices",
        "/proposals",
        "/search?q=ab",
    ]:
        r = await c.get(path)
        assert r.status_code == 200, (path, r.status_code, r.text[:500])


async def test_csrf_required(make_client):
    c = await make_client()
    r = await c.http.post("/me", data={"display_name": "Nope"})
    assert r.status_code == 403


async def test_recruit_cannot_open_steward(make_client):
    c = await make_client("Rookie", "recruit")
    r = await c.get("/steward")
    assert r.status_code == 403


async def test_header_auth_requires_secret(app):
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        r = await http.get("/me", headers={"X-Email": "leader@example.test"})
        assert r.status_code == 401  # no secret: treated as a visitor
        r = await http.get(
            "/me", headers={"X-Email": "leader@example.test", "X-MMGU-Proxy-Secret": "test-proxy-secret"}
        )
        assert r.status_code == 200
        assert "Leader" in r.text
        assert "leader@example.test" not in r.text  # emails never become display names


def test_who_parser():
    lines, skipped = parse_who(
        "[60 Wizard] Velyra (High Elf) <Test Guild>\n[ANONYMOUS] Shade\nnonsense line\nBrannoc - 34 Cleric", hall.game
    )
    by = {w.name: w for w in lines}
    assert by["Velyra"].level == 60 and by["Velyra"].class_name == "Wizard" and by["Velyra"].race == "High Elf"
    assert "Shade" in by
    assert by["Brannoc"].class_name == "Cleric"
    assert skipped == ["nonsense line"]


async def test_module_toggle_needs_ack(make_client):
    c = await make_client()
    addons = [m for m in hall.modules.values() if m.needs_acknowledgement]
    if not addons:
        return
    m = addons[0]
    r = await c.post(f"/steward/modules/{m.id}", {"enable": "1"})
    assert r.status_code == 422
    r = await c.post(f"/steward/modules/{m.id}", {"enable": "1", "ack": "yes"})
    assert r.status_code == 303
    assert hall.is_enabled(m.id) or m.requires


async def test_error_page_renders_for_logged_in_browser(make_client):
    c = await make_client()
    r = await c.get("/archive/items/999999", headers={"Accept": "text/html"})
    assert r.status_code == 404
    assert "bricked up" in r.text


async def test_bus_events_wait_for_commit(app):
    """Handlers run after the transaction commits, so they can read what was just written."""
    import asyncio

    from sqlalchemy import select

    from mmgu.core.models import Member
    from mmgu.db import session_scope

    seen: list = []

    async def handler(e):
        async with session_scope() as s:
            seen.append((await s.execute(select(Member).where(Member.id == e.data["id"]))).scalar_one_or_none())

    hall.bus.subscribe("test.deferred", handler)
    async with session_scope() as s:
        m = Member(display_name="Deferred")
        s.add(m)
        await s.flush()
        await hall.bus.emit("test.deferred", id=m.id)
        await asyncio.sleep(0.05)
        assert seen == []  # not delivered before commit
    await hall.bus.drain()
    assert seen and seen[0] is not None

    seen.clear()
    try:
        async with session_scope() as s:
            await hall.bus.emit("test.deferred", id=-1)
            raise RuntimeError("roll back")
    except RuntimeError:
        pass
    await hall.bus.drain()
    assert seen == []  # dropped on rollback
