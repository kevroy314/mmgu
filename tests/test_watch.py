from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select


async def _timer(c, creature="Lord Grimjaw", respawn="60", variance="10", zone="Scarwood"):
    r = await c.post(
        "/watch/timers",
        {"creature": creature, "zone": zone, "respawn_minutes": respawn, "variance_minutes": variance, "active": "1"},
    )
    assert r.status_code == 303, r.text
    from mmgu.db import session_scope
    from mmgu.modules.watch.models import Timer

    async with session_scope() as s:
        return (await s.execute(select(Timer).where(Timer.creature == creature))).scalar_one().id


def test_parse_when_and_status():
    from mmgu.modules.watch import services

    now = datetime(2026, 10, 1, 12, 0, 0)
    assert services.parse_when("now", now=now) == now
    assert services.parse_when("ago", "30", now=now) == now - timedelta(minutes=30)
    # browser in UTC-5 (getTimezoneOffset() = 300): 06:30 local = 11:30 UTC
    assert services.parse_when("at", at="2026-10-01T06:30", tz_offset="300", now=now) == datetime(2026, 10, 1, 11, 30)
    assert services.parse_when("at", at="2026-10-01T11:30", now=now) == datetime(2026, 10, 1, 11, 30)  # no offset: UTC
    with pytest.raises(services.WatchError):
        services.parse_when("at", at="2026-10-01T18:00", tz_offset="0", now=now)  # future
    with pytest.raises(services.WatchError):
        services.parse_when("ago", "abc", now=now)
    ws, we = now + timedelta(minutes=50), now + timedelta(minutes=70)
    assert services.status_of(ws, we, now) == "waiting"
    assert services.status_of(ws, we, now + timedelta(minutes=60)) == "open"
    assert services.status_of(ws, we, now + timedelta(minutes=71)) == "overdue"
    assert services.status_of(None, None, now) == "unknown"


async def test_board_report_and_api(make_client):
    c = await make_client()
    r = await c.get("/watch")
    assert r.status_code == 200 and "No creatures tracked yet" in r.text
    tid = await _timer(c)
    r = await c.get("/watch")
    assert "Lord Grimjaw" in r.text and "No time of death" in r.text

    r = await c.post(f"/watch/timers/{tid}/tod", {"when": "now"})
    assert r.status_code == 303
    r = await c.get("/watch")
    assert 'data-open-label="window open"' in r.text and "Waiting" in r.text
    r = await c.get(f"/watch/timers/{tid}")
    assert r.status_code == 200 and "manual" in r.text

    # the board's report form, with an explicit local time + offset
    at = (datetime.now(UTC) - timedelta(minutes=55)).replace(tzinfo=None)
    r = await c.post(
        "/watch/tod", {"timer_id": str(tid), "at": at.strftime("%Y-%m-%dT%H:%M"), "tz_offset": "0", "note": "late"}
    )
    assert r.status_code == 303
    r = await c.post("/watch/tod", {"creature": "nobody"})
    assert r.status_code == 422

    r = await c.get("/api/watch/timers")
    t = r.json()["timers"][0]
    assert set(t) >= {"id", "creature", "zone", "window_start", "window_end", "status"}
    assert t["creature"] == "Lord Grimjaw" and t["status"] == "waiting"  # the "now" report is the latest

    r = await c.post_json("/api/watch/tod", {"creature": "lord grimjaw", "minutes_ago": 55})
    body = r.json()
    assert set(body) == {"timer_id", "window_start", "window_end"} and body["timer_id"] == tid
    r = await c.post_json("/api/watch/tod", {"creature": "Nobody", "minutes_ago": 1})
    assert r.status_code == 404

    # editing, history removal, search, dashboard card
    r = await c.post(
        f"/watch/timers/{tid}",
        {"creature": "Lord Grimjaw", "respawn_minutes": "90", "variance_minutes": "0", "active": "1"},
    )
    assert r.status_code == 303
    from mmgu.db import session_scope
    from mmgu.modules.watch.models import DeathReport

    async with session_scope() as s:
        did = (await s.execute(select(DeathReport.id).limit(1))).scalar_one()
    r = await c.post(f"/watch/deaths/{did}/delete")
    assert r.status_code == 303
    r = await c.get("/search?q=grimj")
    assert "Spawn timer" in r.text
    r = await c.get("/")
    assert "The Watch" in r.text


