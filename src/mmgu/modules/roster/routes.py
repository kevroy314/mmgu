from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmgu.core.audit import record
from mmgu.core.auth import Viewer, require
from mmgu.core.hall import hall
from mmgu.core.models import Character, Member
from mmgu.core.web import redirect, render
from mmgu.core.whoparse import parse_who
from mmgu.db import get_session
from mmgu.modules.roster import services
from mmgu.modules.roster.models import OfficerNote

router = APIRouter(prefix="/roster")
api = APIRouter()


def _int(v: str | None) -> int | None:
    v = (v or "").strip()
    return int(v) if v.isdigit() else None


@router.get("", response_class=HTMLResponse)
async def roster(
    request: Request,
    q: str = "",
    cls: str = "",
    role: str = "",
    min_level: str = "",
    max_level: str = "",
    mules: str = "",
    viewer: Viewer = Depends(require("members.view")),
    session: AsyncSession = Depends(get_session),
):
    rows = await services.list_characters(
        session,
        q=q,
        class_name=cls,
        role=role,
        min_level=_int(min_level),
        max_level=_int(max_level),
        mules=None if mules == "all" else (mules == "only") if mules else False,
    )
    everyone = await services.list_characters(session, mules=False, limit=5000)
    makeup = Counter(c.class_name or "Unknown" for c, _ in everyone if c.is_main or c.member_id is None)
    members = (
        (await session.execute(select(Member).where(Member.status == "active").order_by(Member.display_name)))
        .scalars()
        .all()
    )
    return render(
        request,
        "roster/index.html",
        rows=rows,
        makeup=makeup.most_common(),
        members=members,
        f={"q": q, "cls": cls, "role": role, "min_level": min_level, "max_level": max_level, "mules": mules},
    )


@router.get("/me")
async def my_roster(viewer: Viewer = Depends(require("hall.view"))):
    return RedirectResponse(f"/roster/members/{viewer.id}", status_code=303)


@router.get("/members/{member_id}", response_class=HTMLResponse)
async def member_page(
    request: Request,
    member_id: int,
    viewer: Viewer = Depends(require("members.view")),
    session: AsyncSession = Depends(get_session),
):
    member = await session.get(Member, member_id)
    if member is None:
        raise HTTPException(404, "No such member.")
    chars = await services.characters_of(session, member_id)
    notes = []
    if viewer.can("members.notes"):
        notes = (
            await session.execute(
                select(OfficerNote, Member)
                .join(Member, Member.id == OfficerNote.author_id, isouter=True)
                .where(OfficerNote.member_id == member_id)
                .order_by(OfficerNote.created_at.desc())
            )
        ).all()
    panels = []
    for c in hall.ext.get("member.panels", hall.enabled_ids):
        if c.module == "roster":
            continue
        html = await c.fn(request, session, member)
        if html:
            panels.append(html)
    from mmgu.core.auth import build_viewer

    their = await build_viewer(session, member, "session")
    return render(
        request,
        "roster/member.html",
        member=member,
        chars=chars,
        notes=notes,
        panels=panels,
        their_rank=their.rank,
        their_duties=sorted(their.duties),
        can_edit=viewer.id == member_id or viewer.can("characters.edit_any"),
        default_server=services.default_server(),
    )


@router.post("/characters")
async def add_character(
    request: Request,
    name: str = Form(...),
    class_name: str = Form(""),
    level: str = Form(""),
    race: str = Form(""),
    server: str = Form(""),
    is_main: str = Form(""),
    is_bank_mule: str = Form(""),
    notes: str = Form(""),
    member_id: str = Form(""),
    viewer: Viewer = Depends(require("hall.view")),
    session: AsyncSession = Depends(get_session),
):
    owner = _int(member_id) or viewer.id
    try:
        ch = await services.save_character(
            session,
            viewer,
            name=name,
            class_name=class_name or None,
            level=_int(level),
            race=race or None,
            server=server,
            is_main=bool(is_main),
            is_bank_mule=bool(is_bank_mule),
            notes=notes,
            member_id=owner,
        )
    except services.RosterError as e:
        raise HTTPException(422, str(e)) from e
    await session.commit()
    return redirect(request, f"/roster/members/{owner}", f"{ch.name} added to the Muster Roll.")


