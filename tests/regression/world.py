"""Seed one of everything through the real web forms, so crawls can fill every path parameter."""

from __future__ import annotations

import io
import re
from datetime import UTC, datetime, timedelta

from PIL import Image
from sqlalchemy import text

from mmgu.core.hall import hall
from mmgu.db import session_scope

XSS = '<script>alert("x")</script>'


def png(color=(20, 20, 20)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), color).save(buf, format="PNG")
    return buf.getvalue()


def future(days: int = 2) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).strftime("%Y-%m-%dT20:00")


async def _ok(r, what: str):
    assert r.status_code in (200, 204, 303), f"seeding {what} failed: {r.status_code} {r.text[:300]}"
    return r


async def enable_all_modules(lead) -> None:
    """Open every room and add-on (accepting add-on notices) so nothing is skipped."""
    for m in hall.modules.values():
        if m.kind != "core" and not hall.is_enabled(m.id):
            await _ok(await lead.post(f"/steward/modules/{m.id}", {"enable": "1", "ack": "yes"}), f"enable {m.id}")


async def seed_world(lead, *, hostile: bool = False) -> dict[str, int]:
    """Create records in every module as the leader. ``hostile`` puts script tags in user-entered text."""
    await enable_all_modules(lead)
    tag = XSS if hostile else ""
    await _ok(
        await lead.post(
            "/roster/characters",
            {"name": "Aldric", "class_name": "Paladin", "level": "30", "is_main": "1", "notes": f"main {tag}"},
        ),
        "main",
    )
    await _ok(
        await lead.post(
            "/roster/characters", {"name": "Coffer", "class_name": "Bard", "level": "5", "is_bank_mule": "1"}
        ),
        "mule",
    )
    await _ok(
        await lead.post(
            "/archive/new",
            {
                "name": f"Rusty Scimitar{tag}",
                "slots": ["PRIMARY"],
                "skill": "1HS",
                "damage": "5",
                "delay": "36",
                "all_classes": "1",
                "creature": "a skeleton",
                "zone": "Scarwood",
                "description": f"notes {tag}",
            },
            files={"screenshot": ("s.png", png(), "image/png")},
        ),
        "item",
    )
    async with session_scope() as s:
        mule = (await s.execute(text("select id from core_characters where name='Coffer'"))).scalar_one()
        item_name = (await s.execute(text("select name from archive_items order by id limit 1"))).scalar_one()
    if hall.is_enabled("vault"):
        await _ok(
            await lead.post("/vault/tx", {"character_id": mule, "name": item_name, "qty": "3", "kind": "deposit"}),
            "deposit",
        )
        await _ok(
            await lead.post("/vault/requests", {"name": item_name, "qty": "1", "reason": f"for raid {tag}"}),
            "vault request",
        )
    if hall.is_enabled("board"):
        await _ok(
            await lead.post("/board/listings", {"kind": "WTS", "item": item_name, "price": "5pp", "notes": tag}),
            "listing",
        )
        await _ok(
            await lead.post(
                "/board/requests", {"category": "Crafting", "title": f"Need a bow {tag}", "details": "please"}
            ),
            "board request",
        )
    if hall.is_enabled("crafters"):
        async with session_scope() as s:
            main = (await s.execute(text("select id from core_characters where name='Aldric'"))).scalar_one()
        await _ok(
            await lead.post("/crafters/mine", {f"new-skill-{main}": "Blacksmithing", f"new-lvl-{main}": "80"}), "skill"
        )
        await _ok(
            await lead.post(
                "/crafters/recipes",
                {
                    "skill": "Blacksmithing",
                    "result": item_name,
                    "trivial": "60",
                    "components": "2 Bronze Bar",
                    "notes": tag,
                },
            ),
            "recipe",
        )
    if hall.is_enabled("events"):
        r = await _ok(
            await lead.post(
                "/events/new",
                {
                    "title": f"Crypt night {tag}",
                    "kind": "Raid",
                    "starts_at": future(),
                    "tz_name": "UTC",
                    "zone": "Scarwood",
                    "description": tag,
                    "role_tank": "1",
                },
            ),
            "event",
        )
        eid = int(r.headers["location"].rsplit("/", 1)[1])
        await _ok(await lead.post(f"/events/{eid}/signup", {"status": "going"}), "signup")
        await _ok(await lead.post(f"/events/{eid}/attendance", {"present": "1"}), "attendance")
        await _ok(
            await lead.post(
                "/loot", {"item": item_name, "character": "Aldric", "event_id": str(eid), "method": "Roll"}
            ),
            "loot",
        )
    if hall.is_enabled("watch"):
        await _ok(
            await lead.post(
                "/watch/timers",
                {
                    "creature": f"Lord Grimjaw{tag}",
                    "zone": "Scarwood",
                    "respawn_minutes": "60",
                    "variance_minutes": "10",
                    "active": "1",
                },
            ),
            "timer",
        )
        async with session_scope() as s:
            tid = (await s.execute(text("select id from watch_timers order by id limit 1"))).scalar_one()
        await _ok(await lead.post(f"/watch/timers/{tid}/tod", {"when": "now"}), "tod")
        await _ok(await lead.post("/watch/camps", {"zone": "Scarwood", "camp": f"Ogre hill {tag}"}), "camp")
    if hall.is_enabled("overlay"):
        await _ok(
            await lead.post(
                "/beacon/new",
                {
                    "name": "Main",
                    "position": "bottom-right",
                    "theme": "window",
                    "scale": "100",
                    "card_seconds": "30",
                    "timers_count": "3",
                    "feeds": ["item", "text"],
                    "active": "1",
                },
            ),
            "scene",
        )
    await _ok(await lead.post("/me/tokens", {"name": "regression"}), "token")
    await _ok(await lead.post_json("/api/proposals", {"kind": "note", "summary": f"check {tag}"}), "proposal")
    return await world_ids()


