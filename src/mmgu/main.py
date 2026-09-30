"""Run everything in one process: the web app, the Discord bot and the job scheduler."""

from __future__ import annotations

import asyncio
import logging

import uvicorn

from mmgu.app import create_app
from mmgu.bot.client import run_bot
from mmgu.core.hall import hall
from mmgu.core.scheduler import run_scheduler

log = logging.getLogger("mmgu")


async def serve() -> None:
    app = create_app()
    s = hall.settings
    config = uvicorn.Config(
        app, host=s.host, port=s.port, proxy_headers=True, forwarded_allow_ips="*", log_level="info"
    )
    server = uvicorn.Server(config)
    tasks = [asyncio.create_task(server.serve(), name="web")]

    async def after_startup() -> None:
        while not server.started:
            await asyncio.sleep(0.2)
        if s.run_bot:
            tasks.append(asyncio.create_task(run_bot(hall), name="bot"))
        if s.run_scheduler:
            tasks.append(asyncio.create_task(run_scheduler(hall), name="scheduler"))

    await after_startup()
    await tasks[0]
    for t in tasks[1:]:
        t.cancel()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(serve())


if __name__ == "__main__":
    main()
