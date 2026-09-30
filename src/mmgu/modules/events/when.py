"""Turn what people type ("tomorrow 8pm", "in 3 hours", "2026-10-04 20:00 UTC") into a UTC datetime.

Assumptions (also shown to people in the /event create modal and on the web form):

- A time zone written in the text wins: ``UTC``/``GMT``/``Z``, an offset like ``+02:00``, an IANA name
  like ``America/Chicago``, or a common abbreviation (EST, CDT, PST, CET, BST, AEST...).
- Otherwise the time is read in the member's profile time zone (``Member.timezone``) when it is set,
  and in UTC when it isn't.
- Numeric dates are year-first (``2026-10-04``) or US month/day (``10/4``). Month names work too
  (``Oct 4``, ``4 October``).
- A bare time ("8pm", "20:00") means the next time the clock shows it: today if it's still ahead, else tomorrow.
- A weekday ("saturday 8pm", "next fri 21:00") means the next such day, never today.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ABBREVIATIONS: dict[str, tzinfo] = {
    "UTC": UTC,
    "GMT": UTC,
    "Z": UTC,
    "EST": timezone(timedelta(hours=-5)),
    "EDT": timezone(timedelta(hours=-4)),
    "CST": timezone(timedelta(hours=-6)),
    "CDT": timezone(timedelta(hours=-5)),
    "MST": timezone(timedelta(hours=-7)),
    "MDT": timezone(timedelta(hours=-6)),
    "PST": timezone(timedelta(hours=-8)),
    "PDT": timezone(timedelta(hours=-7)),
    "AKST": timezone(timedelta(hours=-9)),
    "AKDT": timezone(timedelta(hours=-8)),
    "HST": timezone(timedelta(hours=-10)),
    "BST": timezone(timedelta(hours=1)),
    "CET": timezone(timedelta(hours=1)),
    "CEST": timezone(timedelta(hours=2)),
    "EET": timezone(timedelta(hours=2)),
    "EEST": timezone(timedelta(hours=3)),
    "AEST": timezone(timedelta(hours=10)),
    "AEDT": timezone(timedelta(hours=11)),
    # Loose US shorthands people actually type; these follow daylight saving.
    "ET": ZoneInfo("America/New_York"),
    "CT": ZoneInfo("America/Chicago"),
    "MT": ZoneInfo("America/Denver"),
    "PT": ZoneInfo("America/Los_Angeles"),
}
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
UNITS = {"m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60, "h": 3600, "hr": 3600, "hrs": 3600}
UNITS |= {"hour": 3600, "hours": 3600, "d": 86400, "day": 86400, "days": 86400, "w": 604800, "week": 604800}
UNITS |= {"weeks": 604800}


class WhenError(ValueError):
    pass


def zone(name: str | None) -> tzinfo | None:
    """An IANA zone from a profile or form field, or None when it's empty or not a real zone."""
    if not name or not name.strip():
        return None
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError):
        return None


def to_utc(local: datetime, tz: tzinfo | None) -> datetime:
    """A naive wall-clock time in ``tz`` (UTC when None) → naive UTC."""
    if tz is None:
        return local.replace(tzinfo=None)
    return local.replace(tzinfo=tz).astimezone(UTC).replace(tzinfo=None)


def from_utc(value: datetime, tz: tzinfo | None) -> datetime:
    """Naive UTC → naive wall-clock time in ``tz``."""
    if tz is None:
        return value
    return value.replace(tzinfo=UTC).astimezone(tz).replace(tzinfo=None)


_TZ_TAIL = re.compile(
    r"\s+(?P<tz>[A-Za-z]+/[A-Za-z_/+-]+|[+-]\d{1,2}(?::?\d{2})?|(?:UTC|GMT)\s*[+-]\s*\d{1,2}(?::?\d{2})?|[A-Za-z]{1,5})$"
)
_TIME = re.compile(r"(?:at\s+)?(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm|a|p)?$")


def _offset_tz(text: str) -> tzinfo | None:
    m = re.fullmatch(r"(?:UTC|GMT)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?", text.strip(), re.I)
    if not m:
        return None
    hours, mins = int(m.group(2)), int(m.group(3) or 0)
    if hours > 14 or mins > 59:
        return None
    sign = -1 if m.group(1) == "-" else 1
    return timezone(sign * timedelta(hours=hours, minutes=mins))


def _split_tz(text: str) -> tuple[str, tzinfo | None]:
    m = _TZ_TAIL.search(text)
    if not m:
        return text, None
    token = m.group("tz")
    tz = ABBREVIATIONS.get(token.upper()) or _offset_tz(token)
    if tz is None and "/" in token:
        tz = zone(token)
        if tz is None:
            raise WhenError(f"'{token}' isn't a time zone I know. Try one like America/Chicago or UTC.")
    if tz is None:
        return text, None
    return text[: m.start()].strip(), tz