@router.post("/characters/{char_id}")
async def edit_character(
    request: Request,
    char_id: int,
    name: str = Form(...),
    class_name: str = Form(""),
    level: str = Form(""),
    race: str = Form(""),
    server: str = Form(""),
    is_main: str = Form(""),
    is_bank_mule: str = Form(""),
    notes: str = Form(""),
    viewer: Viewer = Depends(require("hall.view")),
    session: AsyncSession = Depends(get_session),
):
    ch = await session.get(Character, char_id)
    if ch is None:
        raise HTTPException(404)
    if not services.can_edit(viewer, ch):
        raise HTTPException(403, "That isn't your character.")
    try:
        await services.save_character(
            session,
            viewer,
            name=name,
            class_name=class_name or None,
            level=_int(level),
            race=race or None,
            server=server,
            is_main=bool(is_main),
            is_bank_mule=bool(is_bank_mule),
            notes=notes,
            character=ch,
        )
    except services.RosterError as e:
        raise HTTPException(422, str(e)) from e
    await session.commit()
    return redirect(request, f"/roster/members/{ch.member_id or viewer.id}", f"Saved {ch.name}.")


@router.post("/characters/{char_id}/retire")
async def retire_character(
    request: Request,
    char_id: int,
    viewer: Viewer = Depends(require("hall.view")),
    session: AsyncSession = Depends(get_session),
):
    ch = await session.get(Character, char_id)
    if ch is None:
        raise HTTPException(404)
    if not services.can_edit(viewer, ch):
        raise HTTPException(403, "That isn't your character.")
    ch.status = "retired"
    ch.is_main = False
    await record(session, "roster.character_retired", actor_id=viewer.id, entity=ch, summary=f"Retired {ch.name}")
    await session.commit()
    return redirect(request, f"/roster/members/{ch.member_id or viewer.id}", f"{ch.name} retired from the roll.")


@router.post("/members/{member_id}/notes")
async def add_note(
    request: Request,
    member_id: int,
    text: str = Form(...),
    viewer: Viewer = Depends(require("members.notes")),
    session: AsyncSession = Depends(get_session),
):
    if not text.strip():
        raise HTTPException(422, "Write something first.")
    note = OfficerNote(member_id=member_id, author_id=viewer.id, text=text.strip()[:4000])
    session.add(note)
    await session.flush()
    await record(session, "roster.officer_note", actor_id=viewer.id, entity=note, summary="Officer note added")
    await session.commit()
    return redirect(request, f"/roster/members/{member_id}#notes", "Note saved.")


@router.get("/who", response_class=HTMLResponse)
async def who_form(request: Request, viewer: Viewer = Depends(require("roster.import_who"))):
    return render(request, "roster/who.html", parsed=None, skipped=None, text="")


@router.post("/who", response_class=HTMLResponse)
async def who_import(
    request: Request,
    text: str = Form(...),
    apply: str = Form(""),
    create_unknown: str = Form(""),
    viewer: Viewer = Depends(require("roster.import_who")),
    session: AsyncSession = Depends(get_session),
):
    lines, skipped = parse_who(text, hall.game)
    if apply:
        result = await services.apply_who(session, viewer, lines, create_unknown=bool(create_unknown))
        await session.commit()
        return redirect(
            request, "/roster", f"Updated {len(result['updated'])}, added {len(result['created'])} from /who."
        )
    known = {}
    for line in lines:
        ch = await services.find_character(session, line.name)
        known[line.name] = ch
    return render(request, "roster/who.html", parsed=lines, skipped=skipped, text=text, known=known)


# ----- JSON API ----------------------------------------------------------------------------
@api.get("/api/roster")
async def api_roster(
    q: str = "",
    cls: str = "",
    role: str = "",
    min_level: str = "",
    viewer: Viewer = Depends(require("members.view")),
    session: AsyncSession = Depends(get_session),
):
    rows = await services.list_characters(
        session,
        q=q,
        class_name=hall.game.class_from_any(cls) or cls,
        role=role,
        min_level=_int(min_level),
        mules=None,
        limit=500,
    )
    return {
        "characters": [
            {
                "name": c.name,
                "level": c.level,
                "class": c.class_name,
                "race": c.race,
                "server": c.server,
                "main": c.is_main,
                "bank_mule": c.is_bank_mule,
                "member": m.display_name if m else None,
                "member_id": m.id if m else None,
            }
            for c, m in rows
        ]
    }
