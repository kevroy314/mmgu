import asyncio
import re

from httpx import ASGITransport, AsyncClient


async def _drain():
    from mmgu.core.hall import hall

    await hall.bus.drain()


async def _make_scene(c, **over):
    data = {
        "name": "Main scene",
        "position": "bottom-right",
        "theme": "window",
        "scale": "100",
        "card_seconds": "30",
        "timers_count": "3",
        "feeds": ["item", "text", "discoveries", "timers", "event"],
        "active": "1",
    }
    data.update(over)
    r = await c.post("/beacon/new", data)
    assert r.status_code == 303, r.text
    page = await c.get("/beacon")
    tokens = re.findall(r'/overlay/([A-Za-z0-9_-]{20,})"', page.text)
    assert tokens
    return tokens[-1]


async def _anon(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_overlay_token_auth(app, make_client):
    c = await make_client()
    token = await _make_scene(c)
    async with await _anon(app) as anon:
        r = await anon.get(f"/overlay/{token}")
        assert r.status_code == 200
        assert "EventSource" in r.text and "no-referrer" in r.text
        assert "/static/" not in r.text  # must not depend on files behind the sign-in proxy
        r = await anon.get(f"/overlay/{token}/state")
        assert r.status_code == 200
        body = r.json()
        assert body["scene"]["position"] == "bottom-right"
        assert body["messages"] == []
        from mmgu.core.hall import hall

        assert body["timers"] == ([] if hall.is_enabled("watch") else None)  # no spawn timers yet
        for bad in ("nope", "x" * 200, token[:-1] + ("A" if token[-1] != "A" else "B")):
            assert (await anon.get(f"/overlay/{bad}")).status_code == 404
            assert (await anon.get(f"/overlay/{bad}/state")).status_code == 404
            assert (await anon.get(f"/overlay/{bad}/stream")).status_code == 404
        # anonymous visitors can't reach the management pages
        r = await anon.get("/beacon", headers={"accept": "text/html"})
        assert r.status_code == 303 and "/login" in r.headers["location"]


async def test_push_text_reaches_state(app, make_client):
    c = await make_client()
    token = await _make_scene(c)
    r = await c.post("/beacon/push", {"title": "Welcome raiders", "body": "Crypt at 8"})
    assert r.status_code == 303
    await _drain()
    async with await _anon(app) as anon:
        msgs = (await anon.get(f"/overlay/{token}/state")).json()["messages"]
    assert msgs[-1]["kind"] == "text" and msgs[-1]["title"] == "Welcome raiders" and msgs[-1]["card"]
    r = await c.get("/beacon")
    assert "Welcome raiders" in r.text  # recently on stream
    r = await c.post("/beacon/push", {"title": "", "body": ""})
    assert r.status_code == 422


async def test_item_push_discovery_and_feed_filter(app, make_client):
    c = await make_client()
    token = await _make_scene(c)
    only_text = await _make_scene(c, name="Text only", feeds=["text"])
    r = await c.post("/archive/new", {"name": "Rusty Scimitar", "slots": ["PRIMARY"], "stat_str": "2"})
    item_url = r.headers["location"]
    item_id = int(item_url.rsplit("/", 1)[1])
    r = await c.post(f"/archive/items/{item_id}/stream")
    assert r.status_code == 204
    r = await c.post("/beacon/push", {"name": "rusty scimitar"})
    assert r.status_code == 303
    await _drain()
    async with await _anon(app) as anon:
        msgs = (await anon.get(f"/overlay/{token}/state")).json()["messages"]
        kinds = [m["kind"] for m in msgs]
        assert "discovery" in kinds and kinds.count("item") == 2
        card = next(m for m in msgs if m["kind"] == "item")
        assert card["item"]["name"] == "Rusty Scimitar"
        assert ["STR", 2] in card["item"]["lines"]["stats"]
        disc = next(m for m in msgs if m["kind"] == "discovery")
        assert disc["item"] == "Rusty Scimitar" and disc["member"] == "Leader Lyra"
        other = (await anon.get(f"/overlay/{only_text}/state")).json()["messages"]
        assert other == []
    r = await c.post("/beacon/push", {"name": "Nothing Like This"})
    assert r.status_code == 422


async def test_manage_scene_lifecycle(app, make_client):
    c = await make_client()
    token = await _make_scene(c)
    page = await c.get("/beacon")
    sid = int(re.search(r'id="scene-(\d+)"', page.text).group(1))
    assert "Shutdown source when not visible" in page.text and "data-copy" in page.text
    assert (await c.get("/beacon/new")).status_code == 200
    r = await c.get(f"/beacon/scenes/{sid}")
    assert r.status_code == 200 and "Main scene" in r.text
    r = await c.post(
        f"/beacon/scenes/{sid}",
        {
            "name": "Raid",
            "position": "top-left",
            "theme": "clear",
            "scale": "80",
            "card_seconds": "8",
            "timers_count": "5",
            "feeds": ["text"],
            "active": "1",
        },
    )
    assert r.status_code == 303
    async with await _anon(app) as anon:
        scene = (await anon.get(f"/overlay/{token}/state")).json()["scene"]
        assert scene == {
            "name": "Raid",
            "position": "top-left",
            "theme": "clear",
            "scale": 80,
            "card_seconds": 8,
            "feeds": ["text"],
            "active": True,
        }
        r = await c.post(f"/beacon/scenes/{sid}", {"name": "Raid", "feeds": [], "position": "top", "theme": "window"})
        assert r.status_code == 422
        r = await c.post(f"/beacon/scenes/{sid}", {"name": "Raid", "feeds": ["text"], "scale": "900"})
        assert r.status_code == 422

        # test card goes to this scene only, even when it doesn't show items
        r = await c.post(f"/beacon/scenes/{sid}/test")
        assert r.status_code == 303
        await _drain()
        msgs = (await anon.get(f"/overlay/{token}/state")).json()["messages"]
        assert msgs and msgs[-1]["kind"] == "text"

        # paused scenes get no new cards
        assert (await c.post(f"/beacon/scenes/{sid}/air")).status_code == 303
        await c.post("/beacon/push", {"title": "While paused"})
        await _drain()
        msgs = (await anon.get(f"/overlay/{token}/state")).json()["messages"]
        assert all(m.get("title") != "While paused" for m in msgs)
        await c.post(f"/beacon/scenes/{sid}/air")

        # a new URL kills the old one
        assert (await c.post(f"/beacon/scenes/{sid}/token")).status_code == 303
        assert (await anon.get(f"/overlay/{token}")).status_code == 404
        page = await c.get("/beacon")
        new_token = re.findall(r'/overlay/([A-Za-z0-9_-]{20,})"', page.text)[-1]
        assert new_token != token
        assert (await anon.get(f"/overlay/{new_token}")).status_code == 200

        assert (await c.post(f"/beacon/scenes/{sid}/delete")).status_code == 303
        assert (await anon.get(f"/overlay/{new_token}/state")).status_code == 404
    assert (await c.get(f"/beacon/scenes/{sid}")).status_code == 404


async def test_permissions(app, make_client):
    rookie = await make_client("Rookie", "recruit")
    assert (await rookie.get("/beacon")).status_code == 403
    r = await rookie.post("/beacon/push", {"title": "hi"})
    assert r.status_code == 403
    officer = await make_client("Officer Oda", "officer")
    r = await officer.get("/beacon")
    assert r.status_code == 200 and "Send to stream" in r.text and "New overlay" not in r.text
    assert (await officer.post("/beacon/push", {"title": "Hello chat"})).status_code == 303
    assert (await officer.get("/beacon/new")).status_code == 403
    assert (await officer.post("/beacon/new", {"name": "x", "feeds": ["text"]})).status_code == 403
    r = await officer.get("/")
    assert 'href="/beacon"' not in r.text  # nav needs overlay.manage


async def test_hub_pubsub():
    from mmgu.modules.overlay.hub import CLOSE, QUEUE_SIZE, Hub

    hub = Hub()
    a = hub.subscribe(1, ["item", "text"])
    b = hub.subscribe(2, ["discoveries"])
    paused = hub.subscribe(3, ["item"], active=False)
    assert hub.publish({"kind": "item", "feed": "item", "id": 1}) == 1
    assert a.queue.get_nowait()["id"] == 1
    assert b.queue.empty() and paused.queue.empty()
    # targeted messages (test cards) ignore feeds and pauses
    assert hub.publish({"kind": "text", "feed": "text", "id": 2}, scene_id=3) == 1
    assert paused.queue.get_nowait()["id"] == 2
    # refresh goes to everyone
    assert hub.publish({"kind": "refresh"}) == 3
    for s in (a, b, paused):
        s.queue.get_nowait()
    # settings change: new feeds apply and the page is told to reload
    hub.configure(2, ["item"], True)
    assert b.queue.get_nowait() == {"kind": "reload"}
    assert hub.publish({"kind": "item", "feed": "item", "id": 3}) == 2
    # a stalled client drops its oldest messages instead of blocking
    for i in range(QUEUE_SIZE + 5):
        hub.publish({"kind": "text", "feed": "text", "id": 100 + i}, scene_id=1)
    assert a.queue.qsize() == QUEUE_SIZE
    # closing a scene ends its streams and forgets them
    hub.close(1)
    items = [a.queue.get_nowait() for _ in range(a.queue.qsize())]
    assert items[-1] is CLOSE
    assert hub.count(1) == 0 and hub.count() == 2
    hub.unsubscribe(b)
    hub.unsubscribe(paused)
    assert hub.count() == 0
    # a waiting stream wakes up on publish
    c = hub.subscribe(9, ["text"])
    waiter = asyncio.create_task(c.queue.get())
    await asyncio.sleep(0)
    hub.publish({"kind": "text", "feed": "text", "id": 7})
    assert (await asyncio.wait_for(waiter, 1))["id"] == 7


async def test_bus_to_hub_and_adapters(app, make_client, monkeypatch):
    from mmgu.core.hall import hall
    from mmgu.modules.overlay import adapters
    from mmgu.modules.overlay.hub import hub

    c = await make_client()
    token = await _make_scene(c)
    page = await c.get("/beacon")
    sid = int(re.search(r'id="scene-(\d+)"', page.text).group(1))
    sub = hub.subscribe(sid, ["text", "timers", "event"])
    try:
        await hall.bus.emit("overlay.push", kind="text", title="Live", body="")
        await _drain()
        msg = sub.queue.get_nowait()
        assert msg["title"] == "Live" and isinstance(msg["id"], int)

        # spawn timers come from the real Watch module; events through next_events (faked here)
        from datetime import datetime, timedelta

        from mmgu.db import session_scope, utcnow
        from mmgu.modules.watch.models import DeathReport, Timer

        async with session_scope() as session:
            t = Timer(
                creature="Lord Grimclaw",
                creature_key="lord grimclaw",
                zone="Scarwood",
                respawn_minutes=60,
                variance_minutes=10,
            )
            idle = Timer(creature="Nobody Knows", creature_key="nobody knows", respawn_minutes=60)
            session.add_all([t, idle])
            await session.flush()
            session.add(DeathReport(timer_id=t.id, died_at=utcnow() - timedelta(minutes=55)))
            timer_id = t.id

        async def fake_events(session, limit):
            return [{"id": 9, "title": "Crypt raid", "starts_at": datetime(2030, 1, 2), "zone": "Crypt", "going": 12}]

        real = adapters._function
        monkeypatch.setattr(
            adapters, "_function", lambda mod, name: fake_events if name == "next_events" else real(mod, name)
        )
        async with await _anon(app) as anon:
            state = (await anon.get(f"/overlay/{token}/state")).json()
        assert [t["creature"] for t in state["timers"]] == ["Lord Grimclaw"]  # no time of death: not listed
        assert state["timers"][0]["status"] == "open" and state["timers"][0]["window_end"].endswith("Z")
        assert state["events"][0]["starts_at"] == "2030-01-02T00:00:00Z"
        assert state["events"][0]["going"] == 12
        await hall.bus.emit("watch.window_open", timer_id=timer_id)
        await hall.bus.emit("events.starting", event_id=9)
        await _drain()
        got = [sub.queue.get_nowait() for _ in range(sub.queue.qsize())]
        kinds = [m["kind"] for m in got]
        assert "refresh" in kinds
        timer = next(m for m in got if m["kind"] == "timer")
        assert timer["creature"] == "Lord Grimclaw" and timer["title"] == "Spawn window open"
        ev = next(m for m in got if m["kind"] == "event")
        assert ev["title"] == "Crypt raid" and ev["heading"] == "Starting now"
    finally:
        hub.unsubscribe(sub)
