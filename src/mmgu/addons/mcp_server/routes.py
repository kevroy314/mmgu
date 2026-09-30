from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from mmgu.core.auth import Viewer, require
from mmgu.core.hall import hall
from mmgu.core.web import render

router = APIRouter()


@router.get("/addons/claude", response_class=HTMLResponse)
async def page(request: Request, viewer: Viewer = Depends(require("hall.view"))):
    return render(
        request,
        "mcp_server/index.html",
        hall_url=hall.settings.base_url.rstrip("/"),
        can_token=viewer.can("api.tokens"),
    )
