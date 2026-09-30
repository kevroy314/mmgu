#!/usr/bin/env python3
"""Hallkeeper Ledger Sync: send your loot and kills from Monsters & Memories' Ledger files to your guild hall.

Runs on your own Windows PC. Needs only Python 3.10+ (nothing to pip install).

    python ledger_sync.py --setup                  # once: hall address and API token
    python ledger_sync.py --dry-run                # show what would be sent, send nothing
    python ledger_sync.py --once --i-understand    # send new events once
    python ledger_sync.py --watch                  # keep sending every 30 seconds while you play

It only READS the game's Ledger files. It never writes to, moves or changes any game file, and it never
reads game memory or network traffic. Its own two small files (settings and what it already sent) live
next to this script.
"""

# ruff: noqa: UP017  (this script supports Python 3.10, which has no datetime.UTC)
from __future__ import annotations

import argparse
import base64
import binascii
import getpass
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = "1.0"
HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "ledger_sync_config.json"
STATE_FILE = HERE / "ledger_sync_state.json"
BATCH_SIZE = 500
MAX_REMEMBERED = 200_000

NOTICE = """\
------------------------------------------------------------------------------------------
 BEFORE YOU USE THIS
------------------------------------------------------------------------------------------
 This script reads the game's own activity log files (the "Ledger") on this computer and
 sends your loot and kills to your guild's Hallkeeper site.

 The Monsters & Memories user agreement forbids software that "intercepts, collects, reads,
 or mines" information the game produces without permission. Reading the Ledger files is
 very likely covered by that, so running this script probably breaks the rules as they
 stand today and could put your account at risk. The developers may change their policy;
 check the current rules yourself.

 What it does: reads Ledger .json files, read-only. What it never does: change game files,
 read game memory, read network traffic, or automate anything in the game.

 Running it is your own choice and your own responsibility, not your guild's.
 To go ahead, run again with --i-understand (it remembers your answer).
------------------------------------------------------------------------------------------
"""

# Event codes documented by the community tool mnm-tools (github.com/Boisteroux/mnm-tools, DATA-COLLECTION.md).
CODES = {"act_13": "loot", "act_14": "kill", "act_24": "vendor_sale", "act_27": "harvest"}

# Internal zone names seen in the Ledger -> display names (from mnm-tools; others are title-cased).
ZONE_NAMES = {
    "evergrove": "Evershade Weald",
    "nightharbor": "Night Harbor",
    "shadeddunes": "Shaded Dunes",
    "wyrmsbanetomb": "Tomb of the Last Wyrmsbane",
}

LEDGER_NAME = re.compile(r"^(?P<char>.+?)_(?P<kind>Character|Social)_[\d-]+\.json$", re.I)


# ----- small helpers ----------------------------------------------------------------------------
def default_game_dir() -> Path:
    home = os.environ.get("USERPROFILE") or str(Path.home())
    return Path(home) / "AppData" / "LocalLow" / "Niche Worlds Cult" / "Monsters and Memories"


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path: Path, data, game_dir: Path | None = None) -> None:
    """Write one of this script's own files. Refuses to write anywhere inside the game folder."""
    if game_dir is not None:
        try:
            path.resolve().relative_to(game_dir.resolve())
        except ValueError:
            pass
        else:
            raise SystemExit(f"Refusing to write {path}: it's inside the game folder. Move this script elsewhere.")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(path)


def b64(s) -> str:
    """Several Ledger fields are base64 text. Returns '' if it isn't."""
    if not s or not isinstance(s, str):
        return ""
    try:
        out = base64.b64decode(s + "=" * (-len(s) % 4), validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return ""
    return out if out.isprintable() else ""


def decode_name(s) -> str:
    """A name field that may be base64, an unresolved 'ref_*' reference, or junk."""
    if not s or not isinstance(s, str) or s.startswith("ref_"):
        return ""
    return b64(s).strip()


def clean_item_name(s) -> str:
    """Newer entries look like '6642399|Encrusted Calafreyan Spear'; drop the numeric id."""
    if not isinstance(s, str):
        return ""
    name = re.sub(r"^\s*\d+\s*\|\s*", "", s).strip()
    return name if name and name.isprintable() else ""


def zone_name(raw) -> str:
    if not isinstance(raw, str) or not raw:
        return ""
    z = b64(raw[5:] if raw.startswith("zone_") else raw)
    if not z:
        return ""
    return ZONE_NAMES.get(z.lower(), z[:1].upper() + z[1:])


_TS = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:[.,](\d+))?\s*(Z|[+-]\d{2}:?\d{2})?$", re.I)


