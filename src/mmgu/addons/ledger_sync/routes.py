from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from mmgu.addons.ledger_sync import services
from mmgu.core.auth import Viewer, require
from mmgu.core.hall import hall
from mmgu.core.web import render
from mmgu.db import get_session

router = APIRouter()


@router.post("/api/addons/ledger/events")
async def api_events(
    request: Request, viewer: Viewer = Depends(require("archive.catalog")), session: AsyncSession = Depends(get_session)
):
    """Receive a batch of events from the companion script. Re-sending the same events is harmless."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise HTTPException(422, "The body must be JSON.") from e
    try:
        result = await services.import_batch(session, viewer, body)
    except services.LedgerError as e:
        raise HTTPException(422, str(e)) from e
    await session.commit()
    return result


@router.get("/addons/ledger", response_class=HTMLResponse)
async def page(
    request: Request, viewer: Viewer = Depends(require("archive.catalog")), session: AsyncSession = Depends(get_session)
):
    return render(
        request,
        "ledger_sync/index.html",
        imports=await services.recent_imports(session, 40),
        totals=await services.member_totals(session),
        hall_url=hall.settings.base_url.rstrip("/"),
        watch_on=hall.is_enabled("watch"),
    )
