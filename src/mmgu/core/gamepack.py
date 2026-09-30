"""Loads the game pack YAML (classes, races, stats, tradeskills...) into a convenient object."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml


@dataclass
class GamePack:
    raw: dict[str, Any] = field(default_factory=dict)
    source: str = ""

    @property
    def name(self) -> str:
        return self.raw.get("name", "Unknown game")

    @property
    def level_cap(self) -> int:
        return int(self.raw.get("level_cap", 60))

    @property
    def servers(self) -> list[str]:
        return [s["name"] if isinstance(s, dict) else s for s in self.raw.get("servers", [])]

    @property
    def classes(self) -> list[dict]:
        return self.raw.get("classes", [])

    @property
    def class_names(self) -> list[str]:
        return [c["name"] for c in self.classes]

    def class_abbr(self, name: str | None) -> str:
        for c in self.classes:
            if c["name"] == name:
                return c.get("abbr", name[:3].upper())
        return (name or "")[:3].upper()

    def class_from_any(self, text: str | None) -> str | None:
        """Accept 'Wizard', 'wiz', 'WIZ' and return the canonical class name."""
        if not text:
            return None
        t = text.strip().lower()
        for c in self.classes:
            if t in (c["name"].lower(), c.get("abbr", "").lower()):
                return c["name"]
        return None

    def class_role(self, name: str | None) -> str:
        for c in self.classes:
            if c["name"] == name:
                return c.get("role", "any")
        return "any"

    @property
    def races(self) -> list[str]:
        return self.raw.get("races", [])

    @property
    def event_roles(self) -> list[dict]:
        return self.raw.get("event_roles", [{"key": "any", "label": "Flexible"}])

    @property
    def tradeskills(self) -> dict[str, list[str]]:
        return self.raw.get("tradeskills", {})

    @property
    def tradeskill_names(self) -> list[str]:
        return sorted({s for group in self.tradeskills.values() for s in group})

    @property
    def tradeskill_max(self) -> int:
        return int(self.raw.get("tradeskill_max", 300))

    @property
    def items(self) -> dict[str, Any]:
        return self.raw.get("items", {})

    @property
    def slots(self) -> list[str]:
        return self.items.get("slots", [])

    @property
    def flags(self) -> list[dict]:
        return self.items.get("flags", [])

    @property
    def stats(self) -> list[dict]:
        return self.items.get("stats", [])

    @property
    def resists(self) -> list[dict]:
        return self.items.get("resists", [])

    @property
    def skills(self) -> list[dict]:
        return self.items.get("skills", [])

    @property
    def sizes(self) -> list[str]:
        return self.items.get("sizes", [])

    @property
    def item_types(self) -> list[str]:
        return self.items.get("types", [])

    def skill_label(self, key: str | None) -> str:
        for s in self.skills:
            if s["key"] == key:
                return s["label"]
        return key or ""

    def stat_label(self, key: str) -> str:
        for s in self.stats + self.resists:
            if s["key"] == key:
                return s["label"]
        return key.upper()

    @property
    def zones(self) -> list[str]:
        places = self.raw.get("places", {})
        return list(places.get("cities", [])) + list(places.get("zones", []))

    def wiki_url(self, item_name: str) -> str | None:
        tpl = (self.raw.get("wiki") or {}).get("item_url")
        if not tpl:
            return None
        return tpl.format(name=quote(item_name.replace(" ", "_")))


def load_game_pack(name_or_path: str, data_dir: Path) -> GamePack:
    override = data_dir / "game_pack.yaml"
    bundled = Path(__file__).resolve().parents[1] / "game_packs" / f"{name_or_path}.yaml"
    candidates = [override, Path(name_or_path), bundled]
    for path in candidates:
        if path.is_file():
            return GamePack(yaml.safe_load(path.read_text()) or {}, str(path))
    raise FileNotFoundError(f"game pack not found: {name_or_path}")
