import io
import json
import re

from PIL import Image


def png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (20, 20, 20)).save(buf, format="PNG")
    return buf.getvalue()


async def _mule(c, name="Bankbeast") -> int:
    r = await c.post("/roster/characters", {"name": name, "class_name": "Fighter", "level": "1", "is_bank_mule": "1"})
    assert r.status_code == 303, r.text
    r = await c.get("/vault")
    m = re.search(rf'/vault/mules/(\d+)">\s*<span class="nm">.*?{name}', r.text, re.S)
    assert m, r.text[:2000]
    return int(m.group(1))


def test_parse_lines():
    from mmgu.modules.vault.services import parse_lines

    p = parse_lines("Rusty Scimitar x3\n3 Bone Chips\nCloth Cap (2)\nbone chips\n\nSpider Silk")
    got = {ln["name"]: ln["qty"] for ln in p.lines}
    assert got == {"Rusty Scimitar": 3, "Bone Chips": 4, "Cloth Cap": 2, "Spider Silk": 1}
    tsv = (
        "Location\tName\tID\tCount\tSlots\n"
        "General1\tBackpack\t17005\t1\t8\n"
        "General1-Slot1\tRusty Scimitar\t5019\t1\t0\n"
        "Bank1\tBone Chips\t13073\t20\t0\n"
        "Bank2\tEmpty\t0\t0\t0\n"
        "SharedBank1\tCloth Cap\t1001\t1\t0\n"
    )
    p = parse_lines(tsv)
    assert p.format == "outputfile"
    assert {ln["name"] for ln in p.lines} == {"Backpack", "Rusty Scimitar", "Bone Chips", "Cloth Cap"}
    p = parse_lines(tsv, bank_only=True)
    assert {ln["name"]: ln["qty"] for ln in p.lines} == {"Bone Chips": 20, "Cloth Cap": 1}


async def test_holdings_transactions_and_bulk_update(make_client):
    c = await make_client()
    await c.post("/archive/new", {"name": "Rusty Scimitar", "slots": ["PRIMARY"]})
    mid = await _mule(c)
    for path in ["/vault", f"/vault/mules/{mid}", "/vault/history", f"/vault/mules/{mid}/update", "/vault?view=mules"]:
        r = await c.get(path)
        assert r.status_code == 200, (path, r.text[:500])

    r = await c.post("/vault/tx", {"character_id": mid, "name": "rusty scimitar", "qty": "2", "kind": "deposit"})
    assert r.status_code == 303, r.text
    r = await c.post("/vault/tx", {"character_id": mid, "name": "Bone Chips", "qty": "10", "kind": "deposit"})
    r = await c.post("/vault/tx", {"character_id": mid, "name": "Bone Chips", "qty": "3", "kind": "withdraw"})
    assert r.status_code == 303
    r = await c.post("/vault/tx", {"character_id": mid, "name": "Bone Chips", "qty": "99", "kind": "withdraw"})
    assert r.status_code == 422
    r = await c.get("/api/vault/holdings?q=")
    got = {h["item"]: h for h in r.json()["holdings"]}
    assert got["Rusty Scimitar"]["qty"] == 2 and got["Rusty Scimitar"]["item_id"]
    assert got["Bone Chips"]["qty"] == 7 and got["Bone Chips"]["item_id"] is None
    assert got["Bone Chips"]["character"] == "Bankbeast"

    # item page shows the vault panel
    r = await c.get(f"/archive/items/{got['Rusty Scimitar']['item_id']}")
    assert "In the Vault" in r.text and "Bankbeast" in r.text

    # bulk update: preview then apply
    r = await c.post(f"/vault/mules/{mid}/update/preview", {"text": "Rusty Scimitar x5\n2 Cloth Cap"})
    assert r.status_code == 200
    assert "Cloth Cap" in r.text and "Bone Chips" in r.text and "Apply these changes" in r.text
    m = re.search(r'name="lines" value="([^"]+)"', r.text)
    lines = json.loads(m.group(1).replace("&#34;", '"'))
    r = await c.post(f"/vault/mules/{mid}/update/apply", {"lines": json.dumps(lines)})
    assert r.status_code == 303
    got = {h["item"]: h["qty"] for h in (await c.get("/api/vault/holdings")).json()["holdings"]}
    assert got == {"Rusty Scimitar": 5, "Cloth Cap": 2}
    r = await c.get("/vault/history")
    assert "Inventory update" in r.text
    r = await c.get("/vault?q=cloth", headers={"HX-Request": "true", "HX-Target": "holdings"})
    assert "Cloth Cap" in r.text and "Rusty Scimitar" not in r.text and "<html" not in r.text
    assert [h["item"] for h in (await c.get("/api/vault/holdings?q=cloth")).json()["holdings"]] == ["Cloth Cap"]
    r = await c.get("/search?q=cloth")
    assert "Cloth Cap" in r.text


