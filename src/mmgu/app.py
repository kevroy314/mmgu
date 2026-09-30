"""Builds the FastAPI app from the enabled modules."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from mmgu.config import Settings
from mmgu.core import store
from mmgu.core.auth import LoginRequired, csrf_protect, current_viewer, require_module, resolve_viewer
from mmgu.core.hall import hall
from mmgu.db import init_engine, session_scope

log = logging.getLogger(__name__)
STATIC = Path(__file__).resolve().parent / "static"


def _wants_html(request: Request) -> bool:
    return not request.url.path.startswith("/api/") and "text/html" in request.headers.get("accept", "text/html")


def create_app(settings: Settings | None = None) -> FastAPI:
    if not hall.booted:
        hall.boot(settings)
    s = hall.settings
    init_engine(s.db_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with session_scope() as session:
            await store.load_all(session)
        await hall.bus.emit_and_wait("hall.startup")
        yield
        await hall.bus.drain()

    app = FastAPI(
        title=hall.app_name,
        lifespan=lifespan,
        root_path=s.root_path,
        dependencies=[Depends(csrf_protect), Depends(current_viewer)],
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=s.resolved_secret(),
        session_cookie="mmgu_session",
        max_age=60 * 60 * 24 * 30,
        same_site="lax",
        https_only=s.base_url.startswith("https"),
    )
    for m in hall.modules.values():
        if m.static_dir:
            app.mount(f"/static/m/{m.id}", StaticFiles(directory=m.static_dir), name=f"static-{m.id}")
        if m.router is not None:
            deps = [] if m.kind == "core" else [Depends(require_module(m.id))]
            try:
                app.include_router(m.router(), dependencies=deps)
            except Exception as e:  # noqa: BLE001
                from mmgu.core.modules import BROKEN

                log.exception("routes failed for module %s", m.id)
                BROKEN[m.id] = repr(e)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")  # after /static/m/<module> mounts

    @app.exception_handler(LoginRequired)
    async def _login(request: Request, _exc: LoginRequired):
        if not _wants_html(request):
            return JSONResponse({"detail": "Log in first."}, status_code=401)
        nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        if request.headers.get("hx-request") == "true":
            return JSONResponse({}, status_code=401, headers={"HX-Redirect": f"/login?next={nxt}"})
        return RedirectResponse(f"/login?next={nxt}", status_code=303)

    @app.exception_handler(HTTPException)
    async def _http(request: Request, exc: HTTPException):
        if not _wants_html(request) or exc.status_code < 400:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
        from mmgu.core.web import render

        if request.headers.get("hx-request") == "true":
            import json

            return JSONResponse(
                {"detail": exc.detail},
                status_code=exc.status_code,
                headers={
                    "HX-Trigger": json.dumps({"toast": {"message": str(exc.detail), "kind": "err"}}),
                    "HX-Reswap": "none",
                },
            )
        # The request's session was rolled back, detaching the viewer; load a fresh one for the error page.
        request.state.viewer = None
        async with session_scope() as fresh:
            await resolve_viewer(request, fresh)
            return render(
                request, "core/error.html", status_code=exc.status_code, code=exc.status_code, detail=exc.detail
            )

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError):
        fields = ", ".join(str(e["loc"][-1]) for e in exc.errors())
        return await _http(request, HTTPException(422, f"Some fields are missing or invalid: {fields}."))

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"ok": True, "modules": sorted(hall.enabled_ids)}

    return app
