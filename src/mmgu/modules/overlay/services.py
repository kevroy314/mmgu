"""The Beacon: overlay scenes, what goes on stream, and the state an overlay page renders."""

from __future__ import annotations

import asyncio
import secrets
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mmgu.core.audit import record
from mmgu.core.auth import Viewer
from mmgu.core.bus import HallEvent
from mmgu.core.hall import hall
from mmgu.core.models import Member
from mmgu.db import utcnow
from mmgu.modules.overlay import adapters
from mmgu.modules.overlay.hub import hub
from mmgu.modules.overlay.models import Message, Scene

# (key, label, what it shows)
FEEDS: list[tuple[str, str, str]] = [
    ("item", "Item cards", "An inspect window when someone presses “Show on stream” on an item."),
    ("text", "Shout-outs", "Messages sent from this page or with /stream say in Discord."),
    ("discoveries", "New discoveries", "“New discovery: <item> by <member>” when an item is first cataloged."),
    ("timers", "Spawn timers", "The next spawn windows with live countdowns (needs the Watch)."),
    ("event", "Next event", "The next guild event, a countdown and how many are going (needs Events)."),
    ("board", "Notice Board posts", "A one-line note when a listing or request is posted."),
]
FEED_KEYS = [k for k, _, _ in FEEDS]
DEFAULT_FEEDS = ["item", "text", "discoveries", "timers", "event"]
POSITIONS: list[tuple[str, str]] = [
    ("bottom-right", "Bottom right"),
    ("bottom-left", "Bottom left"),
    ("top-right", "Top right"),
    ("top-left", "Top left"),
    ("bottom", "Bottom centre"),
    ("top", "Top centre"),
]
THEMES: list[tuple[str, str]] = [
    ("window", "Dark windows (readable on any game scene)"),
    ("clear", "Transparent (text with a shadow only)"),
]
CARD_KINDS = {"item", "text", "timer", "event"}
TEXT_TITLE_MAX = 80
TEXT_BODY_MAX = 280
KEEP_MESSAGES = timedelta(days=1)


class OverlayError(ValueError):
    pass


def new_token() -> str:
    return secrets.token_urlsafe(24)


def overlay_url(scene: Scene) -> str:
    return hall.settings.base_url.rstrip("/") + f"/overlay/{scene.token}"