def parse_time(value) -> datetime | None:
    """Ledger timestamps → aware UTC datetime. Times without a zone are treated as this PC's local time."""
    if isinstance(value, (int, float)):
        secs = value / 1000 if value > 1e11 else value  # epoch milliseconds or seconds
        return datetime.fromtimestamp(secs, tz=timezone.utc)
    s = str(value or "").strip()
    if s.isdigit():
        return parse_time(int(s))
    m = _TS.match(s)
    if m:
        y, mo, d, h, mi, se, frac, tz = m.groups()
        micro = int((frac or "0")[:6].ljust(6, "0"))
        dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(se), micro)
        if tz:
            if tz.upper() == "Z":
                return dt.replace(tzinfo=timezone.utc)
            sign = 1 if tz[0] == "+" else -1
            hh, mm = int(tz[1:3]), int(tz[-2:])
            return dt.replace(tzinfo=timezone(sign * timedelta(hours=hh, minutes=mm))).astimezone(timezone.utc)
        return dt.astimezone().astimezone(timezone.utc)
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).astimezone().astimezone(timezone.utc)
        except ValueError:
            continue
    return None


# ----- finding and reading Ledger files -----------------------------------------------------------
def find_ledger_files(game_dir: Path) -> list[Path]:
    """Every <Character>_Character_<date>.json / _Social_ file under the game folder."""
    if not game_dir.is_dir():
        return []
    return sorted(p for p in game_dir.rglob("*.json") if LEDGER_NAME.match(p.name))


def character_and_server(path: Path, game_dir: Path) -> tuple[str, str]:
    """Layout: <game dir>/<server>/<Character>/[Ledger/]<Character>_Character_<date>.json"""
    m = LEDGER_NAME.match(path.name)
    character = m.group("char") if m else path.stem
    parts = path.relative_to(game_dir).parts[:-1]
    server = ""
    if character in parts:
        i = parts.index(character)
        server = parts[i - 1] if i > 0 else ""
    elif parts:
        server = parts[0]
    return character, server


