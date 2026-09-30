"""The three add-ons (screenshot reader, ledger sync, MCP server) and the ledger companion script.

No test needs Ollama, Anthropic or the network: HTTP is served by httpx.MockTransport or the app itself.
"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

REPO = Path(__file__).resolve().parents[1]


def png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (60, 40), (20, 20, 20)).save(buf, format="PNG")
    return buf.getvalue()


async def enable(client, module_id: str) -> None:
    r = await client.post(f"/steward/modules/{module_id}", {"enable": "1", "ack": "yes"})
    assert r.status_code in (303, 204), r.text


async def api_token(client) -> str:
    r = await client.post("/me/tokens", {"name": "tests"})
    assert r.status_code in (303, 204), r.text
    r = await client.get("/me")
    return re.search(r"mmgu_[A-Za-z0-9_\-]+", r.text).group(0)


def b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


# ================================================================================================
# Screenshot Reader
# ================================================================================================
MODEL_ITEM = {
    "raw_text": "Fine Steel Scimitar\nMAGIC ITEM LORE ITEM\nSlot: PRIMARY SECONDARY\n"
    "Skill: 1H Slashing Atk Delay: 36\nDMG: 5\nSTR: +2 AGI: +3\nSV Fire: +4\nWT: 7.0 Size: MEDIUM\n"
    "Class: WAR PAL RNG SHD\nRace: ALL",
    "name": "Fine Steel Scimitar",
    "item_type": "Weapon",
    "slots": ["PRIMARY", "SECONDARY"],
    "flags": ["MAGIC ITEM", "LORE ITEM"],
    "classes": ["WAR", "PAL", "RNG", "SHD"],
    "races": ["ALL"],
    "skill": "1H Slashing",
    "damage": 5,
    "delay": 36,
    "ac": None,
    "weight": 7.0,
    "size": "MEDIUM",
    "stats": {},  # left empty on purpose: the reader fills it from raw_text
    "resists": {},
    "effects": [],
}


@pytest.fixture
def ollama(monkeypatch):
    """A fake Ollama. ``calls`` records every request; set ``state['down']`` to refuse connections."""
    from mmgu.addons.screenshot_reader import reader

    state = {"calls": [], "down": False, "reply": MODEL_ITEM}
    reader._capabilities.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        if state["down"]:
            raise httpx.ConnectError("refused", request=request)
        body = json.loads(request.content or b"{}")
        state["calls"].append((request.url.path, body))
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion", "vision", "thinking"]})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3-vl:8b", "size": 6_100_000_000}]})
        if request.url.path == "/api/chat":
            return httpx.Response(200, json={"message": {"role": "assistant", "content": json.dumps(state["reply"])}})
        return httpx.Response(404)

    monkeypatch.setattr(reader, "_client", lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kw))
    return state


async def test_reader_reads_item_and_fills_gaps(app, ollama):
    from mmgu.addons.screenshot_reader import reader
    from mmgu.modules.archive.services import clean_fields

    res = await reader.read_item(png())
    assert res.source == "ollama:qwen3-vl:8b"
    assert res.fields["stats"] == {"str": 2, "agi": 3}
    assert res.fields["resists"] == {"fr": 4}
    assert any("WAR" in n for n in res.notes)  # M&M has no Warrior class: flagged for the person checking
    path, body = ollama["calls"][-1]
    assert path == "/api/chat"
    assert body["stream"] is False and body["options"]["temperature"] == 0
    assert body["format"]["properties"]["slots"]["items"]["enum"][0] == "CHARM"
    assert "str" in body["format"]["properties"]["stats"]["properties"]
    assert body["messages"][0]["images"] and body["messages"][-1]["role"] == "assistant"  # skip-thinking prefill
    cleaned = clean_fields(res.fields)
    assert cleaned["classes"] == ["Paladin", "Ranger", "Shadow Knight"]
    assert cleaned["flags"] == ["magic", "lore"] and cleaned["skill"] == "1HS"


def test_fields_from_text_parses_inspect_window(app):
    from mmgu.addons.screenshot_reader.reader import fields_from_text

    f = fields_from_text(
        "Cloak of Embers\nMAGIC ITEM NO DROP\nSlot: BACK\nAC: 6\nHP: +15 HP Regen: +1 MANA: +10\n"
        "SV Fire: +10 SV Cold: -2\nEffect: Warmth\nWT: 1.5 Size: SMALL\nClass: ALL\nRace: HUM ELF"
    )
    assert f["name"] == "Cloak of Embers"
    assert f["flags"] == ["MAGIC ITEM", "NO DROP"]
    assert f["slots"] == ["BACK"] and f["ac"] == 6
    assert f["stats"] == {"hp": 15, "hp_regen": 1, "mana": 10}
    assert f["resists"] == {"fr": 10, "cr": -2}
    assert f["effects"] == ["Warmth"] and f["weight"] == 1.5 and f["size"] == "SMALL"
    assert f["classes"] == ["ALL"] and f["races"] == ["HUM", "ELF"]


async def test_reader_plugs_into_archive_and_pages(make_client, ollama):
    c = await make_client()
    await enable(c, "screenshot_reader")
    r = await c.get("/archive/new")
    assert "Read a screenshot" in r.text
    r = await c.post("/archive/read", {"creature": "a bandit"}, files={"screenshot": ("s.png", png(), "image/png")})
    assert r.status_code == 303 and "proposal=" in r.headers["location"]
    r = await c.get(r.headers["location"])
    assert "Fine Steel Scimitar" in r.text and "ollama:qwen3-vl:8b" in r.text

    r = await c.get("/addons/screenshot-reader")
    assert r.status_code == 200 and "Test the reader" in r.text and "qwen3-vl:8b" in r.text
    r = await c.post(
        "/addons/screenshot-reader/test", {"kind": "item"}, files={"screenshot": ("s.png", png(), "image/png")}
    )
    assert r.status_code == 200 and "What the model returned" in r.text and "Fine Steel Scimitar" in r.text
    r = await c.post("/addons/screenshot-reader/check")
    assert r.status_code == 200 and 'id="ollama-check"' in r.text and "is ready" in r.text

    ollama["down"] = True
    r = await c.post("/addons/screenshot-reader/check")
    assert "Not connected" in r.text
    r = await c.post("/archive/read", {}, files={"screenshot": ("t.png", png(), "image/png")})
    assert r.status_code == 303  # falls back to typing it in by hand


async def test_reader_page_needs_leader(make_client, ollama):
    lead = await make_client()
    await enable(lead, "screenshot_reader")
    officer = await make_client("Officer Olin", "officer")
    assert (await officer.get("/addons/screenshot-reader")).status_code == 403


async def test_bank_reader(app, ollama):
    from mmgu.addons.screenshot_reader import reader

    ollama["reply"] = {
        "items": [{"name": " Bone  Chips ", "qty": 12}, {"name": "", "qty": 1}, {"name": "Rat Ear", "qty": 0}]
    }
    assert await reader.read_bank(png()) == [{"name": "Bone Chips", "qty": 12}, {"name": "Rat Ear", "qty": 1}]
    ollama["reply"] = {"items": []}
    assert await reader.read_bank(png()) is None


async def test_reader_with_claude(app, monkeypatch):
    from mmgu.addons.screenshot_reader import reader
    from mmgu.core import store

    monkeypatch.setitem(store._cache, "screenshot_reader.provider", "anthropic")
    monkeypatch.setitem(store._cache, "screenshot_reader.anthropic_api_key", "sk-test")
    seen = {}

    class Messages:
        async def create(self, **kw):
            seen.update(kw)
            return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=dict(MODEL_ITEM))])

    monkeypatch.setattr(reader, "_anthropic_client", lambda key: SimpleNamespace(messages=Messages()))
    res = await reader.read_item(png())
    assert res.source == "claude:claude-haiku-4-5"
    assert res.fields["name"] == "Fine Steel Scimitar" and res.fields["stats"]["agi"] == 3
    assert seen["tool_choice"] == {"type": "tool", "name": "record_item"}
    assert seen["messages"][0]["content"][0]["type"] == "image"

    monkeypatch.setitem(store._cache, "screenshot_reader.anthropic_api_key", None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(reader.ReaderError, match="API key"):
        await reader.read_item(png())


# ================================================================================================
# Ledger Sync
# ================================================================================================
def _events(now: datetime) -> list[dict]:
    at = now.replace(microsecond=0).isoformat() + "Z"
    return [
        {
            "type": "loot",
            "at": at,
            "item": "Rusty Scimitar",
            "creature": "a bandit",
            "zone": "Scarwood",
            "raw": {"event_id": "a"},
        },
        {
            "type": "loot",
            "at": at,
            "item": "Glowing Shard",
            "creature": "a wisp",
            "zone": "Scarwood",
            "raw": {"event_id": "b"},
        },
        {
            "type": "loot",
            "at": at,
            "item": "glowing  shard",
            "creature": "a wisp",
            "zone": "Scarwood",
            "raw": {"event_id": "c"},
        },
        {"type": "kill", "at": at, "creature": "Lord Grimclaw", "zone": "Scarwood", "raw": {"event_id": "d"}},
        {
            "type": "kill",
            "at": (now - timedelta(days=10)).isoformat(),
            "creature": "Old Kill",
            "raw": {"event_id": "e"},
        },
        {"type": "vendor_sale", "at": at, "item": "Rat Ear", "qty": 3, "raw": {"event_id": "f"}},
        {"type": "harvest", "at": at, "item": "Copper Ore", "raw": {"event_id": "g"}},
        {"type": "loot", "at": "yesterday-ish", "item": "Bad Time", "raw": {"event_id": "h"}},
    ]


async def test_ledger_import(make_client, monkeypatch):
    from mmgu.core.hall import hall
    from mmgu.modules.watch import services as watch_services

    c = await make_client()
    await enable(c, "ledger_sync")
    r = await c.post("/archive/new", {"name": "Rusty Scimitar", "slots": ["PRIMARY"]})
    item_url = r.headers["location"]
    token = await api_token(c)

    reported = []

    async def fake_report(session, viewer, name, died_at, source="manual"):
        reported.append((name, died_at, source))
        return object() if name == "Lord Grimclaw" else None

    real_enabled = hall.is_enabled
    monkeypatch.setattr(hall, "is_enabled", lambda mid: True if mid == "watch" else real_enabled(mid))
    monkeypatch.setattr(watch_services, "report_death_by_name", fake_report)

    body = {"character": "Lyra", "server": "Oakvale", "events": _events(datetime.now(UTC).replace(tzinfo=None))}
    headers = {"Authorization": f"Bearer {token}"}
    r = await c.http.post("/api/addons/ledger/events", json=body, headers=headers)
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["received"] == 8 and got["invalid"] == 1 and got["duplicates"] == 0
    assert got["drops_recorded"] == 1 and got["unknown_items"] == 2 and got["items_proposed"] == 1
    assert got["kills"] == 2 and got["timers_updated"] == 1 and got["other"] == 2
    assert [n for n, _, _ in reported] == ["Lord Grimclaw"]  # the 10-day-old kill is not sent to the Watch
    assert reported[0][2] == "ledger"

    r = await c.http.post("/api/addons/ledger/events", json=body, headers=headers)
    again = r.json()
    assert again["duplicates"] == 7 and again["drops_recorded"] == 0 and again["items_proposed"] == 0

    r = await c.get(item_url)
    assert "a bandit" in r.text
    r = await c.get("/proposals")
    assert "Glowing Shard" in r.text
    r = await c.get("/addons/ledger")
    assert r.status_code == 200 and "Lyra" in r.text and "Set up the companion" in r.text


async def test_ledger_rejects_bad_input_and_tokens(make_client):
    c = await make_client()
    r = await c.http.post(
        "/api/addons/ledger/events", json={"character": "X", "events": []}, headers={"X-CSRF-Token": c.csrf}
    )
    assert r.status_code == 404  # add-on is off until a leader turns it on
    await enable(c, "ledger_sync")
    r = await c.post_json("/api/addons/ledger/events", {"events": []})
    assert r.status_code == 422 and "character" in r.json()["detail"]
    r = await c.post_json("/api/addons/ledger/events", {"character": "X", "events": [{}] * 1001})
    assert r.status_code == 422
    r = await c.http.post(
        "/api/addons/ledger/events",
        json={"character": "X", "events": []},
        headers={"Authorization": "Bearer mmgu_nope"},
    )
    assert r.status_code == 401
    guest = await make_client("Guest Gil", "guest")
    r = await guest.post_json("/api/addons/ledger/events", {"character": "X", "events": []})
    assert r.status_code == 403


async def test_ledger_kills_without_watch_are_counted(make_client, monkeypatch):
    from mmgu.core.hall import hall

    c = await make_client()
    await enable(c, "ledger_sync")
    real_enabled = hall.is_enabled
    monkeypatch.setattr(hall, "is_enabled", lambda mid: False if mid == "watch" else real_enabled(mid))
    at = datetime.now(UTC).isoformat()
    r = await c.post_json(
        "/api/addons/ledger/events", {"character": "Lyra", "events": [{"type": "kill", "at": at, "creature": "a rat"}]}
    )
    assert r.json()["kills"] == 1 and r.json()["timers_updated"] == 0


# ================================================================================================
# Companion script
# ================================================================================================
@pytest.fixture
def companion(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("ledger_sync_companion", REPO / "companion" / "ledger_sync.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    home = tmp_path / "script"
    home.mkdir()
    monkeypatch.setattr(mod, "CONFIG_FILE", home / "ledger_sync_config.json")
    monkeypatch.setattr(mod, "STATE_FILE", home / "ledger_sync_state.json")
    for var in ("HALLKEEPER_URL", "HALLKEEPER_TOKEN", "HALLKEEPER_GAME_DIR"):
        monkeypatch.delenv(var, raising=False)
    yield mod
    sys.modules.pop(spec.name, None)


def _fake_game(tmp_path: Path) -> Path:
    game = tmp_path / "Monsters and Memories"
    ledger = game / "Oakvale" / "Lyra" / "Ledger"
    ledger.mkdir(parents=True)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.1234567Z")
    events = [
        {
            "f01": "act_13",
            "f02": "x",
            "f03": json.dumps({"d02": b64("a bandit"), "d04": "6642399|Rusty Scimitar", "d05": "6642399"}),
            "f04": now,
            "f05": "zone_" + b64("evergrove"),
        },
        {
            "f01": "act_14",
            "f03": json.dumps({"d13": b64("Lord Grimclaw"), "d12": b64("0,1,2,3")}),
            "f04": now,
            "f05": "zone_" + b64("scarwood"),
        },
        {"f01": "act_14", "f03": json.dumps({"d13": b64("party_split")}), "f04": now},
        {
            "f01": "act_24",
            "f02": "name_x",
            "f03": json.dumps({"d04": "Rat Ear", "d01": 3, "d03": b64("0 platinum 0 gold 1 silver 2 copper")}),
            "f04": now,
        },
        {"f01": "act_27", "f03": json.dumps({"d04": "Copper Ore"}), "f04": now},
        {"f01": "act_99", "f03": json.dumps({"d04": "chat with someone"}), "f04": now},  # never sent
        {"f01": "act_13", "f03": json.dumps({"d02": "ref_123", "d04": "Bone Chips"}), "f04": "2020-01-01T00:00:00Z"},
    ]
    (ledger / "Lyra_Character_2026-09-30.json").write_text(json.dumps({"c01": events}))
    (ledger / "notes.json").write_text("{}")  # not a ledger file
    return game


def test_companion_parses_ledger(companion, tmp_path):
    game = _fake_game(tmp_path)
    before = {p: p.read_bytes() for p in game.rglob("*") if p.is_file()}
    groups = companion.collect(game, datetime.now(UTC) - timedelta(days=7), set())
    assert list(groups) == [("Lyra", "Oakvale")]
    evs = groups[("Lyra", "Oakvale")]
    by_type = {e["type"]: e for e in evs}
    assert len(evs) == 4  # party_split, unknown code and the old event are skipped
    assert by_type["loot"]["item"] == "Rusty Scimitar" and by_type["loot"]["creature"] == "a bandit"
    assert by_type["loot"]["zone"] == "Evershade Weald"
    assert by_type["kill"]["creature"] == "Lord Grimclaw" and by_type["kill"]["zone"] == "Scarwood"
    assert by_type["kill"]["raw"]["coin_copper"] == 10203
    assert by_type["vendor_sale"]["qty"] == 3 and by_type["harvest"]["item"] == "Copper Ore"
    assert by_type["loot"]["at"].endswith("Z")
    assert len(companion.collect(game, None, set())[("Lyra", "Oakvale")]) == 5  # --days 0 includes old events
    assert {p: p.read_bytes() for p in game.rglob("*") if p.is_file()} == before  # read-only


def test_companion_time_formats(companion):
    utc = companion.parse_time("2026-09-30T12:00:00.1234567Z")
    assert utc == datetime(2026, 9, 30, 12, 0, 0, 123456, tzinfo=UTC)
    assert companion.parse_time("2026-09-30T14:00:00+02:00") == datetime(2026, 9, 30, 12, tzinfo=UTC)
    assert companion.parse_time(1790000000000) == companion.parse_time(1790000000)
    assert companion.parse_time("09/30/2026 12:00:00 PM") is not None
    assert companion.parse_time("soon") is None


def test_companion_requires_notice_and_never_sends_on_dry_run(companion, tmp_path, capsys, monkeypatch):
    game = _fake_game(tmp_path)
    sent = []
    monkeypatch.setattr(companion, "post_batch", lambda *a: sent.append(a) or {})
    code = companion.main(["--once", "--game-dir", str(game), "--hall", "http://hall"])
    assert code == 1 and "BEFORE YOU USE THIS" in capsys.readouterr().out and not sent
    code = companion.main(["--dry-run", "--game-dir", str(game)])
    out = capsys.readouterr().out
    assert code == 0 and "Would send for Lyra" in out and not sent
    assert not companion.STATE_FILE.exists()


async def test_companion_batch_is_accepted_by_hall(companion, make_client, tmp_path, monkeypatch):
    """What the companion builds is exactly what the hall's endpoint accepts, and it isn't resent."""
    c = await make_client()
    await enable(c, "ledger_sync")
    await c.post("/archive/new", {"name": "Rusty Scimitar", "slots": ["PRIMARY"]})
    game = _fake_game(tmp_path)
    companion.save_json(companion.CONFIG_FILE, {"hall_url": "http://hall", "token": "mmgu_x"})
    bodies = []
    monkeypatch.setattr(companion, "post_batch", lambda url, token, body: bodies.append(body) or {"drops_recorded": 1})
    assert companion.main(["--once", "--i-understand", "--game-dir", str(game)]) == 0
    assert json.loads(companion.CONFIG_FILE.read_text())["i_understand"] is True
    assert len(bodies) == 1 and bodies[0]["character"] == "Lyra"
    r = await c.post_json("/api/addons/ledger/events", bodies[0])
    assert r.status_code == 200 and r.json()["drops_recorded"] == 1
    assert companion.main(["--once", "--game-dir", str(game)]) == 0
    assert len(bodies) == 1  # remembered what it sent

    with pytest.raises(SystemExit):
        companion.save_json(game / "oops.json", {}, game)  # never writes inside the game folder


# ================================================================================================
# MCP server
# ================================================================================================
def mock_api(routes: dict):
    """A HallApi whose HTTP calls are answered from ``routes[(method, path)]``; records requests."""
    from mmgu.addons.mcp_server.server import HallApi

    log = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        log.append(
            (request.method, request.url.path, dict(request.url.params), body, request.headers.get("authorization"))
        )
        found = routes.get((request.method, request.url.path))
        if found is None:
            return httpx.Response(404, json={"detail": "That part of the hall is closed."})
        status, payload = found if isinstance(found, tuple) else (200, found)
        return httpx.Response(status, json=payload)

    return HallApi("http://hall.test", "mmgu_tok", transport=httpx.MockTransport(handler)), log


async def test_mcp_tools_shape_requests_and_answers():
    from mmgu.addons.mcp_server import server as s

    item = {
        "id": 7,
        "name": "Rusty Scimitar",
        "text": "Slot: PRIMARY\nDMG: 5",
        "url": "http://hall.test/archive/items/7",
    }
    api, log = mock_api(
        {
            ("GET", "/api/archive/items"): {"items": [item]},
            ("GET", "/api/archive/items/7"): {
                **item,
                "drops": [{"creature": "a bandit", "zone": "Scarwood", "count": 2}],
            },
            ("POST", "/api/archive/drops"): {"status": "proposed", "proposal_id": 3},
            ("GET", "/api/roster"): {
                "characters": [
                    {"name": "Lyra", "level": 20, "class": "Wizard", "race": "Human", "main": True, "member": "Lyra P"}
                ]
            },
            ("GET", "/api/vault/holdings"): {
                "holdings": [{"item": "Bone Chips", "item_id": None, "qty": 40, "character": "Mule"}]
            },
            ("GET", "/api/vault/requests"): {
                "requests": [
                    {
                        "id": 1,
                        "item": "Cloth Cap",
                        "qty": 1,
                        "requester": "Bo",
                        "status": "pending",
                        "created_at": "2026-09-30T10:00:00",
                    }
                ]
            },
            ("GET", "/api/board/listings"): {
                "listings": [
                    {
                        "id": 2,
                        "kind": "WTS",
                        "title": "Rusty Scimitar",
                        "item": "Rusty Scimitar",
                        "price": "5g",
                        "author": "Bo",
                        "status": "open",
                    }
                ]
            },
            ("GET", "/api/board/requests"): {
                "requests": [
                    {
                        "id": 4,
                        "category": "crafting",
                        "title": "Need a bow",
                        "author": "Bo",
                        "status": "claimed",
                        "claimed_by": "Lyra",
                    }
                ]
            },
            ("POST", "/api/board/requests"): {"id": 9},
            ("GET", "/api/crafters/whocan"): {
                "results": [
                    {"character": "Lyra", "member": "Lyra P", "skill": "Smithing", "level": 120, "recipe": None}
                ]
            },
            ("GET", "/api/events"): {
                "events": [
                    {
                        "id": 5,
                        "title": "Crypt raid",
                        "kind": "raid",
                        "starts_at": "2026-10-01T02:00:00",
                        "zone": "Tel Ekir",
                        "signups": 12,
                        "url": "http://hall.test/events/5",
                    }
                ]
            },
            ("GET", "/api/watch/timers"): {
                "timers": [
                    {
                        "id": 1,
                        "creature": "Lord Grimclaw",
                        "zone": "Scarwood",
                        "window_start": "2026-10-01T00:00:00",
                        "window_end": "2026-10-01T04:00:00",
                        "status": "waiting",
                    }
                ]
            },
            ("POST", "/api/watch/tod"): {
                "timer_id": 1,
                "window_start": "2026-10-01T00:00:00",
                "window_end": "2026-10-01T04:00:00",
            },
            ("GET", "/api/proposals"): {
                "proposals": [
                    {
                        "id": 3,
                        "kind": "archive.item",
                        "source": "claude",
                        "summary": "New item",
                        "created_at": "2026-09-30T10:00:00",
                    }
                ]
            },
            ("POST", "/api/proposals"): {"id": 11, "status": "pending"},
        }
    )
    assert "#7 Rusty Scimitar: Slot: PRIMARY | DMG: 5" in await s.search_items(api, "rusty", slot="PRIMARY")
    assert log[-1][2] == {"q": "rusty", "slot": "PRIMARY"} and log[-1][4] == "Bearer mmgu_tok"
    detail = await s.get_item(api, "Rusty Scimitar")
    assert "a bandit in Scarwood (2x)" in detail and "items/7" in detail
    assert "suggestion (#3)" in await s.report_drop(api, "Mystery Ring", "an imp")
    assert log[-1][3]["source"] == "claude"
    assert "Lyra: level 20 Human Wizard (Lyra P, main)" in await s.roster(api, min_level=10)
    assert "40 x Bone Chips (on Mule)" in await s.vault_holdings(api, "bone")
    assert "#1 1 x Cloth Cap for Bo" in await s.vault_requests(api)
    assert "[WTS] Rusty Scimitar for 5g" in await s.board_listings(api, "wts")
    assert log[-1][2]["kind"] == "WTS"
    assert "claimed by Lyra" in await s.board_requests(api)
    assert "#9" in await s.post_board_request(api, "crafting", "Need arrows", "200 please")
    assert log[-1][3] == {"category": "crafting", "title": "Need arrows", "details": "200 please"}
    assert "Smithing 120" in await s.who_can_craft(api, "smith")
    assert "Crypt raid (raid) at 2026-10-01 02:00 UTC in Tel Ekir, 12 signed up" in await s.upcoming_events(api)
    assert "Lord Grimclaw (Scarwood): waiting" in await s.spawn_timers(api)
    assert "Next spawn window" in await s.report_tod(api, "Lord Grimclaw", 5)
    assert log[-1][3] == {"creature": "Lord Grimclaw", "minutes_ago": 5}
    assert "#3 [archive.item] New item" in await s.list_proposals(api)
    out = await s.propose_change(api, "roster.update", "Lyra is now level 21", {"character": "Lyra", "level": 21})
    assert "Nothing has changed yet" in out and log[-1][3]["payload"] == {"character": "Lyra", "level": 21}
    await api.aclose()


async def test_mcp_errors_are_plain_text():
    from mmgu.addons.mcp_server import server as s

    api, _ = mock_api({("GET", "/api/archive/items"): (401, {"detail": "no"})})
    assert "rejected the API token" in await s.search_items(api, "x")
    api, _ = mock_api({})
    assert "closed" in await s.spawn_timers(api)


async def test_mcp_server_registers_all_tools():
    from mmgu.addons.mcp_server import server as s

    api, _ = mock_api({("GET", "/api/watch/timers"): {"timers": []}})
    mcp = s.build_server(api)
    names = {t.name for t in await mcp.list_tools()}
    assert names == {
        "search_items",
        "get_item",
        "report_drop",
        "roster",
        "vault_holdings",
        "vault_requests",
        "board_listings",
        "board_requests",
        "post_board_request",
        "who_can_craft",
        "upcoming_events",
        "spawn_timers",
        "report_tod",
        "list_proposals",
        "propose_change",
    }
    result = await mcp.call_tool("spawn_timers", {})
    assert "No spawn timers" in json.dumps(result.model_dump() if hasattr(result, "model_dump") else str(result))


async def test_mcp_against_real_hall(app, make_client):
    """The MCP client talks to the actual API with a real token and gets the hall's permissions."""
    from mmgu.addons.mcp_server import server as s

    c = await make_client()
    await enable(c, "mcp_server")
    await c.post("/archive/new", {"name": "Rusty Scimitar", "slots": ["PRIMARY"], "creature": "a bandit"})
    token = await api_token(c)
    api = s.HallApi("http://test", token, transport=httpx.ASGITransport(app=app))
    assert "Rusty Scimitar" in await s.search_items(api, "Rusty")
    assert "a bandit" in await s.get_item(api, "Rusty Scimitar")
    assert "Suggestion #" in await s.propose_change(api, "note", "Please add Lord Grimclaw to the Watch")
    assert "Please add Lord Grimclaw" in await s.list_proposals(api)
    await api.aclose()
    r = await c.get("/addons/claude")
    assert r.status_code == 200 and "claude mcp add hallkeeper" in r.text and token not in r.text


def test_mcp_needs_env(monkeypatch):
    from mmgu.addons.mcp_server import server as s

    monkeypatch.delenv("MMGU_API_URL", raising=False)
    with pytest.raises(SystemExit):
        s.HallApi.from_env()
