"""Turning a screenshot into a suggested item.

The Archive doesn't read images itself. Add-ons register a reader in the ``archive.readers``
extension slot::

    async def reader(image: bytes) -> ReaderResult | None: ...
    hall.ext.add("archive.readers", reader, module="screenshot_reader")

Whatever a reader returns becomes a Proposal that a person reviews on the catalog form.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from mmgu.core.auth import Viewer
from mmgu.core.hall import hall
from mmgu.core.models import Proposal, Upload
from mmgu.core.uploads import upload_bytes

log = logging.getLogger(__name__)


@dataclass
class ReaderResult:
    fields: dict[str, Any]
    source: str
    raw_text: str = ""
    confidence: float | None = None
    notes: list[str] = field(default_factory=list)


def reader_available() -> bool:
    return bool(hall.ext.get("archive.readers", hall.enabled_ids))


async def read_screenshot(
    session: AsyncSession, viewer: Viewer, upload: Upload, *, creature: str | None = None, zone: str | None = None
) -> Proposal:
    result: ReaderResult | None = None
    error = None
    for c in hall.ext.get("archive.readers", hall.enabled_ids):
        try:
            result = await c.fn(upload_bytes(upload))
        except Exception as e:  # noqa: BLE001
            log.exception("screenshot reader %s failed", c.module)
            error = f"{c.module}: {e}"
        if result is not None:
            break
    fields = dict(result.fields) if result else {}
    if result and result.raw_text:
        fields.setdefault("raw_text", result.raw_text)
    name = (fields.get("name") or "").strip()
    p = Proposal(
        kind="archive.item",
        source=result.source if result else "manual",
        summary=f"Item from screenshot: {name or 'unnamed'}",
        upload_id=upload.id,
        created_by=viewer.id,
        payload={
            "item": fields,
            "creature": creature,
            "zone": zone,
            "confidence": result.confidence if result else None,
            "notes": result.notes if result else [],
        },
        error=error,
    )
    session.add(p)
    await session.flush()
    p.payload = {**p.payload, "review_url": f"/archive/new?proposal={p.id}"}
    return p
