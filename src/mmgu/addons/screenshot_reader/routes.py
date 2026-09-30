from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from mmgu.addons.screenshot_reader import reader
from mmgu.core.auth import Viewer, require
from mmgu.core.uploads import MAX_BYTES
from mmgu.core.web import render, render_string

router = APIRouter()
PAGE = "/addons/screenshot-reader"


def _context() -> dict:
    return {
        "provider": reader.provider(),
        "ollama_url": reader.ollama_url(),
        "ollama_model": reader.ollama_model(),
        "anthropic_model": reader.anthropic_model(),
        "has_key": bool(reader.anthropic_key()),
    }


@router.get(PAGE, response_class=HTMLResponse)
async def page(request: Request, viewer: Viewer = Depends(require("admin.settings"))):
    return render(request, "screenshot_reader/index.html", result=None, **_context())


@router.post(PAGE + "/test", response_class=HTMLResponse)
async def test_reader(request: Request, viewer: Viewer = Depends(require("admin.settings"))):
    """Run the reader on an image and show what the model said. Nothing is saved."""
    form = await request.form()
    shot = form.get("screenshot")
    if shot is None or not getattr(shot, "filename", ""):
        raise HTTPException(422, "Choose a screenshot to test with.")
    data = await shot.read()
    if len(data) > MAX_BYTES:
        raise HTTPException(422, "That image is over 12 MB. Crop it first.")
    kind = "bank" if form.get("kind") == "bank" else "item"
    result: dict = {"kind": kind}
    try:
        raw = await reader.read_raw(data, kind)
        fields = reader.item_fields(raw.data) if kind == "item" else None
        result.update(
            ok=True,
            source=raw.source,
            seconds=round(raw.seconds, 1),
            json=json.dumps(raw.data, indent=2, ensure_ascii=False),
            fields=json.dumps(fields, indent=2, ensure_ascii=False) if fields is not None else None,
            notes=reader.item_notes(fields) if fields is not None else [],
        )
    except reader.ReaderError as e:
        result.update(ok=False, error=str(e))
    return render(request, "screenshot_reader/index.html", result=result, **_context())


@router.post(PAGE + "/check", response_class=HTMLResponse)
async def check(request: Request, viewer: Viewer = Depends(require("admin.settings"))):
    """HTMX: list the models Ollama has, so a leader can see the connection works."""
    models, error = [], None
    try:
        models = await reader.list_ollama_models()
    except reader.ReaderError as e:
        error = str(e)
    html = render_string(
        "screenshot_reader/_check.html",
        request,
        models=models,
        error=error,
        ollama_url=reader.ollama_url(),
        ollama_model=reader.ollama_model(),
    )
    return HTMLResponse(html)
