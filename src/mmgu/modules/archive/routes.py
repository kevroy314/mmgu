from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mmgu.core.audit import record
from mmgu.core.auth import Viewer, require
from mmgu.core.hall import hall
from mmgu.core.models import Member, Proposal
from mmgu.core.uploads import UploadError, parse_crop, save_image
from mmgu.core.web import redirect, render
from mmgu.db import get_session, utcnow
from mmgu.modules.archive import services
from mmgu.modules.archive.models import DropReport, Item, ItemImage, ItemRevision
from mmgu.modules.archive.reading import read_screenshot, reader_available

router = APIRouter()


def _filters(request: Request) -> dict[str, str]:
    qp = request.query_params
    return {k: qp.get(k, "") for k in ("q", "slot", "cls", "stat", "item_type", "flag", "zone", "creature")}


@router.get("/archive", response_class=HTMLResponse)
async def index(
    request: Request, viewer: Viewer = Depends(require("archive.view")), session: AsyncSession = Depends(get_session)
):
    f = _filters(request)
    searching = any(f.values())
    items = await services.search_items(session, **f, limit=90 if searching else 24)
    total = (await session.execute(select(func.count()).select_from(Item))).scalar_one()
    creatures, zones = await services.known_places(session)
    top = await services.top_catalogers(session)
    tpl = "archive/_results.html" if request.headers.get("hx-target") == "results" else "archive/index.html"
    return render(
        request,
        tpl,
        items=items,
        f=f,
        searching=searching,
        total=total,
        zones=zones,
        creatures=creatures,
        top=top,
        lines=services.item_lines,
    )


@router.get("/archive/suggest", response_class=HTMLResponse)
async def suggest(
    request: Request,
    q: str = "",
    into: str = "",
    name: str = "",
    viewer: Viewer = Depends(require("archive.view")),
    session: AsyncSession = Depends(get_session),
):
    from markupsafe import escape

    names = await services.suggest_names(session, q or into or name, 12)
    opts = "".join(f'<option value="{escape(n)}"></option>' for _, n in names)
    return HTMLResponse(f'<datalist id="item-names">{opts}</datalist>')


