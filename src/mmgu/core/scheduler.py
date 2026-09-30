"""Runs module jobs (expire listings, spawn-window alerts, event reminders) on fixed intervals."""

from __future__ import annotations

import asyncio
import logging
import time

from mmgu.core.hall import Hall

log = logging.getLogger(__name__)


async def run_scheduler(hall: Hall, tick_seconds: float = 5.0) -> None:
    last_run: dict[str, float] = {}
    while True:
        now = time.monotonic()
        for module_id, job in hall.jobs:
            key = f"{module_id}.{job.name}"
            if not hall.is_enabled(module_id):
                continue
            if now - last_run.get(key, 0) < job.every_seconds:
                continue
            last_run[key] = now
            try:
                await job.fn()
            except Exception:  # noqa: BLE001
                log.exception("job %s failed", key)
        await asyncio.sleep(tick_seconds)
