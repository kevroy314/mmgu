"""Every route the app serves, from its OpenAPI schema plus the few routes kept out of it."""

from __future__ import annotations

from fastapi import FastAPI

# Routes deliberately excluded from the schema (token-authorised overlay, calendar feed, health).
EXTRA_GET = ["/healthz"]


def routes(app: FastAPI, method: str) -> list[str]:
    paths = app.openapi()["paths"]
    out = sorted(p for p, ops in paths.items() if method.lower() in ops)
    if method.upper() == "GET":
        out += [p for p in EXTRA_GET if p not in out]
    return out


def is_api(path: str) -> bool:
    return path.startswith("/api/") or path.endswith(".ics") or path == "/healthz"
