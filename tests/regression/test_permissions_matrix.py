"""The rank/duty model behaves exactly as documented for every registered permission."""

from __future__ import annotations

from mmgu.core.hall import hall
from mmgu.core.permissions import DUTIES, RANK_ORDER, RANKS, PermissionRegistry, best_rank


def test_every_permission_follows_its_minimum_rank():
    for key, perm in hall.perms.perms.items():
        for rank, _ in RANKS:
            expected = RANK_ORDER[rank] >= RANK_ORDER[perm.min_rank]
            assert hall.perms.allowed(key, rank, set()) is expected, (key, rank)


def test_duties_grant_their_permissions_to_any_rank():
    for key, perm in hall.perms.perms.items():
        for duty in perm.duties:
            assert duty in {d for d, _, _ in DUTIES}, f"{key} names unknown duty {duty}"
            assert hall.perms.allowed(key, "guest", {duty}), (key, duty)


def test_leader_has_everything_and_guest_almost_nothing():
    assert hall.perms.grants("leader", set()) == set(hall.perms.perms)
    guest = hall.perms.grants("guest", set())
    assert not {p for p in guest if p.startswith(("admin.", "members.manage", "audit."))}


def test_unknown_permission_is_leader_only():
    reg = PermissionRegistry()
    assert reg.allowed("made.up", "leader", set())
    assert not reg.allowed("made.up", "officer", {"banker"})


def test_overrides_change_rules():
    key = "archive.view"
    o = {key: {"min_rank": "guest", "duties": []}}
    assert hall.perms.allowed(key, "guest", set(), o)
    o = {key: {"min_rank": "officer", "duties": ["crafter"]}}
    assert not hall.perms.allowed(key, "member", set(), o)
    assert hall.perms.allowed(key, "recruit", {"crafter"}, o)


def test_best_rank_picks_highest_and_falls_back():
    assert best_rank({"member", "officer", "banker"}, "recruit") == "officer"
    assert best_rank({"banker"}, "recruit") == "recruit"


async def test_permission_overrides_cannot_lock_out_admins(make_client):
    lead = await make_client()
    r = await lead.post("/steward/permissions", {"rank:admin.permissions": "guest"})
    assert r.status_code == 303
    from mmgu.core import store

    assert "admin.permissions" not in (store.get("permissions.overrides") or {})
    assert (await lead.get("/steward/permissions")).status_code == 200


async def test_override_takes_effect_on_pages(make_client):
    lead = await make_client()
    rookie = await make_client("Rookie", "recruit")
    assert (await rookie.get("/archive")).status_code == 200
    await lead.post("/steward/permissions", {"rank:archive.view": "officer"})
    assert (await rookie.get("/archive")).status_code == 403
    await lead.post("/steward/permissions", {"rank:archive.view": "recruit"})
    assert (await rookie.get("/archive")).status_code == 200


async def test_officers_cannot_create_leaders(make_client):
    import re

    await make_client()
    officer = await make_client("Officer Ott", "officer")
    await make_client("Target", "member")
    page = (await officer.get("/steward/members")).text
    ids = re.findall(r'action="/steward/members/(\d+)"', page)
    names = re.findall(r'aria-label="Rank for ([^"]+)"', page)
    tid = dict(zip(names, ids, strict=True))["Target"]
    assert (await officer.post(f"/steward/members/{tid}", {"rank": "leader"})).status_code == 403
    assert (await officer.post(f"/steward/members/{tid}", {"rank": "officer"})).status_code == 403
    assert (await officer.post(f"/steward/members/{tid}", {"rank": "recruit"})).status_code == 303
