"""The Beacon's pages.

``/beacon/...``  normal hall pages for streamers and leaders (login required).
``/overlay/<token>...``  what OBS loads. No login: the token in the URL is the only key, CSRF is
skipped for this prefix and a trusted proxy may expose it without its sign-in gateway, so these
routes check the token first and only return what that scene is set to show.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from mmgu.core.auth import Viewer, require
from mmgu.core.hall import hall
from mmgu.core.web import redirect, render, render_string
from mmgu.db import get_session, session_scope
from mmgu.modules.overlay import services
from mmgu.modules.overlay.hub import CLOSE, hub
from mmgu.modules.overlay.models import Scene

log = logging.getLogger(__name__)
router = APIRouter()
KEEPALIVE_SECONDS = 15
PUBLIC_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
}


def _form_context(**kw):
    return {
        "feeds": services.FEEDS,
        "positions": services.POSITIONS,
        "themes": services.THEMES,
        "watch_on": hall.is_enabled("watch"),
        "events_on": hall.is_enabled("events"),
        "board_on": hall.is_enabled("board"),
        **kw,
    }


async def _scene(session: AsyncSession, scene_id: int) -> Scene:
    scene = await session.get(Scene, scene_id)
    if scene is None:
        raise HTTPException(404, "That overlay was deleted.")
    return scene


# ----- management (/beacon) ----------------------------------------------------------------
@router.get("/beacon", response_class=HTMLResponse)
async def index(
    request: Request, viewer: Viewer = Depends(require("overlay.push")), session: AsyncSession = Depends(get_session)
):
    manage = viewer.can("overlay.manage")
    scenes = await services.list_scenes(session) if manage else []
    return render(
        request,
        "overlay/index.html",
        scenes=scenes,
        manage=manage,
        url=services.overlay_url,
        live={s.id: hub.count(s.id) for s in scenes},
        recent=await services.recent_messages(session),
        archive_on=hall.is_enabled("archive"),
        feed_labels={k: label for k, label, _ in services.FEEDS},
        text_max=(services.TEXT_TITLE_MAX, services.TEXT_BODY_MAX),
    )


@router.get("/beacon/new", response_class=HTMLResponse)
async def new_form(request: Request, viewer: Viewer = Depends(require("overlay.manage"))):
    data = {
        "name": "Main scene",
        "position": "bottom-right",
        "theme": "window",
        "scale": 100,
        "card_seconds": 12,
        "timers_count": 3,
        "feeds": services.DEFAULT_FEEDS,
        "active": True,
    }
    return render(request, "overlay/form.html", **_form_context(data=data, scene=None))


@router.post("/beacon/new")
async def create(
    request: Request, viewer: Viewer = Depends(require("overlay.manage")), session: AsyncSession = Depends(get_session)
):
    form = await request.form()
    try:
        scene = await services.create_scene(session, viewer, services.scene_form(form))
    except services.OverlayError as e:
        raise HTTPException(422, str(e)) from e
    await session.commit()
    return redirect(request, f"/beacon#scene-{scene.id}", f"{scene.name} is ready. Copy its URL into OBS.")


@router.get("/beacon/scenes/{scene_id}", response_class=HTMLResponse)
async def edit_form(
    request: Request,
    scene_id: int,
    viewer: Viewer = Depends(require("overlay.manage")),
    session: AsyncSession = Depends(get_session),
):
    scene = await _scene(session, scene_id)
    data = services.scene_json(scene) | {"timers_count": scene.timers_count}
    return render(request, "overlay/form.html", **_form_context(data=data, scene=scene))


@router.post("/beacon/scenes/{scene_id}")
async def edit(
    request: Request,
    scene_id: int,
    viewer: Viewer = Depends(require("overlay.manage")),
    session: AsyncSession = Depends(get_session),
):
    scene = await _scene(session, scene_id)
    form = await request.form()
    try:
        await services.update_scene(session, viewer, scene, services.scene_form(form))
    except services.OverlayError as e:
        raise HTTPException(422, str(e)) from e
    await session.commit()
    return redirect(request, f"/beacon#scene-{scene.id}", "Saved. Open overlays update within a few seconds.")


@router.post("/beacon/scenes/{scene_id}/air")
async def toggle_air(
    request: Request,
    scene_id: int,
    viewer: Viewer = Depends(require("overlay.manage")),
    session: AsyncSession = Depends(get_session),
):
    scene = await _scene(session, scene_id)
    await services.set_active(session, viewer, scene, not scene.active)
    await session.commit()
    msg = f"{scene.name} is on air." if scene.active else f"{scene.name} is paused: new cards won't show on it."
    return redirect(request, f"/beacon#scene-{scene.id}", msg)


@router.post("/beacon/scenes/{scene_id}/token")
async def regenerate(
    request: Request,
    scene_id: int,
    viewer: Viewer = Depends(require("overlay.manage")),
    session: AsyncSession = Depends(get_session),
):
    scene = await _scene(session, scene_id)
    await services.regenerate_token(session, viewer, scene)
    await session.commit()
    return redirect(
        request, f"/beacon#scene-{scene.id}", "New URL made. The old one has stopped working; update it in OBS.", "warn"
    )


@router.post("/beacon/scenes/{scene_id}/delete")
async def delete(
    request: Request,
    scene_id: int,
    viewer: Viewer = Depends(require("overlay.manage")),
    session: AsyncSession = Depends(get_session),
):
    scene = await _scene(session, scene_id)
    name = scene.name
    await services.delete_scene(session, viewer, scene)
    await session.commit()
    return redirect(request, "/beacon", f"Deleted {name}. Its URL no longer works.")


@router.post("/beacon/scenes/{scene_id}/test")
async def test_card(
    request: Request,
    scene_id: int,
    viewer: Viewer = Depends(require("overlay.manage")),
    session: AsyncSession = Depends(get_session),
):
    scene = await _scene(session, scene_id)
    msg = await services.push_test(session, viewer, scene)
    return redirect(request, f"/beacon#scene-{scene.id}", msg)


@router.post("/beacon/push")
async def push(
    request: Request, viewer: Viewer = Depends(require("overlay.push")), session: AsyncSession = Depends(get_session)
):
    """A shout-out (title + message) or an Archive item, to every overlay on air."""
    form = await request.form()
    item_name = (form.get("name") or "").strip()
    if item_name:
        if not hall.is_enabled("archive"):
            raise HTTPException(422, "The Archive is closed, so there are no items to show.")
        from mmgu.modules.archive.services import find_by_name

        item = await find_by_name(session, item_name)
        if item is None:
            raise HTTPException(422, f"{item_name} isn't in the Archive. Check the spelling or pick from the list.")
        await services.push_item(viewer, item.id)
        return redirect(request, "/beacon", f"{item.name} is on stream.")
    try:
        await services.push_text(viewer, form.get("title"), form.get("body"))
    except services.OverlayError as e:
        raise HTTPException(422, str(e)) from e
    return redirect(request, "/beacon", "Sent to stream.")


# ----- public overlay (/overlay/<token>) -----------------------------------------------------
def _gone() -> Response:
    return PlainTextResponse("No overlay here. The link may have been replaced.", 404, headers=PUBLIC_HEADERS)


@router.get("/overlay/{token}", response_class=HTMLResponse, include_in_schema=False)
async def overlay_page(token: str, session: AsyncSession = Depends(get_session)):
    scene = await services.scene_by_token(session, token)
    if scene is None:
        return _gone()
    html = render_string(
        "overlay/stage.html",
        scene=scene,
        cfg=services.scene_json(scene),
        guild=hall.guild_name,
    )
    return HTMLResponse(html, headers=PUBLIC_HEADERS)


@router.get("/overlay/{token}/state", include_in_schema=False)
async def overlay_state(token: str, session: AsyncSession = Depends(get_session)):
    scene = await services.scene_by_token(session, token)
    if scene is None:
        return _gone()
    return JSONResponse(await services.scene_state(session, scene), headers=PUBLIC_HEADERS)


@router.get("/overlay/{token}/stream", include_in_schema=False)
async def overlay_stream(token: str, request: Request):
    # Its own short session: an open stream must not hold a database connection.
    async with session_scope() as session:
        scene = await services.scene_by_token(session, token)
        if scene is None:
            return _gone()
        sub = hub.subscribe(scene.id, scene.feeds or [], scene.active)

    async def events():
        try:
            yield {"event": "hello", "data": "{}"}
            while True:
                try:
                    msg = await asyncio.wait_for(sub.queue.get(), timeout=KEEPALIVE_SECONDS * 2)
                except TimeoutError:
                    if await request.is_disconnected():
                        return
                    continue
                if msg is CLOSE:
                    yield {"event": "gone", "data": "{}"}
                    return
                yield {"event": "msg", "data": json.dumps(msg)}
        finally:
            hub.unsubscribe(sub)

    return EventSourceResponse(
        events(),
        ping=KEEPALIVE_SECONDS,
        headers={**PUBLIC_HEADERS, "X-Accel-Buffering": "no"},
    )