@router.get("/archive/items/{item_id}", response_class=HTMLResponse)
async def item_page(
    request: Request,
    item_id: int,
    viewer: Viewer = Depends(require("archive.view")),
    session: AsyncSession = Depends(get_session),
):
    item = await session.get(Item, item_id)
    if item is None:
        raise HTTPException(404, "That item isn't in the Archive (it may have been merged into another).")
    drops = await services.drop_summary(session, item.id)
    images = (
        (
            await session.execute(
                select(ItemImage).where(ItemImage.item_id == item.id).order_by(ItemImage.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    revisions = (
        await session.execute(
            select(ItemRevision, Member)
            .join(Member, Member.id == ItemRevision.editor_id, isouter=True)
            .where(ItemRevision.item_id == item.id)
            .order_by(ItemRevision.at.desc())
            .limit(20)
        )
    ).all()
    finder = await session.get(Member, item.first_cataloged_by) if item.first_cataloged_by else None
    panels = []
    for c in hall.ext.get("item.panels", hall.enabled_ids):
        html = await c.fn(request, session, item)
        if html:
            panels.append(html)
    creatures, zones = await services.known_places(session)
    return render(
        request,
        "archive/item.html",
        item=item,
        L=services.item_lines(item),
        drops=drops,
        images=images,
        revisions=revisions,
        finder=finder,
        panels=panels,
        creatures=creatures,
        zones=zones,
        wiki=hall.game.wiki_url(item.name),
        can_stream=hall.is_enabled("overlay") and viewer.can("overlay.push"),
    )


@router.get("/archive/new", response_class=HTMLResponse)
async def new_form(
    request: Request,
    proposal: int | None = None,
    viewer: Viewer = Depends(require("archive.catalog")),
    session: AsyncSession = Depends(get_session),
):
    prop = None
    data: dict = {}
    if proposal:
        prop = await session.get(Proposal, proposal)
        if prop is None or prop.kind != "archive.item" or prop.status != "pending":
            raise HTTPException(404, "That suggestion was already handled.")
        data = dict(prop.payload.get("item") or {})
        data.setdefault("_creature", prop.payload.get("creature"))
        data.setdefault("_zone", prop.payload.get("zone"))
    creatures, zones = await services.known_places(session)
    existing = await services.find_by_name(session, data["name"]) if data.get("name") else None
    return render(
        request,
        "archive/new.html",
        data=data,
        proposal=prop,
        creatures=creatures,
        zones=zones,
        reader=reader_available(),
        existing=existing,
    )


@router.post("/archive/read")
async def read(
    request: Request, viewer: Viewer = Depends(require("archive.catalog")), session: AsyncSession = Depends(get_session)
):
    """Upload a screenshot and let the reader add-on suggest the fields."""
    form = await request.form()
    shot = form.get("screenshot")
    if shot is None or not getattr(shot, "filename", ""):
        raise HTTPException(422, "Choose or paste a screenshot first.")
    try:
        up = await save_image(session, await shot.read(), uploaded_by=viewer.id, crop=parse_crop(form.get("crop")))
    except UploadError as e:
        raise HTTPException(422, str(e)) from e
    prop = await read_screenshot(session, viewer, up, creature=form.get("creature"), zone=form.get("zone"))
    await session.commit()
    msg = (
        "Read the screenshot. Check every field before saving."
        if prop.source != "manual"
        else "Screenshot attached. Fill in the fields by hand."
    )
    if prop.error and prop.source == "manual":
        msg = "The screenshot reader couldn't read that one. Fill in the fields by hand."
    return redirect(request, prop.payload["review_url"], msg, "ok" if prop.source != "manual" else "warn")


@router.post("/archive/new")
async def create(
    request: Request, viewer: Viewer = Depends(require("archive.catalog")), session: AsyncSession = Depends(get_session)
):
    form = await request.form()
    upload_id = None
    prop = None
    if form.get("proposal_id"):
        prop = await session.get(Proposal, int(form["proposal_id"]))
        if prop is not None and prop.status == "pending":
            upload_id = prop.upload_id
    shot = form.get("screenshot")
    if shot is not None and getattr(shot, "filename", ""):
        try:
            up = await save_image(session, await shot.read(), uploaded_by=viewer.id, crop=parse_crop(form.get("crop")))
            upload_id = up.id
        except UploadError as e:
            raise HTTPException(422, str(e)) from e
    data = services.fields_from_form(form)
    try:
        item = await services.create_item(session, viewer, data, upload_id=upload_id)
        msg = f"{item.name} is in the Archive. Nice find!"
    except services.DuplicateItem as dup:
        item = dup.item
        if upload_id:
            await services.add_image(session, viewer, item, upload_id)
        msg = f"{item.name} was already cataloged, so your screenshot and drop were added to it."
    except services.ArchiveError as e:
        raise HTTPException(422, str(e)) from e
    await services.add_drop(
        session,
        viewer,
        item,
        creature=form.get("creature"),
        zone=form.get("zone"),
        note=form.get("drop_note"),
        source="screenshot" if prop else "manual",
    )
    if prop is not None and prop.status == "pending":
        prop.status = "accepted"
        prop.decided_by = viewer.id
        prop.decided_at = utcnow()
        prop.result_ref = f"archive.item:{item.id}"
        await record(session, "proposal.accepted", actor_id=viewer.id, entity=prop, summary=prop.summary)
    await session.commit()
    return redirect(request, f"/archive/items/{item.id}", msg)


@router.get("/archive/items/{item_id}/edit", response_class=HTMLResponse)
async def edit_form(
    request: Request,
    item_id: int,
    viewer: Viewer = Depends(require("archive.catalog")),
    session: AsyncSession = Depends(get_session),
):
    item = await session.get(Item, item_id)
    if item is None:
        raise HTTPException(404)
    if not (viewer.can("archive.edit") or item.first_cataloged_by == viewer.id):
        raise HTTPException(403, "Only the person who cataloged it, or members and up, can edit items.")
    data = {
        k: getattr(item, k)
        for k in (
            "name",
            "item_type",
            "slots",
            "flags",
            "classes",
            "races",
            "skill",
            "damage",
            "delay",
            "ac",
            "weight",
            "size",
            "stats",
            "resists",
            "effects",
            "description",
            "raw_text",
            "patch_seen",
        )
    }
    return render(
        request,
        "archive/new.html",
        data=data,
        item=item,
        proposal=None,
        creatures=[],
        zones=[],
        reader=False,
        existing=None,
    )


@router.post("/archive/items/{item_id}/edit")
async def edit(
    request: Request,
    item_id: int,
    viewer: Viewer = Depends(require("archive.catalog")),
    session: AsyncSession = Depends(get_session),
):
    item = await session.get(Item, item_id)
    if item is None:
        raise HTTPException(404)
    if not (viewer.can("archive.edit") or item.first_cataloged_by == viewer.id):
        raise HTTPException(403, "Only the person who cataloged it, or members and up, can edit items.")
    form = await request.form()
    try:
        await services.update_item(session, viewer, item, services.fields_from_form(form), note=form.get("note") or "")
    except services.ArchiveError as e:
        raise HTTPException(422, str(e)) from e
    await session.commit()
    return redirect(request, f"/archive/items/{item.id}", "Saved. The old version is kept in the item's history.")


@router.post("/archive/items/{item_id}/drops")
async def add_drop(
    request: Request,
    item_id: int,
    viewer: Viewer = Depends(require("archive.catalog")),
    session: AsyncSession = Depends(get_session),
):
    item = await session.get(Item, item_id)
    if item is None:
        raise HTTPException(404)
    form = await request.form()
    rep = await services.add_drop(
        session, viewer, item, creature=form.get("creature"), zone=form.get("zone"), note=form.get("note")
    )
    if rep is None:
        raise HTTPException(422, "Name the creature, the zone, or both.")
    await session.commit()
    return redirect(request, f"/archive/items/{item.id}#drops", "Drop recorded.")


@router.post("/archive/drops/{drop_id}/delete")
async def delete_drop(
    request: Request,
    drop_id: int,
    viewer: Viewer = Depends(require("archive.catalog")),
    session: AsyncSession = Depends(get_session),
):
    rep = await session.get(DropReport, drop_id)
    if rep is None:
        raise HTTPException(404)
    if rep.reported_by != viewer.id and not viewer.can("archive.moderate"):
        raise HTTPException(403, "You can only remove drop reports you made.")
    item_id = rep.item_id
    await record(session, "archive.drop_removed", actor_id=viewer.id, entity=rep, summary="Removed a drop report")
    await session.delete(rep)
    item = await session.get(Item, item_id)
    await services.reindex(session, item)
    await session.commit()
    return redirect(request, f"/archive/items/{item_id}#drops", "Drop report removed.")


@router.post("/archive/items/{item_id}/images")
async def add_image(
    request: Request,
    item_id: int,
    viewer: Viewer = Depends(require("archive.catalog")),
    session: AsyncSession = Depends(get_session),
):
    item = await session.get(Item, item_id)
    if item is None:
        raise HTTPException(404)
    form = await request.form()
    shot = form.get("screenshot")
    if shot is None or not getattr(shot, "filename", ""):
        raise HTTPException(422, "Choose a screenshot first.")
    try:
        up = await save_image(session, await shot.read(), uploaded_by=viewer.id, crop=parse_crop(form.get("crop")))
    except UploadError as e:
        raise HTTPException(422, str(e)) from e
    await services.add_image(session, viewer, item, up.id)
    await session.commit()
    return redirect(request, f"/archive/items/{item.id}", "Screenshot added.")


@router.post("/archive/items/{item_id}/merge")
async def merge(
    request: Request,
    item_id: int,
    viewer: Viewer = Depends(require("archive.moderate")),
    session: AsyncSession = Depends(get_session),
):
    item = await session.get(Item, item_id)
    form = await request.form()
    target = await services.find_by_name(session, form.get("into") or "")
    if item is None or target is None:
        raise HTTPException(422, "Type the exact name of the item to keep.")
    try:
        await services.merge_items(session, viewer, target, item)
    except services.ArchiveError as e:
        raise HTTPException(422, str(e)) from e
    await session.commit()
    return redirect(request, f"/archive/items/{target.id}", f"Merged into {target.name}.")


@router.post("/archive/items/{item_id}/status")
async def set_status(
    request: Request,
    item_id: int,
    viewer: Viewer = Depends(require("archive.moderate")),
    session: AsyncSession = Depends(get_session),
):
    item = await session.get(Item, item_id)
    if item is None:
        raise HTTPException(404)
    item.status = "active" if item.status == "stale" else "stale"
    await record(
        session,
        "archive.item_status",
        actor_id=viewer.id,
        entity=item,
        summary=f"{item.name} marked {'outdated' if item.status == 'stale' else 'current'}",
    )
    await session.commit()
    return redirect(
        request,
        f"/archive/items/{item.id}",
        "Marked as outdated (changed by a patch)." if item.status == "stale" else "Marked as current.",
    )


@router.post("/archive/items/{item_id}/stream")
async def stream(
    request: Request,
    item_id: int,
    viewer: Viewer = Depends(require("archive.view")),
    session: AsyncSession = Depends(get_session),
):
    if not (hall.is_enabled("overlay") and viewer.can("overlay.push")):
        raise HTTPException(403, "Only streamers can push to the overlay.")
    item = await session.get(Item, item_id)
    if item is None:
        raise HTTPException(404)
    await hall.bus.emit("overlay.push", actor_id=viewer.id, kind="item", item_id=item.id)
    from fastapi.responses import Response

    from mmgu.core.web import toast

    return toast(Response(status_code=204), f"{item.name} is on stream.")


@router.get("/archive/loot", response_class=HTMLResponse)
async def loot(
    request: Request,
    creature: str = "",
    zone: str = "",
    viewer: Viewer = Depends(require("archive.view")),
    session: AsyncSession = Depends(get_session),
):
    rows = await services.loot_table(session, creature=creature, zone=zone) if (creature or zone) else []
    creatures, zones = await services.known_places(session)
    return render(
        request, "archive/loot.html", rows=rows, creature=creature, zone=zone, creatures=creatures, zones=zones
    )


# ----- JSON API (companions, MCP, scripts) -------------------------------------------------
def item_json(item: Item) -> dict:
    return {
        "id": item.id,
        "name": item.name,
        "type": item.item_type,
        "slots": item.slots,
        "flags": item.flags,
        "classes": item.classes,
        "races": item.races,
        "skill": item.skill,
        "damage": item.damage,
        "delay": item.delay,
        "ac": item.ac,
        "weight": item.weight,
        "size": item.size,
        "stats": item.stats,
        "resists": item.resists,
        "effects": item.effects,
        "status": item.status,
        "url": hall.settings.base_url.rstrip("/") + f"/archive/items/{item.id}",
        "text": services.item_text(item),
    }


@router.get("/api/archive/items")
async def api_search(
    request: Request, viewer: Viewer = Depends(require("archive.view")), session: AsyncSession = Depends(get_session)
):
    items = await services.search_items(session, **_filters(request), limit=50)
    return {"items": [item_json(i) for i in items]}


@router.get("/api/archive/items/{item_id}")
async def api_item(
    item_id: int, viewer: Viewer = Depends(require("archive.view")), session: AsyncSession = Depends(get_session)
):
    item = await session.get(Item, item_id)
    if item is None:
        raise HTTPException(404)
    return {
        **item_json(item),
        "drops": [
            {k: v for k, v in d.items() if k in ("creature", "zone", "count")}
            for d in await services.drop_summary(session, item.id)
        ],
    }


@router.post("/api/archive/drops")
async def api_drop(
    request: Request, viewer: Viewer = Depends(require("archive.catalog")), session: AsyncSession = Depends(get_session)
):
    """Report a drop by item name. Unknown items become a pending suggestion, never a silent new item."""
    body = await request.json()
    name = (body.get("item") or "").strip()
    if not name:
        raise HTTPException(422, "item is required")
    item = await services.find_by_name(session, name)
    if item is None:
        prop = Proposal(
            kind="archive.item",
            source=body.get("source") or "api",
            created_by=viewer.id,
            summary=f"New item reported: {name}",
            payload={"item": {"name": name}, "creature": body.get("creature"), "zone": body.get("zone")},
        )
        session.add(prop)
        await session.flush()
        prop.payload = {**prop.payload, "review_url": f"/archive/new?proposal={prop.id}"}
        await session.commit()
        return {"status": "proposed", "proposal_id": prop.id}
    rep = await services.add_drop(
        session,
        viewer,
        item,
        creature=body.get("creature"),
        zone=body.get("zone"),
        note=body.get("note"),
        source=str(body.get("source") or "api")[:30],
        via="api",
    )
    await session.commit()
    return {"status": "recorded" if rep else "ignored", "item_id": item.id}
