import io

from PIL import Image


def png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (20, 20, 20)).save(buf, format="PNG")
    return buf.getvalue()


async def test_catalog_search_and_drop(make_client):
    c = await make_client()
    data = {
        "name": "Rusty Scimitar",
        "slots": ["PRIMARY", "SECONDARY"],
        "skill": "1HS",
        "damage": "5",
        "delay": "36",
        "weight": "7.0",
        "size": "MEDIUM",
        "all_classes": "1",
        "races": "ALL",
        "stat_str": "2",
        "creature": "a decaying skeleton",
        "zone": "Scarwood",
    }
    r = await c.post("/archive/new", data, files={"screenshot": ("s.png", png(), "image/png")})
    assert r.status_code == 303, r.text
    url = r.headers["location"]
    r = await c.get(url)
    assert "Rusty Scimitar" in r.text and "Scarwood" in r.text and "STR" in r.text
    r = await c.get("/archive?q=scim")
    assert "Rusty Scimitar" in r.text
    r = await c.get("/archive?slot=PRIMARY&cls=Wizard")
    assert "Rusty Scimitar" in r.text
    r = await c.get("/archive/loot?zone=Scarwood")
    assert "Rusty Scimitar" in r.text
    # duplicate name folds into the existing item
    r = await c.post("/archive/new", {"name": "rusty  scimitar", "creature": "a rat"})
    assert r.headers["location"] == url
    r = await c.get("/api/archive/items?q=Rusty")
    assert r.json()["items"][0]["name"] == "Rusty Scimitar"


async def test_api_drop_unknown_item_becomes_proposal(make_client):
    c = await make_client()
    r = await c.post_json("/api/archive/drops", {"item": "Mystery Ring", "creature": "an imp", "zone": "Rothold"})
    assert r.json()["status"] == "proposed"
    r = await c.get("/proposals")
    assert "Mystery Ring" in r.text


async def test_recruit_cannot_edit_others_items(make_client):
    lead = await make_client()
    r = await lead.post("/archive/new", {"name": "Cloth Cap", "slots": ["HEAD"]})
    item_url = r.headers["location"]
    rookie = await make_client("Rookie", "recruit")
    r = await rookie.get(item_url + "/edit")
    assert r.status_code == 403
