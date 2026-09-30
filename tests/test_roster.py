async def test_add_character_and_who_import(make_client):
    c = await make_client()
    r = await c.post("/roster/characters", {"name": "velyra", "class_name": "Wizard", "level": "12", "is_main": "1"})
    assert r.status_code == 303, r.text
    r = await c.get("/roster")
    assert "Velyra" in r.text
    r = await c.post(
        "/roster/who", {"text": "[14 Wizard] Velyra (High Elf)\n[3 Cleric] Newbie", "apply": "1", "create_unknown": "1"}
    )
    assert r.status_code == 303
    r = await c.get("/roster?mules=all")
    assert "Newbie" in r.text and ">14<" in r.text


async def test_cannot_edit_someone_elses_character(make_client):
    a = await make_client("Alpha", "member")
    await a.post("/roster/characters", {"name": "Alphachar", "class_name": "Rogue", "level": "5"})
    b = await make_client("Bravo", "member")
    r = await b.get("/roster")
    import re

    # find Alpha's member page link and try to add a character for them
    m = re.search(r'/roster/members/(\d+)">Alpha<', r.text)
    assert m
    r = await b.post("/roster/characters", {"name": "Sneaky", "member_id": m.group(1)})
    assert r.status_code == 422
