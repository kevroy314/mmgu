"""How the roster plugs into the rest of the hall."""

from __future__ import annotations

from sqlalchemy import func, or_, select

from mmgu.core.extensions import SearchHit
from mmgu.core.models import Character, Member
from mmgu.core.web import render_string


def register(hall) -> None:
    async def my_characters(request, session, member):
        from mmgu.modules.roster.services import characters_of

        chars = await characters_of(session, member.id)
        return render_string("roster/_panel.html", request, chars=chars, member=member)

    async def search(session, q, viewer):
        if not viewer.can("members.view"):
            return []
        like = f"%{q}%"
        hits = []
        rows = (
            await session.execute(
                select(Character, Member)
                .join(Member, Member.id == Character.member_id, isouter=True)
                .where(Character.status == "active", Character.name.ilike(like))
                .limit(10)
            )
        ).all()
        for c, m in rows:
            sub = " ".join(str(x) for x in (c.level, c.class_name) if x)
            url = f"/roster/members/{m.id}" if m else "/roster"
            hits.append(
                SearchHit(
                    "Character",
                    c.name,
                    url,
                    (sub + (f" · {m.display_name}" if m else "")).strip(" ·"),
                    score=2 if c.name.lower().startswith(q.lower()) else 1,
                )
            )
        mems = (
            (
                await session.execute(
                    select(Member).where(Member.display_name.ilike(like), Member.status == "active").limit(5)
                )
            )
            .scalars()
            .all()
        )
        for m in mems:
            hits.append(SearchHit("Member", m.display_name, f"/roster/members/{m.id}", score=1.5))
        return hits

    async def card(request, session):
        count = (
            await session.execute(select(func.count()).select_from(Member).where(Member.status == "active"))
        ).scalar_one()
        mains = (
            await session.execute(
                select(Character.class_name, func.count())
                .where(
                    Character.status == "active", or_(Character.is_main.is_(True)), Character.is_bank_mule.is_(False)
                )
                .group_by(Character.class_name)
            )
        ).all()
        mine = (
            await session.execute(
                select(func.count())
                .select_from(Character)
                .where(Character.member_id == request.state.viewer.id, Character.status == "active")
            )
        ).scalar_one()
        return render_string(
            "roster/_card.html", request, count=count, mains=sorted(mains, key=lambda r: -r[1]), mine=mine
        )

    hall.ext.add("member.panels", my_characters, module="roster", order=10)
    hall.ext.add("search.providers", search, module="roster")
    hall.ext.add("hall.cards", card, module="roster", order=90)