# path parameter -> SQL that finds a real row for it
PARAM_SQL = {
    "item_id": "select id from archive_items",
    "event_id": "select id from events_events",
    "scene_id": "select id from overlay_scenes",
    "character_id": "select id from core_characters where is_bank_mule",
    "char_id": "select id from core_characters",
    "member_id": "select id from core_members",
    "recipe_id": "select id from crafters_recipes",
    "timer_id": "select id from watch_timers",
    "listing_id": "select id from board_listings",
    "token_id": "select id from core_api_tokens",
    "upload_id": "select id from core_uploads",
    "proposal_id": "select id from core_proposals",
    "drop_id": "select id from archive_drop_reports",
    "attendance_id": "select id from events_attendance",
    "award_id": "select id from events_loot",
    "holding_id": "select id from vault_holdings",
    "death_id": "select id from watch_deaths",
    "camp_id": "select id from watch_camps",
    "vault_request_id": "select id from vault_requests",
    "board_request_id": "select id from board_requests",
}


async def world_ids() -> dict[str, int]:
    ids: dict[str, int] = {}
    async with session_scope() as s:
        for key, sql in PARAM_SQL.items():
            try:
                v = (await s.execute(text(sql + " order by id limit 1"))).scalar_one_or_none()
            except Exception:  # noqa: BLE001 - table of a closed module
                v = None
            if v is not None:
                ids[key] = v
    return ids


def fill(path: str, ids: dict[str, int]) -> str | None:
    """Turn /vault/requests/{request_id} into a real URL, or None if we have no row for it."""

    def sub(m: re.Match) -> str:
        name = m.group(1)
        if name == "request_id":
            name = (
                "vault_request_id" if path.startswith("/vault") or path.startswith("/api/vault") else "board_request_id"
            )
        if name == "module_id":
            return "archive"
        if name == "action":
            return "close"
        if name not in ids:
            raise KeyError(name)
        return str(ids[name])

    try:
        return re.sub(r"{([^}:]+)(?::[^}]+)?}", sub, path)
    except KeyError:
        return None
