from datetime import timedelta


def _id(location: str) -> int:
    return int(location.rstrip("/").rsplit("/", 1)[1])


def test_price_value():
    from mmgu.modules.board.services import price_value

    assert price_value("5pp") == 5000
    assert price_value("2p 5g") == 2500
    assert price_value("offer") is None
    assert price_value("") is None


async def test_listings_matching_and_pages(make_client):
    seller = await make_client("Sella", "member")
    await seller.post("/archive/new", {"name": "Rusty Scimitar", "slots": ["PRIMARY"]})
    for path in [
        "/board",
        "/board?tab=wts",
        "/board?tab=wtb",
        "/board?tab=requests",
        "/board?tab=mine",
        "/board/new",
        "/board/new?kind=WTB",
        "/board/new?kind=request",
    ]:
        r = await seller.get(path)
        assert r.status_code == 200, (path, r.text[:500])

    r = await seller.post(
        "/board/listings", {"kind": "WTS", "item": "rusty scimitar", "qty": "2", "price": "5pp", "notes": "barely used"}
    )
    assert r.status_code == 303, r.text
    wts_id = _id(r.headers["location"])
    r = await seller.get(r.headers["location"])
    assert r.status_code == 200 and "Rusty Scimitar" in r.text and "5pp" in r.text

    buyer = await make_client("Byron", "recruit")
    r = await buyer.post("/board/listings", {"kind": "WTB", "item": "Rusty Scimitar", "price": "4pp"})
    assert r.status_code == 303
    wtb_url = r.headers["location"]
    r = await buyer.get(wtb_url)
    assert "Sella" in r.text  # the matching seller shows on the WTB page
    # both parties were told
    assert "Match for Rusty Scimitar" in (await buyer.get("/notices")).text
    assert "Byron wants to buy Rusty Scimitar" in (await seller.get("/notices")).text

    r = await buyer.get("/api/board/listings?kind=WTS&q=rusty")
    ls = r.json()["listings"]
    assert len(ls) == 1 and ls[0]["price"] == "5pp" and ls[0]["author"] == "Sella"
    assert set(ls[0]) >= {"id", "kind", "title", "item", "price", "author", "status", "expires_at"}

    # item page panel and board search
    item_id = ls[0]["item_id"]
    r = await buyer.get(f"/archive/items/{item_id}")
    assert "On the Notice Board" in r.text and "Sella" in r.text
    r = await buyer.get("/board?q=rusty", headers={"HX-Request": "true", "HX-Target": "board-results"})
    assert "Rusty Scimitar" in r.text and "<html" not in r.text
    r = await buyer.get("/search?q=rusty")
    assert "WTS Rusty Scimitar" in r.text

    # only the author can close; then sold, renew
    r = await buyer.post(f"/board/listings/{wts_id}/sold")
    assert r.status_code == 422
    r = await seller.post(f"/board/listings/{wts_id}/sold")
    assert r.status_code == 303
    r = await seller.post(f"/board/listings/{wts_id}/renew")
    assert r.status_code == 303
    r = await seller.post(f"/board/listings/{wts_id}/close")
    assert r.status_code == 303
    r = await seller.get("/api/board/listings?kind=WTS")
    assert r.json()["listings"] == []


async def test_expiry_job(make_client):
    c = await make_client()
    r = await c.post("/board/listings", {"kind": "WTS", "item": "Bone Chips", "qty": "20"})
    lid = _id(r.headers["location"])
    from mmgu.db import session_scope, utcnow
    from mmgu.modules.board.models import Listing
    from mmgu.modules.board.services import expire_due

    async with session_scope() as session:
        li = await session.get(Listing, lid)
        li.expires_at = utcnow() - timedelta(minutes=1)
    async with session_scope() as session:
        assert await expire_due(session) == 1
    async with session_scope() as session:
        assert (await session.get(Listing, lid)).status == "expired"
    assert "expired" in (await c.get("/notices")).text


async def test_requests_claim_done_thank(make_client):
    asker = await make_client("Asha", "recruit")
    r = await asker.post(
        "/board/requests",
        {"category": "Port / travel", "title": "Port to Rothold", "details": "At the docks", "tradeskill": ""},
    )
    assert r.status_code == 303, r.text
    rid = _id(r.headers["location"])
    r = await asker.get(f"/board/requests/{rid}")
    assert r.status_code == 200 and "Port to Rothold" in r.text
    r = await asker.post(f"/board/requests/{rid}/claim")
    assert r.status_code == 422  # can't claim your own

    helper = await make_client("Helga", "member")
    r = await helper.post(f"/board/requests/{rid}/claim")
    assert r.status_code == 303
    assert "Helga is on it" in (await asker.get("/notices")).text
    r = await helper.post(f"/board/requests/{rid}/done")
    assert r.status_code == 303
    assert "marked" in (await asker.get("/notices")).text
    r = await helper.post(f"/board/requests/{rid}/thank")
    assert r.status_code == 422  # only the asker thanks
    r = await asker.post(f"/board/requests/{rid}/thank")
    assert r.status_code == 303
    assert "thanked you" in (await helper.get("/notices")).text
    r = await helper.get("/me")
    assert "helped 1 time" in r.text

    r = await asker.get("/api/board/requests?status=done")
    rq = r.json()["requests"][0]
    assert rq["claimed_by"] == "Helga" and rq["category"] == "Port / travel"
    assert set(rq) >= {"id", "category", "title", "author", "status", "claimed_by"}

    r = await asker.post_json("/api/board/requests", {"category": "Buffs", "title": "SoW please", "details": "Soon"})
    assert r.status_code == 200, r.text
    new_id = r.json()["id"]
    r = await asker.get("/api/board/requests?status=open")
    assert [x["id"] for x in r.json()["requests"]] == [new_id]
    r = await asker.post_json("/api/board/requests", {"category": "Nope", "title": "x"})
    assert r.status_code == 422
    r = await asker.post(f"/board/requests/{new_id}/close")
    assert r.status_code == 303
    r = await helper.get("/")
    assert r.status_code == 200 and "The Notice Board" in r.text


async def test_guest_cannot_post(make_client):
    c = await make_client("Wanderer", "guest")
    r = await c.get("/board")
    assert r.status_code == 403
    r = await c.post("/board/requests", {"category": "Other", "title": "hi"})
    assert r.status_code == 403