def _int(value: Any, label: str, lo: int, hi: int) -> int:
    try:
        n = int(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        raise OverlayError(f"{label} must be a whole number between {lo} and {hi}.") from None
    if not lo <= n <= hi:
        raise OverlayError(f"{label} must be between {lo} and {hi}.")
    return n


def clean_scene(data: dict[str, Any]) -> dict[str, Any]:
    """Validate scene settings from a form or API. Raises OverlayError with a plain explanation."""
    name = " ".join(str(data.get("name") or "").split())[:80]
    if not name:
        raise OverlayError("Give the overlay a name, such as “Main scene”.")
    position = data.get("position") or "bottom-right"
    if position not in dict(POSITIONS):
        raise OverlayError("Pick where on screen the overlay sits.")
    theme = data.get("theme") or "window"
    if theme not in dict(THEMES):
        raise OverlayError("Pick a look: dark windows or transparent.")
    feeds = [f for f in FEED_KEYS if f in (data.get("feeds") or [])]
    if not feeds:
        raise OverlayError("Tick at least one thing to show on stream.")
    return {
        "name": name,
        "position": position,
        "theme": theme,
        "scale": _int(data.get("scale", 100), "Size", 50, 200),
        "card_seconds": _int(data.get("card_seconds", 12), "Seconds on screen", 3, 120),
        "timers_count": _int(data.get("timers_count", 3), "Number of spawn timers", 1, 10),
        "feeds": feeds,
        "active": bool(data.get("active", True)),
    }


def scene_form(form) -> dict[str, Any]:
    return {
        "name": form.get("name"),
        "position": form.get("position"),
        "theme": form.get("theme"),
        "scale": form.get("scale") or 100,
        "card_seconds": form.get("card_seconds") or 12,
        "timers_count": form.get("timers_count") or 3,
        "feeds": form.getlist("feeds"),
        "active": form.get("active") == "1",
    }


async def list_scenes(session: AsyncSession) -> list[Scene]:
    return list((await session.execute(select(Scene).order_by(Scene.created_at))).scalars().all())


async def scene_by_token(session: AsyncSession, token: str) -> Scene | None:
    if not token or len(token) > 64:
        return None
    return (await session.execute(select(Scene).where(Scene.token == token))).scalar_one_or_none()


async def _changed(scene: Scene, viewer: Viewer, action: str) -> None:
    await hall.bus.emit("overlay.scene_changed", actor_id=viewer.id, scene_id=scene.id, action=action)


async def create_scene(session: AsyncSession, viewer: Viewer, data: dict[str, Any], via: str = "web") -> Scene:
    fields = clean_scene(data)
    scene = Scene(**fields, token=new_token(), created_by=viewer.id)
    session.add(scene)
    await session.flush()
    await record(
        session, "overlay.scene_created", actor_id=viewer.id, entity=scene, via=via, summary=f"New overlay {scene.name}"
    )
    await _changed(scene, viewer, "created")
    return scene


async def update_scene(
    session: AsyncSession, viewer: Viewer, scene: Scene, data: dict[str, Any], via: str = "web"
) -> Scene:
    fields = clean_scene(data)
    before = {k: getattr(scene, k) for k in fields}
    for k, v in fields.items():
        setattr(scene, k, v)
    await session.flush()
    await record(
        session,
        "overlay.scene_updated",
        actor_id=viewer.id,
        entity=scene,
        before=before,
        via=via,
        summary=f"Changed overlay {scene.name}",
    )
    hub.configure(scene.id, scene.feeds, scene.active)
    await _changed(scene, viewer, "updated")
    return scene


async def set_active(session: AsyncSession, viewer: Viewer, scene: Scene, active: bool) -> Scene:
    scene.active = active
    await session.flush()
    await record(
        session,
        "overlay.scene_updated",
        actor_id=viewer.id,
        entity=scene,
        summary=f"Overlay {scene.name} {'on air' if active else 'paused'}",
    )
    hub.configure(scene.id, scene.feeds, scene.active)
    await _changed(scene, viewer, "on_air" if active else "paused")
    return scene


async def regenerate_token(session: AsyncSession, viewer: Viewer, scene: Scene) -> Scene:
    scene.token = new_token()
    await session.flush()
    await record(
        session,
        "overlay.token_regenerated",
        actor_id=viewer.id,
        entity_type="Scene",
        entity_id=scene.id,
        summary=f"New URL for overlay {scene.name}; the old one stopped working",
    )
    hub.close(scene.id)
    await _changed(scene, viewer, "token")
    return scene


async def delete_scene(session: AsyncSession, viewer: Viewer, scene: Scene) -> None:
    await record(
        session,
        "overlay.scene_deleted",
        actor_id=viewer.id,
        entity_type="Scene",
        entity_id=scene.id,
        before={"name": scene.name},
        summary=f"Deleted overlay {scene.name}",
    )
    await session.execute(delete(Message).where(Message.scene_id == scene.id))
    await session.delete(scene)
    await session.flush()
    hub.close(scene.id)
    await _changed(scene, viewer, "deleted")


# ----- what goes on stream ---------------------------------------------------------------
def clean_text(title: Any, body: Any) -> tuple[str, str]:
    title = " ".join(str(title or "").split())[:TEXT_TITLE_MAX]
    body = str(body or "").strip()[:TEXT_BODY_MAX]
    if not title and not body:
        raise OverlayError("Write a title or a message to show on stream.")
    return title, body


async def push_text(viewer: Viewer, title: Any, body: Any, scene_id: int | None = None) -> None:
    """Put a shout-out on every scene on air (or one scene). Rendered by the bus handler."""
    title, body = clean_text(title, body)
    await hall.bus.emit("overlay.push", actor_id=viewer.id, kind="text", title=title, body=body, scene_id=scene_id)


async def push_item(viewer: Viewer, item_id: int, scene_id: int | None = None) -> None:
    await hall.bus.emit("overlay.push", actor_id=viewer.id, kind="item", item_id=item_id, scene_id=scene_id)


async def push_test(session: AsyncSession, viewer: Viewer, scene: Scene) -> str:
    """Send a sample card to one scene: the newest Archive item if there is one, else a shout-out."""
    if hall.is_enabled("archive"):
        from mmgu.modules.archive.models import Item

        item = (await session.execute(select(Item).order_by(Item.created_at.desc()).limit(1))).scalar_one_or_none()
        if item is not None:
            await push_item(viewer, item.id, scene_id=scene.id)
            return f"Sent {item.name} to {scene.name}."
    await push_text(viewer, "Test card", "If you can read this on stream, the overlay works.", scene_id=scene.id)
    return f"Sent a test card to {scene.name}."


def _item_payload(item) -> dict[str, Any]:
    from mmgu.modules.archive.services import item_lines

    lines = item_lines(item)
    lines["stats"] = [[k, v] for k, v in lines["stats"]]
    lines["resists"] = [[k, v] for k, v in lines["resists"]]
    lines["effects"] = list(lines["effects"])
    return {"item": {"id": item.id, "name": item.name, "stale": item.status == "stale", "lines": lines}}


async def _wait_for_row(session: AsyncSession, model: Any, row_id: int, tries: int = 10, delay: float = 0.3):
    """Fetch a row that the emitting request may not have committed yet (events fire before commit)."""
    for attempt in range(tries):
        row = await session.get(model, row_id)
        if row is not None or attempt == tries - 1:
            return row
        await session.rollback()  # end the read snapshot so the next try sees new commits
        await asyncio.sleep(delay)
    return None


async def _payload_for(session: AsyncSession, e: HallEvent) -> tuple[str, str, dict[str, Any]] | None:
    """(feed, kind, payload) for a bus event, or None when there is nothing to show."""
    d = e.data
    if e.name == "overlay.push":
        kind = d.get("kind")
        if kind == "item":
            if not hall.is_enabled("archive") or d.get("item_id") is None:
                return None
            from mmgu.modules.archive.models import Item

            item = await session.get(Item, int(d["item_id"]))
            return ("item", "item", _item_payload(item)) if item else None
        if kind == "text":
            try:
                title, body = clean_text(d.get("title"), d.get("body"))
            except OverlayError:
                return None
            return "text", "text", {"title": title, "body": body}
        if kind == "timer":
            t = await adapters.timer(session, d.get("timer_id"))
            return ("timers", "timer", {"title": "Spawn timer", **t}) if t else None
        if kind == "event":
            ev = await adapters.event(session, d.get("event_id"))
            return ("event", "event", {"heading": "Guild event", **ev}) if ev else None
        return None
    if e.name == "archive.item_created":
        if not hall.is_enabled("archive"):
            return None
        from mmgu.modules.archive.models import Item

        item = await _wait_for_row(session, Item, int(d.get("item_id") or 0))
        if item is None:
            return None
        finder = await session.get(Member, item.first_cataloged_by) if item.first_cataloged_by else None
        return "discoveries", "discovery", {"item": item.name, "member": finder.display_name if finder else ""}
    if e.name == "watch.window_open":
        t = await adapters.timer(session, d.get("timer_id"))
        return ("timers", "timer", {"title": "Spawn window open", **t}) if t else None
    if e.name == "events.starting":
        ev = await adapters.event(session, d.get("event_id"))
        if ev is None:
            ev = {"title": "A guild event", "zone": "", "starts_at": None, "going": 0}
        return "event", "event", {"heading": "Starting now", **ev}
    if e.name == "board.posted":
        line = await adapters.board_line(session, d.get("kind") or "", d.get("id"))
        if not line:
            line = (
                "New request on the Notice Board"
                if d.get("kind") == "request"
                else "New trade listing on the Notice Board"
            )
        return "board", "board", {"text": line}
    return None


def message_json(m: Message) -> dict[str, Any]:
    return {
        **m.payload,
        "id": m.id,
        "feed": m.feed,
        "kind": m.kind,
        "at": adapters.iso(m.created_at),
        "card": m.kind in CARD_KINDS,
    }


async def message_for(session: AsyncSession, e: HallEvent) -> tuple[dict[str, Any], int | None] | None:
    """Turn a bus event into a stored overlay message. Returns (message json, target scene id)."""
    found = await _payload_for(session, e)
    if found is None:
        return None
    feed, kind, payload = found
    scene_id = e.data.get("scene_id") if e.name == "overlay.push" else None
    if scene_id is not None and await session.get(Scene, int(scene_id)) is None:
        return None
    msg = Message(feed=feed, kind=kind, payload=payload, scene_id=scene_id, actor_id=e.actor_id)
    session.add(msg)
    await session.flush()
    if e.name == "overlay.push":
        label = payload.get("item", {}).get("name") or payload.get("title") or payload.get("body") or kind
        await record(
            session,
            "overlay.pushed",
            actor_id=e.actor_id,
            entity_type="overlay_message",
            entity_id=msg.id,
            after={"kind": kind, **({"scene_id": scene_id} if scene_id else {})},
            via="system",
            summary=f"Put “{str(label)[:80]}” on stream",
        )
    return message_json(msg), scene_id


async def recent_messages(session: AsyncSession, limit: int = 10) -> list[tuple[Message, Member | None]]:
    rows = await session.execute(
        select(Message, Member)
        .join(Member, Member.id == Message.actor_id, isouter=True)
        .order_by(Message.id.desc())
        .limit(limit)
    )
    return [(m, a) for m, a in rows.all()]


async def prune(session: AsyncSession) -> None:
    await session.execute(delete(Message).where(Message.created_at < utcnow() - KEEP_MESSAGES))


# ----- what an overlay page shows --------------------------------------------------------
def scene_json(scene: Scene) -> dict[str, Any]:
    return {
        "name": scene.name,
        "position": scene.position,
        "theme": scene.theme,
        "scale": scene.scale,
        "card_seconds": scene.card_seconds,
        "feeds": list(scene.feeds or []),
        "active": scene.active,
    }


async def scene_state(session: AsyncSession, scene: Scene) -> dict[str, Any]:
    """Everything the overlay page needs to draw itself. Only includes the feeds this scene shows."""
    feeds = set(scene.feeds or [])
    messages: list[dict[str, Any]] = []
    since = utcnow() - timedelta(seconds=max(scene.card_seconds, 15))
    shared = Message.scene_id.is_(None) & Message.feed.in_(feeds) if scene.active else None
    cond = Message.scene_id == scene.id if shared is None else or_(shared, Message.scene_id == scene.id)
    rows = (
        (await session.execute(select(Message).where(cond, Message.created_at >= since).order_by(Message.id).limit(20)))
        .scalars()
        .all()
    )
    messages = [message_json(m) for m in rows]
    timers = await adapters.upcoming_windows(session, scene.timers_count) if "timers" in feeds else None
    events = await adapters.next_events(session, 1) if "event" in feeds else None
    return {
        "scene": scene_json(scene),
        "server_time": adapters.iso(utcnow()),
        "messages": messages,
        "timers": timers,
        "events": events,
    }
