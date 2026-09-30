"""Tell a member something: an in-app notice, plus a Discord DM when their account is linked."""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from mmgu.core.hall import hall
from mmgu.core.models import Notice


async def notify(
    session: AsyncSession, member_id: int | None, text: str, url: str | None = None, dm: bool = True
) -> None:
    if not member_id:
        return
    session.add(Notice(member_id=member_id, text=text[:400], url=url))
    if dm and hall.discord is not None and hall.discord.available:
        full = text + (f"\n{hall.settings.base_url.rstrip('/')}{url}" if url else "")
        asyncio.get_running_loop().create_task(hall.discord.dm(member_id, full))