def _parse_clock(text: str) -> time | None:
    text = text.strip().lower().replace(".", "")
    if text in ("noon", "midday"):
        return time(12, 0)
    if text == "midnight":
        return time(0, 0)
    m = _TIME.fullmatch(text)
    if not m:
        return None
    h, mins, ap = int(m.group("h")), int(m.group("m") or 0), m.group("ap")
    if ap:
        if not 1 <= h <= 12:
            return None
        h = (h % 12) + (12 if ap.startswith("p") else 0)
    elif m.group("m") is None:
        return None  # a lone number like "8" is too ambiguous
    if h > 23 or mins > 59:
        return None
    return time(h, mins)


def _parse_date(text: str, today: date) -> date | None:
    t = text.strip().lower().rstrip(",")
    if t in ("today", "tonight"):
        return today
    if t == "tomorrow":
        return today + timedelta(days=1)
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", t)
    if m:  # US month/day
        year = int(m.group(3)) if m.group(3) else None
        if year is not None and year < 100:
            year += 2000
        return _next_date(today, int(m.group(1)), int(m.group(2)), year)
    m = re.fullmatch(r"(?:next\s+|this\s+|on\s+)?([a-z]{3})[a-z]*", t)
    if m and m.group(1) in WEEKDAYS:
        ahead = (WEEKDAYS.index(m.group(1)) - today.weekday()) % 7 or 7
        return today + timedelta(days=ahead)
    m = re.fullmatch(r"([a-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?", t)
    if m and m.group(1) in MONTHS:
        return _next_date(today, MONTHS.index(m.group(1)) + 1, int(m.group(2)), int(m.group(3) or 0) or None)
    m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3})[a-z]*\.?(?:,?\s+(\d{4}))?", t)
    if m and m.group(2) in MONTHS:
        return _next_date(today, MONTHS.index(m.group(2)) + 1, int(m.group(1)), int(m.group(3) or 0) or None)
    return None


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _next_date(today: date, month: int, day: int, year: int | None) -> date | None:
    if year:
        return _safe_date(year, month, day)
    d = _safe_date(today.year, month, day)
    if d is not None and d < today:
        d = _safe_date(today.year + 1, month, day)
    return d


def parse_when(text: str, *, tz: tzinfo | None = None, now: datetime | None = None) -> datetime:
    """Parse a start time. Returns naive UTC. ``tz`` is the member's zone (None → UTC); ``now`` is naive UTC."""
    raw = re.sub(r"\s+", " ", (text or "").strip())
    raw = re.sub(r"(\d)Z$", r"\1 Z", raw)
    if not raw:
        raise WhenError("Say when: e.g. 'tomorrow 8pm', 'in 3 hours' or '2026-10-04 20:00 UTC'.")
    now = now or datetime.now(UTC).replace(tzinfo=None)

    # "in 3 hours", "in 1h30m", "in 90 minutes"
    m = re.fullmatch(r"in\s+(.+)", raw, re.I)
    if m:
        total = 0
        rest = m.group(1).lower().replace(" and ", " ")
        parts = re.findall(r"(\d+(?:\.\d+)?|an?|half an?)\s*([a-z]+)", rest)
        if not parts or re.sub(r"(\d+(?:\.\d+)?|an?|half an?)\s*([a-z]+)|[\s,]", "", rest):
            raise WhenError(f"I couldn't read '{raw}'. Try 'in 3 hours' or 'in 45 minutes'.")
        for qty, unit in parts:
            n = 0.5 if qty.startswith("half") else 1.0 if qty in ("a", "an") else float(qty)
            if unit not in UNITS:
                raise WhenError(f"'{unit}' isn't a unit I know. Use minutes, hours, days or weeks.")
            total += n * UNITS[unit]
        return (now + timedelta(seconds=total)).replace(second=0, microsecond=0)

    body, explicit = _split_tz(raw)
    zone_used = explicit if explicit is not None else tz
    local_now = from_utc(now, zone_used) if zone_used is not None else now
    body = body.replace("T", " ") if re.match(r"\d{4}-\d{2}-\d{2}T", body) else body

    words = body.split(" ")
    # Try every split of "<date words> <time words>", longest date first.
    for cut in range(len(words), -1, -1):
        date_part, time_part = " ".join(words[:cut]), " ".join(words[cut:])
        day = _parse_date(date_part, local_now.date()) if date_part else None
        if date_part and day is None:
            continue
        if time_part:
            clock = _parse_clock(time_part)
            if clock is None:
                continue
        elif day is not None:
            raise WhenError(f"What time on {day:%a %b %d}? e.g. '{date_part} 8pm'.")
        else:
            continue
        if day is None:
            day = local_now.date()
            if datetime.combine(day, clock) <= local_now:
                day += timedelta(days=1)
        return to_utc(datetime.combine(day, clock), zone_used)
    raise WhenError(
        f"I couldn't read '{raw}'. Try 'tomorrow 8pm', 'saturday 20:00', 'in 3 hours' or '2026-10-04 20:00 UTC'."
    )