async def test_screenshot_reader_makes_proposal(make_client):
    from mmgu.core.hall import hall

    async def fake_reader(image: bytes):
        return [{"name": "Bone Chips", "qty": 12}]

    hall.ext.add("vault.readers", fake_reader, module="vault")
    try:
        c = await make_client()
        mid = await _mule(c)
        r = await c.post(f"/vault/mules/{mid}/read", {}, files={"screenshot": ("s.png", png(), "image/png")})
        assert r.status_code == 303, r.text
        url = r.headers["location"]
        assert "proposal=" in url
        r = await c.get(url)
        assert "Bone Chips" in r.text and "Apply these changes" in r.text
        pid = re.search(r"proposal=(\d+)", url).group(1)
        r = await c.post(
            f"/vault/mules/{mid}/update/apply",
            {"lines": json.dumps([{"name": "Bone Chips", "qty": 12}]), "proposal_id": pid},
        )
        assert r.status_code == 303
        r = await c.get("/api/proposals?status=pending")
        assert all(p["kind"] != "vault.holdings" for p in r.json()["proposals"])
    finally:
        hall.ext._slots["vault.readers"] = [x for x in hall.ext._slots["vault.readers"] if x.fn is not fake_reader]


async def test_requests_flow(make_client):
    banker = await make_client()
    mid = await _mule(banker)
    await banker.post("/vault/tx", {"character_id": mid, "name": "Bone Chips", "qty": "10", "kind": "deposit"})
    member = await make_client("Mira", "member")
    r = await member.get("/vault/requests/new?item=Bone+Chips")
    assert r.status_code == 200 and "The bank holds 10" in r.text
    r = await member.post("/vault/requests", {"name": "bone chips", "qty": "4", "reason": "Tailoring"})
    assert r.status_code == 303
    req_url = r.headers["location"]
    rid = int(req_url.rsplit("/", 1)[1])
    for path in [req_url, "/vault/requests", "/vault/requests?tab=mine", "/vault/requests?tab=done"]:
        r = await member.get(path)
        assert r.status_code == 200, path
    # members can't approve
    r = await member.post(f"/vault/requests/{rid}/approve")
    assert r.status_code == 403
    r = await banker.get("/api/vault/requests?status=pending")
    reqs = r.json()["requests"]
    assert reqs[0]["item"] == "Bone Chips" and reqs[0]["requester"] == "Mira" and reqs[0]["qty"] == 4
    assert set(reqs[0]) >= {"id", "item", "qty", "requester", "status", "created_at"}
    r = await banker.get("/")
    assert "waiting for a banker" in r.text
    r = await banker.post(f"/vault/requests/{rid}/approve")
    assert r.status_code == 303
    r = await banker.post(f"/vault/requests/{rid}/fulfil", {"character_id": mid})
    assert r.status_code == 303, r.text
    got = {h["item"]: h["qty"] for h in (await banker.get("/api/vault/holdings")).json()["holdings"]}
    assert got["Bone Chips"] == 6
    r = await member.get("/notices")
    assert "approved" in r.text and "handing you 4" in r.text
    # a second request, cancelled by its owner; another denied
    r = await member.post("/vault/requests", {"name": "Cloth Cap", "qty": "1"})
    rid2 = int(r.headers["location"].rsplit("/", 1)[1])
    r = await member.post(f"/vault/requests/{rid2}/cancel")
    assert r.status_code == 303
    r = await member.post("/vault/requests", {"name": "Cloth Cap", "qty": "1"})
    rid3 = int(r.headers["location"].rsplit("/", 1)[1])
    r = await banker.post(f"/vault/requests/{rid3}/deny", {"note": "None left"})
    assert r.status_code == 303
    r = await member.get("/notices")
    assert "None left" in r.text


async def test_recruit_cannot_see_vault(make_client):
    c = await make_client("Rookie", "recruit")
    r = await c.get("/vault")
    assert r.status_code == 403
    r = await c.get("/api/vault/holdings")
    assert r.status_code == 403
