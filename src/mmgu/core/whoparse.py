"""Parse pasted /who output into (name, level, class, race, guild).

The exact Monsters & Memories /who format isn't documented yet, so this accepts the EverQuest
shape and a few loose variants. Unrecognised lines are returned separately so the person pasting
can see what was skipped. Adjust ``PATTERNS`` when the real format is known.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from mmgu.core.gamepack import GamePack

_TS = r"(?:\[[A-Z][a-z]{2} [A-Z][a-z]{2} +\d+ [\d:]+ \d{4}\]\s*)?"  # EQ log timestamp
PATTERNS = [
    # [60 Wizard] Name (High Elf) <Guild> ZONE: somewhere
    re.compile(
        _TS + r"\[\s*(?P<level>\d{1,3})\s+(?P<cls>[A-Za-z][A-Za-z ]*?)\s*\]\s+(?P<name>[A-Z][A-Za-z'`-]{1,30})"
        r"(?:\s+\((?P<race>[^)]+)\))?(?:\s+<(?P<guild>[^>]+)>)?"
    ),
    # [ANONYMOUS] Name <Guild>
    re.compile(_TS + r"\[\s*ANONYMOUS\s*\]\s+(?P<name>[A-Z][A-Za-z'`-]{1,30})(?:\s+<(?P<guild>[^>]+)>)?"),
    # Name - 60 Wizard   |   Name 60 Wizard   |   Name (60 Wizard)
    re.compile(
        r"^\s*(?P<name>[A-Z][A-Za-z'`-]{1,30})\s*[-:(]?\s*(?P<level>\d{1,3})\s+(?P<cls>[A-Za-z][A-Za-z ]+?)\)?\s*$"
    ),
]


@dataclass
class WhoLine:
    name: str
    level: int | None = None
    class_name: str | None = None
    race: str | None = None
    guild: str | None = None
    raw: str = ""


def parse_who(text: str, game: GamePack) -> tuple[list[WhoLine], list[str]]:
    found: dict[str, WhoLine] = {}
    skipped: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.lower().startswith(("players on", "there are", "there is", "---", "total")):
            continue
        for pat in PATTERNS:
            m = pat.search(line)
            if not m:
                continue
            g = m.groupdict()
            cls = game.class_from_any(g.get("cls")) if g.get("cls") else None
            if g.get("cls") and cls is None and pat is PATTERNS[2]:
                continue  # "Name 60 Something" where Something isn't a class: probably not a who line
            level = int(g["level"]) if g.get("level") else None
            found[g["name"]] = WhoLine(
                g["name"], level, cls or (g.get("cls") or None), (g.get("race") or None), (g.get("guild") or None), raw
            )
            break
        else:
            skipped.append(line)
    return list(found.values()), skipped
