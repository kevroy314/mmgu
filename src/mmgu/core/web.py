"""Rendering helpers shared by every module: templates, toasts, redirects, time formatting."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

from mmgu.core.auth import Viewer, csrf_token
from mmgu.core.hall import hall
from mmgu.core.icons import icon

CORE_TEMPLATES = Path(__file__).resolve().parents[1] / "templates"

_env: Environment | None = None


def _fmt_dt(value: datetime | None) -> Markup:
    """Render a UTC datetime as a <time> element; static/js/app.js shows it in the viewer's timezone."""
    if value is None:
        return Markup("")
    iso = value.isoformat(timespec="seconds") + "Z"
    return Markup(f'<time datetime="{iso}" data-local>{value.strftime("%Y-%m-%d %H:%M")} UTC</time>')


def _reltime(value: datetime | None) -> Markup:
    if value is None:
        return Markup("")
    iso = value.isoformat(timespec="seconds") + "Z"
    return Markup(f'<time datetime="{iso}" data-rel title="{iso}">{value.strftime("%b %d")}</time>')


def _countdown(value: datetime | None) -> Markup:
    if value is None:
        return Markup("")
    iso = value.isoformat(timespec="seconds") + "Z"
    return Markup(f'<time datetime="{iso}" data-countdown>{value.strftime("%H:%M")} UTC</time>')


def _nl2br(value: str | None) -> Markup:
    if not value:
        return Markup("")
    return Markup("<br>".join(escape(value).split("\n")))


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def env() -> Environment:
    global _env
    if _env is None:
        dirs = [CORE_TEMPLATES]
        for m in hall.modules.values():
            if m.templates_dir:
                dirs.append(m.templates_dir)
        _env = Environment(
            loader=ChoiceLoader([FileSystemLoader(str(d)) for d in dirs]),
            autoescape=select_autoescape(["html"]),
            enable_async=False,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        _env.filters.update(
            dt=_fmt_dt, reltime=_reltime, countdown=_countdown, nl2br=_nl2br, tojson_attr=lambda v: json.dumps(v)
        )
        _env.globals.update(icon=icon, hall=hall, plural=_plural, timedelta=timedelta)
    return _env


def reset_env() -> None:
    global _env
    _env = None


def nav_for(viewer: Viewer) -> list[dict[str, Any]]:
    items = []
    for m in hall.enabled_modules():
        for n in m.nav:
            if viewer.can(n.permission):
                items.append(
                    {
                        "label": n.label,
                        "plain": n.plain,
                        "href": n.href,
                        "icon": n.icon,
                        "order": n.order,
                        "module": m.id,
                    }
                )
    return sorted(items, key=lambda i: i["order"])


def render(request: Request, template: str, status_code: int = 200, **context: Any) -> HTMLResponse:
    viewer: Viewer = getattr(request.state, "viewer", None) or Viewer()
    is_htmx = request.headers.get("hx-request") == "true" and request.headers.get("hx-boosted") != "true"
    ctx = {
        "request": request,
        "viewer": viewer,
        "game": hall.game,
        "nav": nav_for(viewer),
        "csrf": csrf_token(request) if "session" in request.scope else "",
        "is_htmx": is_htmx,
        "current_path": request.url.path,
        "enabled": hall.enabled_ids,
    }
    ctx.update(context)
    html = env().get_template(template).render(**ctx)
    return HTMLResponse(html, status_code=status_code)


def render_string(template: str, request: Request | None = None, **context: Any) -> str:
    """Render a fragment: dashboard cards and page panels (pass ``request``), or outside a request."""
    context.setdefault("game", hall.game)
    context.setdefault("enabled", hall.enabled_ids)
    if request is not None:
        context.setdefault("request", request)
        context.setdefault("viewer", getattr(request.state, "viewer", None) or Viewer())
        context.setdefault("csrf", csrf_token(request) if "session" in request.scope else "")
    return env().get_template(template).render(**context)


def toast(response: Response, message: str, kind: str = "ok") -> Response:
    """Show a toast after an HTMX request (or on the next page for a normal POST)."""
    response.headers["HX-Trigger"] = json.dumps({"toast": {"message": message, "kind": kind}})
    return response


def redirect(request: Request, url: str, message: str | None = None, kind: str = "ok") -> Response:
    if request.headers.get("hx-request") == "true":
        resp: Response = Response(status_code=204)
        target = urlsplit(url)
        current = urlsplit(request.headers.get("hx-current-url", ""))
        if (target.path, target.query) == (current.path, current.query):
            # Same page (maybe a different #anchor): the browser would only scroll, so reload it.
            resp.headers["HX-Refresh"] = "true"
        else:
            resp.headers["HX-Redirect"] = url
    else:
        resp = RedirectResponse(url, status_code=303)
    if message:
        request.session["flash"] = {"message": message, "kind": kind}
    return resp
