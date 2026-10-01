"""The game pack is hand-edited after every patch; catch typos before they reach the guild."""

from __future__ import annotations

from pathlib import Path

import yaml

from mmgu.core.gamepack import GamePack

PACKS = sorted((Path(__file__).resolve().parents[2] / "src" / "mmgu" / "game_packs").glob("*.yaml"))


def _packs():
    assert PACKS, "no game packs found"
    return [GamePack(yaml.safe_load(p.read_text()), str(p)) for p in PACKS]


def test_required_sections_present():
    for g in _packs():
        for key in ("name", "classes", "races", "tradeskills", "items", "event_roles"):
            assert g.raw.get(key), f"{g.source}: missing {key}"


def test_classes_unique_with_valid_roles():
    for g in _packs():
        names = [c["name"] for c in g.classes]
        abbrs = [c["abbr"] for c in g.classes]
        roles = {r["key"] for r in g.event_roles}
        assert len(names) == len(set(names)), "duplicate class name"
        assert len(abbrs) == len(set(abbrs)), "duplicate class abbreviation"
        for c in g.classes:
            assert c.get("role", "any") in roles, f"{c['name']} has unknown role {c.get('role')}"
            assert g.class_from_any(c["abbr"].lower()) == c["name"]


def test_item_vocabulary_is_consistent():
    for g in _packs():
        keys = [s["key"] for s in g.stats + g.resists]
        assert len(keys) == len(set(keys)), "a stat/resist key is defined twice"
        assert len(g.slots) == len(set(g.slots))
        assert all(s == s.upper() for s in g.slots), "slots are upper-case like the inspect window"
        assert len({f["key"] for f in g.flags}) == len(g.flags)
        assert len({s["key"] for s in g.skills}) == len(g.skills)


def test_tradeskills_unique_across_categories():
    for g in _packs():
        all_skills = [s for group in g.tradeskills.values() for s in group]
        assert len(all_skills) == len(set(all_skills)), "a tradeskill is listed in two categories"


def test_wiki_url_template_formats():
    for g in _packs():
        url = g.wiki_url("Rusty Scimitar")
        assert url is None or "Rusty_Scimitar" in url
