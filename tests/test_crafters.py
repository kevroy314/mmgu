import re

from sqlalchemy import select


async def _char(c, name, cls="Fighter", level="20"):
    r = await c.post("/roster/characters", {"name": name, "class_name": cls, "level": level})
    assert r.status_code == 303, r.text
    from mmgu.core.models import Character
    from mmgu.db import session_scope

    async with session_scope() as s:
        return (await s.execute(select(Character).where(Character.name == name))).scalar_one().id


async def _skill_row_ids(char_id):
    from mmgu.db import session_scope
    from mmgu.modules.crafters.models import CraftSkill

    async with session_scope() as s:
        rows = (await s.execute(select(CraftSkill).where(CraftSkill.character_id == char_id))).scalars().all()
        return {r.skill: r.id for r in rows}


async def test_skills_grid_directory_and_panels(make_client):
    c = await make_client()
    cid = await _char(c, "Hammerfall")
    for path in ["/crafters", "/crafters/mine", "/crafters/recipes", "/crafters/whocan", "/crafters/recipes/new"]:
        r = await c.get(path)
        assert r.status_code == 200, (path, r.text[:400])
    assert "Hammerfall" in (await c.get("/crafters/mine")).text

    r = await c.post(
        "/crafters/mine",
        {f"new-skill-{cid}": "Blacksmithing", f"new-lvl-{cid}": "142", f"new-spec-{cid}": "can make Bronze weapons"},
    )
    assert r.status_code == 303, r.text
    rows = await _skill_row_ids(cid)
    assert "Blacksmithing" in rows
    r = await c.post(
        "/crafters/mine",
        {
            f"lvl-{rows['Blacksmithing']}": "150",
            f"spec-{rows['Blacksmithing']}": "Bronze",
            f"new-skill-{cid}": "Tailoring",
            f"new-lvl-{cid}": "40",
        },
    )
    assert r.status_code == 303
    r = await c.get("/crafters")
    assert "Hammerfall" in r.text and "150" in r.text and "Blacksmithing" in r.text

    # a bad level is refused in plain words
    r = await c.post("/crafters/mine", {f"new-skill-{cid}": "Cooking", f"new-lvl-{cid}": "9999"})
    assert r.status_code == 422

    # remove a skill
    rows = await _skill_row_ids(cid)
    r = await c.post("/crafters/mine", {f"del-{rows['Tailoring']}": "1"})
    assert "Tailoring" not in await _skill_row_ids(cid)

    # member panel, dashboard card and search
    me = await c.get("/me")
    mid = re.search(r"/roster/members/(\d+)", (await c.get("/roster/me")).headers["location"]).group(1)
    r = await c.get(f"/roster/members/{mid}")
    assert r.status_code == 200 and "Tradeskills" in r.text and "Blacksmithing" in r.text
    assert me.status_code == 200
    r = await c.get("/")
    assert "The Crafters' Hall" in r.text
    r = await c.get("/search?q=Blacksm")
    assert "Tradeskill" in r.text


async def test_recipes_whocan_and_api(make_client):
    c = await make_client()
    cid = await _char(c, "Anvilia")
    await c.post("/crafters/mine", {f"new-skill-{cid}": "Blacksmithing", f"new-lvl-{cid}": "80"})
    low = await _char(c, "Tinytim")
    await c.post("/crafters/mine", {f"new-skill-{low}": "Blacksmithing", f"new-lvl-{low}": "10"})
    spec = await _char(c, "Specky")
    await c.post(
        "/crafters/mine",
        {f"new-skill-{spec}": "Tailoring", f"new-lvl-{spec}": "5", f"new-spec-{spec}": "Bronze Sword hilts"},
    )
    # an Archive item so the result links up
    r = await c.post("/archive/new", {"name": "Bronze Sword", "slots": ["PRIMARY"]})
    item_url = r.headers["location"]

    r = await c.post(
        "/crafters/recipes",
        {
            "skill": "blacksmithing",
            "result": "bronze sword",
            "trivial": "60",
            "components": "2 Bronze Bar\nLeather Grip x1\nWater Flask",
            "notes": "Use a forge.",
            "source_url": "https://example.org/bronze",
        },
    )
    assert r.status_code == 303, r.text
    rurl = r.headers["location"]
    r = await c.get(rurl)
    assert r.status_code == 200
    assert "Bronze Sword" in r.text and "Anvilia" in r.text and "Tinytim" not in r.text
    assert "Bronze Bar" in r.text and item_url in r.text
    r = await c.get(rurl + "/edit")
    assert "2 Bronze Bar" in r.text
    r = await c.post(
        rurl + "/edit",
        {"skill": "Blacksmithing", "result": "Bronze Sword", "trivial": "", "components": "3 Bronze Bar"},
    )
    assert r.status_code == 303
    r = await c.get(rurl)
    assert "Tinytim" in r.text  # no trivial: everyone with the skill

    r = await c.get("/crafters/whocan?q=bronze sw")
    assert "Anvilia" in r.text and "Specky" in r.text
    r = await c.get("/crafters/whocan?q=Blacksmithing")
    assert "Anvilia" in r.text and "Tinytim" in r.text

    r = await c.get("/api/crafters/whocan?q=Bronze Sword")
    body = r.json()
    assert set(body) == {"results"}
    row = next(x for x in body["results"] if x["character"] == "Anvilia")
    assert row["skill"] == "Blacksmithing" and row["level"] == 80 and row["recipe"] == "Bronze Sword"
    assert {"character", "member", "skill", "level", "recipe"} <= set(row)

    # item page panel shows the recipe and makers
    r = await c.get(item_url)
    assert "Crafting" in r.text and "Anvilia" in r.text
    r = await c.get("/crafters/recipes?q=bronze")
    assert "Bronze Sword" in r.text
    r = await c.get("/search?q=Bronze")
    assert "Recipe" in r.text

    # ask for components: a Notice Board request when the board is open, else just the bus event
    from mmgu.core.hall import hall

    r = await c.post(rurl + "/ask")
    assert r.status_code == 303
    if hall.is_enabled("board"):
        assert r.headers["location"].startswith("/board/requests/")
        r = await c.get(r.headers["location"])
        assert "Components for Bronze Sword" in r.text and "3 × Bronze Bar" in r.text
    else:
        assert r.headers["location"] == rurl

    r = await c.post(rurl + "/delete")
    assert r.status_code == 303
    assert (await c.get(rurl)).status_code == 404


async def test_permissions(make_client):
    lead = await make_client()
    r = await lead.post("/crafters/recipes", {"skill": "Cooking", "result": "Bread", "components": "Flour"})
    rurl = r.headers["location"]
    lead_char = await _char(lead, "Leadchar")

    rookie = await make_client("Rookie", "recruit")
    assert (await rookie.get("/crafters")).status_code == 200
    # can't edit someone else's recipe or skills
    r = await rookie.post(rurl + "/edit", {"skill": "Cooking", "result": "Stolen Bread"})
    assert r.status_code == 403
    assert (await rookie.get(rurl + "/edit")).status_code == 403
    r = await rookie.post("/crafters/mine", {"member_id": "1"})
    assert r.status_code == 403
    # skills for a character that isn't theirs: a grid for their own member won't include it
    r = await rookie.post("/crafters/mine", {f"new-skill-{lead_char}": "Cooking", f"new-lvl-{lead_char}": "5"})
    assert r.status_code == 303
    assert await _skill_row_ids(lead_char) == {}

    guest = await make_client("Guesty", "guest")
    assert (await guest.get("/crafters")).status_code == 403
    assert (await guest.get("/api/crafters/whocan?q=bread")).status_code == 403
