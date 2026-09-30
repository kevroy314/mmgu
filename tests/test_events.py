import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select


def future(days=2, hour=20):
    d = (datetime.now(UTC).replace(tzinfo=None) + timedelta(days=days)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    return d.strftime("%Y-%m-%dT%H:%M")


def event_form(**over):
    data = {
        "title": "Wyrmsbane clear",
        "kind": "Raid",
        "starts_at": future(),
        "ends_at": "",
        "tz_name": "UTC",
        "zone": "Tomb of the Last Wyrmsbane",
        "description": "Bring fire resist.",
        "capacity": "",
        "min_level": "20",
        "max_level": "",
        "role_tank": "2",
        "role_healer": "1",
    }
    data.update(over)
    return data


async def create_event(c, **over) -> int:
    r = await c.post("/events/new", event_form(**over))
    assert r.status_code == 303, r.text
    return int(r.headers["location"].rsplit("/", 1)[1])


async def member_id(name: str) -> int:
    from mmgu.core.models import Member
    from mmgu.db import session_scope

    async with session_scope() as s:
        return (await s.execute(select(Member.id).where(Member.display_name == name))).scalar_one()


async def test_event_lifecycle_pages_and_api(make_client):
    lead = await make_client()
    r = await lead.get("/events")
    assert r.status_code == 200 and "Nothing on the schedule" in r.text
    r = await lead.get("/events/new")
    assert r.status_code == 200 and 'name="tz_offset"' in r.text
    eid = await create_event(lead, repeat="1", repeat_weeks="2")
    r = await lead.get(f"/events/{eid}")
    assert r.status_code == 200
    assert "Wyrmsbane clear" in r.text and "Tank" in r.text and "Other weeks" in r.text
    r = await lead.get("/events")
    assert r.text.count("Wyrmsbane clear") == 3
    r = await lead.get(f"/events/{eid}/edit")
    assert r.status_code == 200 and "Wyrmsbane clear" in r.text
    r = await lead.post(f"/events/{eid}/edit", event_form(title="Wyrmsbane full clear", starts_at=future(3)))
    assert r.status_code == 303
    for tab in ("roster", "attendance", "loot"):
        r = await lead.get(f"/events/{eid}?tab={tab}")
        assert r.status_code == 200 and "Wyrmsbane full clear" in r.text
    r = await lead.get("/api/events?upcoming=1")
    ev = r.json()["events"][0]
    assert set(ev) >= {"id", "title", "kind", "starts_at", "zone", "signups", "url"}
    assert ev["starts_at"].endswith("Z") and ev["url"].endswith(f"/events/{ev['id']}")
    r = await lead.post(f"/events/{eid}/status", {"status": "cancelled"})
    assert r.status_code == 303
    r = await lead.get("/events?show=past")
    assert "Wyrmsbane full clear" in r.text and "Cancelled" in r.text
    for page in ("/events/attendance", "/loot", "/events?kind=Raid"):
        assert (await lead.get(page)).status_code == 200


async def test_bad_input_and_permissions(make_client):
    lead = await make_client()
    r = await lead.post("/events/new", event_form(title=""))
    assert r.status_code == 422
    r = await lead.post("/events/new", event_form(ends_at=future(1)))
    assert r.status_code == 422 and "end time" in r.text
    rookie = await make_client("Rookie", "recruit")
    assert (await rookie.get("/events")).status_code == 200
    assert (await rookie.get("/events/new")).status_code == 403
    assert (await rookie.post("/events/new", event_form())).status_code == 403
    assert (await rookie.post("/loot", {"item": "Rusty Sword", "character": "X"})).status_code == 403
    eid = await create_event(lead)
    assert (await rookie.post(f"/events/{eid}/attendance", {"present": "1"})).status_code == 403
    assert (await rookie.get(f"/events/{eid}/edit")).status_code == 403
    # raid leader duty grants management
    rl = await make_client("Rhea", "raidlead")
    assert (await rl.get("/events/new")).status_code == 200


async def test_signups_roster_bench_and_one_click(make_client):
    lead = await make_client()
    eid = await create_event(lead, capacity="2")
    # no character yet → clear message
    r = await lead.post(f"/events/{eid}/signup", {"status": "going"})
    assert r.status_code == 422 and "Muster Roll" in r.text
    await lead.post("/roster/characters", {"name": "Tankard", "class_name": "Paladin", "level": "30", "is_main": "1"})
    await lead.post("/roster/characters", {"name": "Healy", "class_name": "Cleric", "level": "25"})
    r = await lead.post(f"/events/{eid}/signup", {"status": "going", "next": "/"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    r = await lead.get(f"/events/{eid}")
    assert "Tankard" in r.text and "Short on" in r.text and "healer (1 more)" in r.text
    # switch to the cleric: role follows the class
    healy_id = re.search(r'<option value="(\d+)"\s*>Healy', r.text).group(1)
    await lead.post(f"/events/{eid}/signup", {"status": "late", "character_id": healy_id, "note": "after dinner"})
    r = await lead.get(f"/events/{eid}")
    assert "after dinner" in r.text and "tank (2 more)" in r.text and "healer" not in r.text.split("Short on")[1][:80]
    others = []
    for name, cls in (("Bo", "Rogue"), ("Cy", "Wizard")):
        c = await make_client(name, "recruit")
        await c.post("/roster/characters", {"name": name + "char", "class_name": cls, "level": "20"})
        r = await c.post(f"/events/{eid}/signup", {"status": "going"})
        assert r.status_code == 303
        others.append(c)
    r = await lead.get(f"/events/{eid}")
    assert "bench" in r.text and "Cychar" in r.text
    # someone else can't sign up with my character
    r = await others[0].post(f"/events/{eid}/signup", {"status": "going", "character_id": healy_id})
    assert r.status_code == 422
    # dashboard card shows the event with my status
    r = await others[0].get("/")
    assert "Coming up" in r.text and "Wyrmsbane clear" in r.text
    r = await lead.get("/api/events")
    assert r.json()["events"][0]["signups"] == 3
    from mmgu.db import session_scope
    from mmgu.modules.events.services import next_events

    async with session_scope() as s:
        nxt = await next_events(s, 3)
    assert nxt[0]["id"] == eid and nxt[0]["going"] == 3 and isinstance(nxt[0]["starts_at"], datetime)
    r = await lead.get("/search?q=Wyrms")
    assert f"/events/{eid}" in r.text


async def test_attendance_checkin_who_and_summary(make_client):
    lead = await make_client()
    await lead.post("/roster/characters", {"name": "Velyra", "class_name": "Wizard", "level": "30", "is_main": "1"})
    bo = await make_client("Bo", "recruit")
    await bo.post("/roster/characters", {"name": "Bochar", "class_name": "Rogue", "level": "20"})
    eid = await create_event(lead)
    await bo.post(f"/events/{eid}/signup", {"status": "going"})
    # check-in only while the event is running
    assert (await bo.post(f"/events/{eid}/checkin")).status_code == 422
    await lead.post(f"/events/{eid}/status", {"status": "active"})
    r = await bo.post(f"/events/{eid}/checkin")
    assert r.status_code == 303
    r = await bo.get(f"/events/{eid}?tab=attendance")
    assert "checked in" in r.text and "Bochar" in r.text
    r = await lead.post(
        f"/events/{eid}/who", {"text": "[30 Wizard] Velyra (High Elf) <Test Guild>\n[12 Cleric] Stranger"}
    )
    assert r.status_code == 303
    r = await lead.get(f"/events/{eid}?tab=attendance")
    assert "Stranger" in r.text and "1 marked" in r.text
    # checkbox list: untick Bo
    lead_id = await member_id("Leader Lyra")
    r = await lead.post(f"/events/{eid}/attendance", {"present": str(lead_id)})
    assert r.status_code == 303
    r = await lead.get("/events/attendance")
    assert "Leader Lyra" in r.text and "100%" in r.text
    r = await lead.get(f"/roster/members/{lead_id}")
    assert "attendance, 30 days" in r.text
    await lead.post(f"/events/{eid}/status", {"status": "done"})


async def test_loot_awards_history_and_item_panel(make_client):
    from mmgu.core.hall import hall

    seen = []

    async def spy(e):
        seen.append(e.data)

    hall.bus.subscribe("loot.awarded", spy)
    lead = await make_client()
    await lead.post("/roster/characters", {"name": "Velyra", "class_name": "Wizard", "level": "30"})
    r = await lead.post("/archive/new", {"name": "Wyrmscale Cloak", "slots": ["BACK"]})
    item_url = r.headers["location"]
    eid = await create_event(lead)
    r = await lead.post(
        "/loot",
        {
            "item": "wyrmscale cloak",
            "character": "velyra",
            "method": "Council",
            "points": "15",
            "event_id": str(eid),
            "from_event": "1",
        },
    )
    assert r.status_code == 303 and r.headers["location"] == f"/events/{eid}?tab=loot"
    r = await lead.post("/loot", {"item": "Mystery Gem", "character": "Pugsley", "method": "Roll", "note": "pug"})
    assert r.status_code == 303
    r = await lead.get(f"/events/{eid}?tab=loot")
    assert "Wyrmscale Cloak" in r.text and "Council" in r.text
    r = await lead.get(item_url)
    assert "Who has received this" in r.text and "Velyra" in r.text
    r = await lead.get("/loot?item=gem")
    assert "Mystery Gem" in r.text and "Wyrmscale Cloak" not in r.text
    r = await lead.get(f"/loot?event={eid}")
    assert "Wyrmscale Cloak" in r.text and "Mystery Gem" not in r.text
    lead_id = await member_id("Leader Lyra")
    r = await lead.get(f"/loot?member={lead_id}&since=2000-01-01&until=2999-01-01")
    assert "Wyrmscale Cloak" in r.text
    r = await lead.get("/loot/suggest?q=wyrm")
    assert "Wyrmscale Cloak" in r.text
    r = await lead.get(f"/roster/members/{lead_id}")
    assert "Wyrmscale Cloak" in r.text
    rookie = await make_client("Rookie", "recruit")
    r = await rookie.get("/loot")
    assert "Mystery Gem" in r.text  # transparent for everyone with loot.view
    await hall.bus.drain()
    assert any(d.get("item_id") for d in seen) and any(d.get("item_id") is None for d in seen)
    award_id = re.search(r'action="/loot/(\d+)/delete"', (await lead.get("/loot?item=gem")).text).group(1)
    r = await lead.post(f"/loot/{award_id}/delete")
    assert r.status_code == 303
    assert "Mystery Gem" not in (await lead.get("/loot")).text
    assert (await lead.post("/loot", {"item": "", "character": "X"})).status_code == 422


async def test_calendar_feed(make_client):
    lead = await make_client()
    eid = await create_event(lead, description="Line one, with comma; and semicolon\nLine two " + "x" * 120)
    r = await lead.get("/events")
    url = re.search(r'value="[^"]*(/events\.ics\?token=[^"]+)"', r.text).group(1)
    r = await lead.http.get(url.replace("&amp;", "&"))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
    body = r.text
    assert body.startswith("BEGIN:VCALENDAR\r\n") and body.endswith("END:VCALENDAR\r\n")
    assert f"UID:event-{eid}@" in body and re.search(r"DTSTART:\d{8}T\d{6}Z\r\n", body)
    assert "with comma\\; and semicolon" in body.replace("\r\n ", "") or "comma\\," in body
    assert all(len(line.encode()) <= 75 for line in body.split("\r\n"))
    assert "\n" not in body.replace("\r\n", "")
    await lead.post("/events/calendar/regenerate")
    r = await lead.http.get(url.replace("&amp;", "&"))
    assert r.status_code == 404
    assert (await lead.http.get("/events.ics?token=nope")).status_code == 404


async def test_job_reminds_starts_and_closes(make_client):
    from mmgu.core.hall import hall
    from mmgu.core.models import Notice
    from mmgu.db import session_scope
    from mmgu.modules.events import services
    from mmgu.modules.events.models import Event

    starting = []

    async def spy(e):
        starting.append(e.data["event_id"])

    hall.bus.subscribe("events.starting", spy)
    lead = await make_client()
    await lead.post("/roster/characters", {"name": "Velyra", "class_name": "Wizard", "level": "30"})
    eid = await create_event(lead)
    await lead.post(f"/events/{eid}/signup", {"status": "going"})
    async with session_scope() as s:
        start = (await s.get(Event, eid)).starts_at
    async with session_scope() as s:
        done = await services.tick(s, now=start - timedelta(minutes=20))
    assert done["reminded"] == [eid]
    async with session_scope() as s:
        again = await services.tick(s, now=start - timedelta(minutes=10))
        notices = (await s.execute(select(Notice).where(Notice.text.like("%starts in%")))).scalars().all()
    assert again["reminded"] == [] and len(notices) == 1
    async with session_scope() as s:
        done = await services.tick(s, now=start + timedelta(minutes=1))
    assert done["started"] == [eid]
    async with session_scope() as s:
        assert (await s.get(Event, eid)).status == "active"
        done = await services.tick(s, now=start + timedelta(minutes=2))
    assert done["started"] == []
    async with session_scope() as s:
        done = await services.tick(s, now=start + timedelta(hours=7))
        assert (await s.get(Event, eid)).status == "done"
    assert done["closed"] == [eid]
    await hall.bus.drain()
    assert starting == [eid]
    # the job entry point runs without Discord
    from mmgu.modules.events.hooks import run_job

    await run_job()


def test_parse_when():
    from mmgu.modules.events.when import WhenError, parse_when, zone

    now = datetime(2026, 9, 30, 18, 0)  # a Wednesday, 18:00 UTC
    assert parse_when("2026-10-04 20:00 UTC", now=now) == datetime(2026, 10, 4, 20, 0)
    assert parse_when("in 3 hours", now=now) == datetime(2026, 9, 30, 21, 0)
    assert parse_when("in 1h30m", now=now) == datetime(2026, 9, 30, 19, 30)
    assert parse_when("tomorrow 8pm", now=now) == datetime(2026, 10, 1, 20, 0)
    assert parse_when("saturday 20:00", now=now) == datetime(2026, 10, 3, 20, 0)
    assert parse_when("17:00", now=now) == datetime(2026, 10, 1, 17, 0)  # already past today
    assert parse_when("Oct 4 8:30pm", now=now) == datetime(2026, 10, 4, 20, 30)
    # member time zone: 8pm Chicago (CDT, UTC-5) is 01:00 UTC next day
    assert parse_when("tomorrow 8pm", tz=zone("America/Chicago"), now=now) == datetime(2026, 10, 2, 1, 0)
    # an explicit zone beats the profile zone
    assert parse_when("tomorrow 8pm UTC", tz=zone("America/Chicago"), now=now) == datetime(2026, 10, 1, 20, 0)
    assert parse_when("tomorrow 8pm PDT", now=now) == datetime(2026, 10, 2, 3, 0)
    for bad in ("whenever", "tomorrow", "in 3 fortnights", "25:00"):
        with pytest.raises(WhenError):
            parse_when(bad, now=now)
    assert zone("Not/AZone") is None


def test_repeat_keeps_local_time_across_dst():
    from mmgu.modules.events.when import from_utc, to_utc, zone

    chicago = zone("America/Chicago")
    first = to_utc(datetime(2026, 10, 24, 20, 0), chicago)  # CDT
    later = to_utc(from_utc(first, chicago) + timedelta(weeks=2), chicago)  # after DST ends on Nov 1
    assert from_utc(later, chicago).hour == 20 and later.hour != first.hour


class FakeBot:
    def __init__(self):
        self.commands, self.handlers = [], {}

    def add_command(self, cmd):
        self.commands.append(cmd)

    def on_component(self, key):
        def deco(fn):
            self.handlers[key] = fn
            return fn

        return deco


def fake_interaction(user_id: int, name: str, data: dict | None = None):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    sent = []
    response = MagicMock()
    response.is_done = MagicMock(return_value=False)

    async def send_message(content=None, **kw):
        sent.append((content, kw))
        response.is_done.return_value = True

    response.send_message = send_message
    response.send_modal = AsyncMock()
    followup = SimpleNamespace(send=send_message)
    msg = SimpleNamespace(id=555, channel=SimpleNamespace(id=777))
    user = SimpleNamespace(id=user_id, name=name, display_name=name, display_avatar=None)
    inter = SimpleNamespace(
        user=user,
        data=data or {},
        channel_id=777,
        response=response,
        followup=followup,
        original_response=AsyncMock(return_value=msg),
    )
    return inter, sent


async def test_discord_create_rsvp_and_loot(make_client):
    from mmgu.core.models import Identity, RoleGrant
    from mmgu.db import session_scope
    from mmgu.modules.events import bot as ebot
    from mmgu.modules.events.models import Event

    await make_client()  # boots the app and database
    fake = FakeBot()
    await ebot.setup(fake)
    names = {c.name for c in fake.commands}
    assert names == {"event", "events", "loot"}
    # first contact creates the member; make them an officer
    inter, sent = fake_interaction(4242, "Raidlead Rho")
    await fake.handlers["events:rsvp"](inter, ["999", "going"])
    async with session_scope() as s:
        mid = (await s.execute(select(Identity.member_id).where(Identity.subject == "4242"))).scalar_one()
        s.add(RoleGrant(member_id=mid, role="officer"))
    modal = {
        "components": [
            {"components": [{"custom_id": "title", "value": "Scarwood night"}]},
            {"components": [{"custom_id": "when", "value": "in 3 hours"}]},
            {"components": [{"custom_id": "zone", "value": "Scarwood"}]},
            {"components": [{"custom_id": "kind", "value": "group"}]},
            {"components": [{"custom_id": "description", "value": ""}]},
        ]
    }
    inter, sent = fake_interaction(4242, "Raidlead Rho", modal)
    await fake.handlers["events:create"](inter, [])
    assert sent and sent[0][1]["embed"].title == "Group: Scarwood night"
    async with session_scope() as s:
        ev = (await s.execute(select(Event).where(Event.title == "Scarwood night"))).scalar_one()
        assert ev.discord_message_id == "555" and ev.discord_channel_id == "777" and ev.kind == "Group"
        eid = ev.id
    # unparseable time → friendly error, nothing created
    bad = {"components": [{"components": [{"custom_id": "when", "value": "someday"}]}]}
    inter, sent = fake_interaction(4242, "Raidlead Rho", bad)
    await fake.handlers["events:create"](inter, [])
    assert "couldn't read" in sent[0][0]
    # RSVP without a character → told to /char add
    inter, sent = fake_interaction(5151, "Newbie")
    await fake.handlers["events:rsvp"](inter, [str(eid), "going"])
    assert "/char add" in sent[0][0]
    # Can't works without a character; Going works once they have one
    inter, sent = fake_interaction(5151, "Newbie")
    await fake.handlers["events:rsvp"](inter, [str(eid), "cant"])
    assert "Can't" in sent[0][0]
    async with session_scope() as s:
        from mmgu.core.models import Character

        nid = (await s.execute(select(Identity.member_id).where(Identity.subject == "5151"))).scalar_one()
        s.add(Character(name="Newbchar", class_name="Cleric", level=5, member_id=nid))
    inter, sent = fake_interaction(5151, "Newbie")
    await fake.handlers["events:rsvp"](inter, [str(eid), "going"])
    assert "Going" in sent[0][0] and "Newbchar" in sent[0][0] and "healer" in sent[0][0]
    async with session_scope() as s:
        emb = await ebot.event_embed(s, await s.get(Event, eid))
    going = next(f for f in emb.fields if f.name.startswith("Going"))
    assert "Newbchar (CLR)" in going.value
    # /loot award defaults to the single active event
    async with session_scope() as s:
        (await s.get(Event, eid)).status = "active"
    award_cmd = next(c for c in fake.commands if c.name == "loot").get_command("award")
    inter, sent = fake_interaction(4242, "Raidlead Rho")
    await award_cmd.callback(
        inter, item="Ember Ring", character="newbchar", method=None, points=5.0, event=None, note=None
    )
    assert "Ember Ring" in sent[0][0] and "Scarwood night" in sent[0][0] and "5 pts" in sent[0][0]
    hist = next(c for c in fake.commands if c.name == "loot").get_command("history")
    inter, sent = fake_interaction(5151, "Newbie")
    await hist.callback(inter, member=None, item="ember")
    assert "Ember Ring" in sent[0][1]["embed"].description
    events_cmd = next(c for c in fake.commands if c.name == "events")
    inter, sent = fake_interaction(5151, "Newbie")
    await events_cmd.callback(inter)
    assert "Scarwood night" in sent[0][1]["embed"].description