async def test_report_by_name_window_job_and_watchers(make_client):
    from mmgu.core.hall import hall
    from mmgu.core.models import Member, Notice
    from mmgu.db import session_scope
    from mmgu.modules.watch import services
    from mmgu.modules.watch.models import DeathReport

    c = await make_client()
    tid = await _timer(c, "Vorn the Ashen", respawn="30", variance="5")
    r = await c.post(f"/watch/timers/{tid}/watch")
    assert r.status_code == 303

    seen = []

    async def listener(e):
        seen.append((e.name, e.data))

    hall.bus.subscribe("watch.tod_reported", listener)
    hall.bus.subscribe("watch.window_open", listener)

    async with session_scope() as s:
        assert await services.report_death_by_name(s, None, "Unknown Beast", datetime.now(UTC)) is None
        # a timezone-aware time is converted to naive UTC
        died = datetime.now(UTC) - timedelta(minutes=20)
        d = await services.report_death_by_name(s, None, "VORN THE ASHEN", died, source="ledger")
        assert isinstance(d, DeathReport) and d.source == "ledger" and d.died_at.tzinfo is None
        # a second report of the same kill is folded into the first
        again = await services.report_death_by_name(s, None, "Vorn the Ashen", died + timedelta(seconds=30))
        assert again.id == d.id
        death_id = d.id

    now = services.utcnow()
    async with session_scope() as s:
        # 20 min after death, window opens at 25: inside the 10-min early warning
        assert await services.check_windows(s, now=now) == []
    async with session_scope() as s:
        opened = await services.check_windows(s, now=now + timedelta(minutes=6))
        assert opened == [tid]
        # only once per death
        assert await services.check_windows(s, now=now + timedelta(minutes=7)) == []
    await hall.bus.drain()
    names = [n for n, _ in seen]
    assert names.count("watch.window_open") == 1
    assert ("watch.tod_reported", {"timer_id": tid, "death_id": death_id}) in seen

    async with session_scope() as s:
        lyra = (await s.execute(select(Member).where(Member.display_name == "Leader Lyra"))).scalar_one()
        texts = (await s.execute(select(Notice.text).where(Notice.member_id == lyra.id))).scalars().all()
    assert any("opens in about" in t for t in texts)
    assert any("window is open now" in t for t in texts)

    r = await c.get("/watch")
    assert "Watching" in r.text
    r = await c.post(f"/watch/timers/{tid}/watch")
    r = await c.get("/watch")
    assert "Watching" not in r.text


async def test_camps(make_client):
    from mmgu.db import session_scope
    from mmgu.modules.watch import services
    from mmgu.modules.watch.models import Camp

    c = await make_client()
    await c.post("/roster/characters", {"name": "Campbell", "class_name": "Ranger", "level": "30"})
    r = await c.post("/watch/camps", {"zone": "Scarwood", "camp": "Ogre hill", "character": "Campbell"})
    assert r.status_code == 303
    r = await c.get("/watch")
    assert "Ogre hill" in r.text and "Campbell" in r.text
    # a new check-in replaces the old one
    await c.post("/watch/camps", {"zone": "Scarwood", "camp": "Bridge"})
    async with session_scope() as s:
        active = await services.active_camps(s)
        assert [cm.camp for cm, _ in active] == ["Bridge"]
        cid = active[0][0].id
    r = await c.post(f"/watch/camps/{cid}/end")
    assert r.status_code == 303
    async with session_scope() as s:
        assert await services.active_camps(s) == []
    # expiry
    await c.post("/watch/camps", {"zone": "Scarwood", "camp": "Tower"})
    async with session_scope() as s:
        n = await services.expire_camps(s, now=services.utcnow() + timedelta(hours=5))
        assert n == 1
        camp = (await s.execute(select(Camp).where(Camp.camp == "Tower"))).scalar_one()
        assert camp.end_reason == "expired"
    r = await c.post("/watch/camps", {"zone": "", "camp": ""})
    assert r.status_code == 422


async def test_permissions(make_client):
    lead = await make_client()
    tid = await _timer(lead, "Snarl")
    rookie = await make_client("Rookie", "recruit")
    assert (await rookie.get("/watch")).status_code == 200
    r = await rookie.post("/watch/timers", {"creature": "Nope", "respawn_minutes": "5"})
    assert r.status_code == 403
    r = await rookie.post(f"/watch/timers/{tid}/tod", {"when": "ago", "minutes_ago": "3"})
    assert r.status_code == 303
    # lead's report can't be removed by the rookie
    await lead.post(f"/watch/timers/{tid}/tod", {"when": "ago", "minutes_ago": "30"})
    from mmgu.db import session_scope
    from mmgu.modules.watch.models import DeathReport

    async with session_scope() as s:
        lead_id = (await s.execute(select(DeathReport.id).order_by(DeathReport.died_at).limit(1))).scalar_one()
    r = await rookie.post(f"/watch/deaths/{lead_id}/delete")
    assert r.status_code == 403
    guest = await make_client("Guesty", "guest")
    assert (await guest.get("/watch")).status_code == 403
    assert (await guest.get("/api/watch/timers")).status_code == 403