def read_ledger(path: Path) -> list[dict]:
    """The raw event list. Opened read-only; a half-written file is skipped until next time."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    events = data.get("c01") if isinstance(data, dict) else data
    return events if isinstance(events, list) else []


def parse_event(ev: dict, index: int, path: Path) -> dict | None:
    """One raw Ledger event → the hall's shape, or None for event types we don't send."""
    if not isinstance(ev, dict):
        return None
    kind = CODES.get(ev.get("f01"))
    if kind is None:
        return None
    payload = ev.get("f03")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload or "{}")
        except ValueError:
            payload = {}
    if not isinstance(payload, dict):
        payload = {}
    when = parse_time(ev.get("f04"))
    if when is None:
        return None
    raw_id = f"{path.name}#{index}#{ev.get('f04')}#{ev.get('f01')}"
    out = {
        "type": kind,
        "at": when.isoformat().replace("+00:00", "Z"),
        "item": None,
        "creature": None,
        "zone": zone_name(ev.get("f05")) or None,
        "qty": None,
        "raw": {"code": ev.get("f01"), "event_id": hashlib.sha256(raw_id.encode()).hexdigest()[:32]},
    }
    if kind == "loot":
        out["item"] = clean_item_name(payload.get("d04")) or None
        out["creature"] = decode_name(payload.get("d02")) or None
        if payload.get("d05"):
            out["raw"]["item_id"] = str(payload["d05"])[:40]
        if not out["item"]:
            return None
    elif kind == "kill":
        name = decode_name(payload.get("d13"))
        if not name:
            alt = decode_name(str(ev.get("f02") or "").removeprefix("name_"))
            if alt and len(alt) <= 40 and not re.search(r"[,]|corpse|copper|split", alt, re.I):
                name = alt
        if not name or re.fullmatch(r"party[_ ]?split", name, re.I):
            return None
        out["creature"] = name
        coin = b64(payload.get("d12")).split(",")
        if len(coin) == 4 and all(c.strip().isdigit() for c in coin):
            p, g, s, c = (int(x) for x in coin)
            out["raw"]["coin_copper"] = p * 1_000_000 + g * 10_000 + s * 100 + c
    elif kind == "vendor_sale":
        out["item"] = clean_item_name(payload.get("d04") or ev.get("f02")) or None
        qty = payload.get("d01")
        out["qty"] = qty if isinstance(qty, int) and qty > 0 else 1
        price = b64(payload.get("d03"))
        if price:
            out["raw"]["price"] = price[:80]
        if not out["item"]:
            return None
    elif kind == "harvest":
        out["item"] = clean_item_name(payload.get("d04")) or None
        if not out["item"]:
            return None
    return out


def collect(game_dir: Path, since: datetime | None, sent: set[str]) -> dict[tuple[str, str], list[dict]]:
    """New events grouped by (character, server)."""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for path in find_ledger_files(game_dir):
        character, server = character_and_server(path, game_dir)
        for i, ev in enumerate(read_ledger(path)):
            out = parse_event(ev, i, path)
            if out is None or out["raw"]["event_id"] in sent:
                continue
            if since and datetime.fromisoformat(out["at"].replace("Z", "+00:00")) < since:
                continue
            groups[(character, server)].append(out)
    return groups


# ----- talking to the hall -------------------------------------------------------------------------
def post_batch(hall_url: str, token: str, body: dict) -> dict:
    req = urllib.request.Request(
        hall_url.rstrip("/") + "/api/addons/ledger/events",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"hallkeeper-ledger-sync/{VERSION}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get("detail")
        except (ValueError, AttributeError):
            detail = ""
        hints = {
            401: "The hall didn't accept your API token. Make a new one on your profile page and run --setup.",
            403: "Your rank in the hall can't report drops, or the token was limited.",
            404: "The hall doesn't have Ledger Sync turned on (a leader turns it on in the Steward's Office), "
            "or the hall address is wrong.",
        }
        raise RuntimeError(hints.get(e.code) or f"The hall answered {e.code}: {detail}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"Couldn't reach the hall at {hall_url} ({e}).") from e


def settings(args) -> dict:
    cfg = load_json(CONFIG_FILE, {})
    return {
        "hall_url": args.hall or os.environ.get("HALLKEEPER_URL") or cfg.get("hall_url") or "",
        "token": os.environ.get("HALLKEEPER_TOKEN") or cfg.get("token") or "",
        "game_dir": Path(
            args.game_dir or os.environ.get("HALLKEEPER_GAME_DIR") or cfg.get("game_dir") or default_game_dir()
        ),
        "accepted": bool(cfg.get("i_understand")),
    }


def setup() -> None:
    cfg = load_json(CONFIG_FILE, {})
    print("Hallkeeper Ledger Sync setup. Press Enter to keep the value in [brackets].\n")
    url = input(f"Your hall's address (like https://hall.example.org) [{cfg.get('hall_url', '')}]: ").strip()
    token = getpass.getpass("Your API token from your profile page (typing is hidden) [keep]: ").strip()
    game = input(f"Game folder [{cfg.get('game_dir') or default_game_dir()}]: ").strip()
    cfg["hall_url"] = url or cfg.get("hall_url", "")
    cfg["token"] = token or cfg.get("token", "")
    if game:
        cfg["game_dir"] = game
    save_json(CONFIG_FILE, cfg, Path(cfg.get("game_dir") or default_game_dir()))
    print(f"\nSaved to {CONFIG_FILE}. Next: python {Path(__file__).name} --dry-run")


def run_once(args, cfg: dict, state: dict) -> int:
    game_dir: Path = cfg["game_dir"]
    if not game_dir.is_dir():
        print(f"Can't find the game folder: {game_dir}\nSet it with --setup or --game-dir.")
        return 2
    sent_order: list[str] = list(state.get("sent", []))
    sent = set(sent_order)
    since = datetime.now(timezone.utc) - timedelta(days=args.days) if args.days else None
    groups = collect(game_dir, since, sent)
    total = sum(len(v) for v in groups.values())
    if not total:
        print(time.strftime("%H:%M:%S"), "Nothing new in the Ledger.")
        return 0
    for (character, server), events in groups.items():
        kinds = defaultdict(int)
        for e in events:
            kinds[e["type"]] += 1
        summary = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))
        if args.dry_run:
            print(f"Would send for {character} ({server or 'unknown server'}): {summary}")
            for e in events[: args.show]:
                print("   ", json.dumps({k: v for k, v in e.items() if v is not None}))
            if len(events) > args.show:
                print(f"    ... and {len(events) - args.show} more")
            continue
        for i in range(0, len(events), BATCH_SIZE):
            chunk = events[i : i + BATCH_SIZE]
            result = post_batch(
                cfg["hall_url"], cfg["token"], {"character": character, "server": server, "events": chunk}
            )
            new_ids = [e["raw"]["event_id"] for e in chunk]
            sent.update(new_ids)
            sent_order.extend(new_ids)
            state["sent"] = sent_order[-MAX_REMEMBERED:]
            save_json(STATE_FILE, state, game_dir)
            print(
                time.strftime("%H:%M:%S"),
                f"{character}: sent {len(chunk)} ({summary}). Hall recorded {result.get('drops_recorded', 0)} drop(s), "
                f"{result.get('items_proposed', 0)} new item suggestion(s), {result.get('timers_updated', 0)} spawn "
                f"timer(s); {result.get('duplicates', 0)} already known.",
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Send loot and kills from Monsters & Memories' Ledger files to Hallkeeper.")
    p.add_argument("--setup", action="store_true", help="save your hall address, API token and game folder")
    p.add_argument("--once", action="store_true", help="send new events once and exit (the default)")
    p.add_argument("--watch", action="store_true", help="keep checking for new events")
    p.add_argument("--interval", type=int, default=30, help="seconds between checks with --watch (default 30)")
    p.add_argument("--dry-run", action="store_true", help="show what would be sent; send nothing")
    p.add_argument("--show", type=int, default=5, help="with --dry-run, how many events to print per character")
    p.add_argument("--days", type=float, default=7, help="only send events from the last N days (0 = all; default 7)")
    p.add_argument("--hall", help="hall address (overrides the saved one)")
    p.add_argument("--game-dir", help="the Monsters and Memories folder (overrides the saved one)")
    p.add_argument("--i-understand", action="store_true", help="accept the notice about the game's rules")
    p.add_argument(
        "--forget", action="store_true", help="forget what was already sent (the hall still ignores repeats)"
    )
    args = p.parse_args(argv)

    if args.setup:
        setup()
        return 0
    cfg = settings(args)
    if args.i_understand and not cfg["accepted"]:
        stored = load_json(CONFIG_FILE, {})
        stored["i_understand"] = True
        stored["i_understand_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        save_json(CONFIG_FILE, stored, cfg["game_dir"])
        cfg["accepted"] = True
    if not cfg["accepted"]:
        print(NOTICE)
        if not args.dry_run:
            return 1
    if not args.dry_run and (not cfg["hall_url"] or not cfg["token"]):
        print("No hall address or token yet. Run: python ledger_sync.py --setup")
        return 1
    state = {} if args.forget else load_json(STATE_FILE, {})
    print(f"Reading Ledger files under {cfg['game_dir']} (read-only).")
    while True:
        try:
            code = run_once(args, cfg, state)
        except RuntimeError as e:
            print(time.strftime("%H:%M:%S"), "Problem:", e)
            code = 1
        if not args.watch or args.dry_run:
            return code
        try:
            time.sleep(max(5, args.interval))
        except KeyboardInterrupt:
            print("Stopped.")
            return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Stopped.")
        sys.exit(0)
