"""Read item and bank screenshots with a vision model (a local Ollama model, or Claude).

The reader only produces a loose dict of fields. ``archive.services.clean_fields`` normalises it
against the game pack when a person saves the item, and nothing is saved until they check it.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from PIL import Image

from mmgu.core.hall import hall
from mmgu.modules.archive.reading import ReaderResult

log = logging.getLogger(__name__)

MODULE_ID = "screenshot_reader"
OLLAMA_TIMEOUT = 180.0  # the first request loads the model into GPU memory, which is slow
MAX_SIDE = 1600  # bigger images cost a lot more tokens (and GPU memory) and rarely read better


class ReaderError(RuntimeError):
    """Something a leader can fix (wrong URL, model missing, no API key)."""


@dataclass
class RawRead:
    """What the model said, before we turn it into a ReaderResult. Shown on the test page."""

    data: dict[str, Any]
    source: str
    seconds: float
    raw: str = ""
    notes: list[str] = field(default_factory=list)


# ----- settings --------------------------------------------------------------------------------
def setting(key: str) -> Any:
    return hall.setting(MODULE_ID, key)


def provider() -> str:
    return (setting("provider") or "ollama").strip().lower()


def ollama_url() -> str:
    return (setting("ollama_url") or os.environ.get("MMGU_OLLAMA_URL") or "http://localhost:11434").rstrip("/")


def ollama_model() -> str:
    return setting("ollama_model") or "qwen3-vl:8b"


def anthropic_model() -> str:
    return setting("anthropic_model") or "claude-haiku-4-5"


def anthropic_key() -> str:
    return setting("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")


# ----- schema and prompt, built from the game pack ---------------------------------------------
def _nullable(kind: str) -> dict[str, Any]:
    return {"type": [kind, "null"]}


def item_schema() -> dict[str, Any]:
    g = hall.game
    # Every property is required (null when absent): models given optional fields tend to skip them.
    stat_props = {s["key"]: {**_nullable("integer"), "description": s["label"]} for s in g.stats}
    resist_props = {s["key"]: {**_nullable("integer"), "description": s["label"]} for s in g.resists}
    slot_item: dict[str, Any] = {"type": "string"}
    if g.slots:
        slot_item["enum"] = list(g.slots)
    # raw_text comes first: the model transcribes the window, then fills the fields from its own transcript.
    props: dict[str, Any] = {
        "raw_text": {"type": "string", "description": "Every line of text in the window, top to bottom"},
        "name": {"type": "string", "description": "The item's name, the title line of the window"},
        "item_type": {"type": "string", "enum": [*g.item_types, ""]} if g.item_types else {"type": "string"},
        "slots": {"type": "array", "items": slot_item},
        "flags": {"type": "array", "items": {"type": "string"}},
        "classes": {"type": "array", "items": {"type": "string"}},
        "races": {"type": "array", "items": {"type": "string"}},
        "skill": _nullable("string"),
        "damage": _nullable("integer"),
        "delay": _nullable("integer"),
        "ac": _nullable("integer"),
        "weight": _nullable("number"),
        "size": _nullable("string"),
        "stats": {"type": "object", "properties": stat_props, "required": list(stat_props)},
        "resists": {"type": "object", "properties": resist_props, "required": list(resist_props)},
        "effects": {"type": "array", "items": {"type": "string"}},
    }
    return {"type": "object", "properties": props, "required": list(props)}


def item_prompt() -> str:
    g = hall.game
    classes = ", ".join(f"{c.get('abbr', c['name'][:3].upper())} = {c['name']}" for c in g.classes)
    stats = ", ".join(f"{s['label']} -> {s['key']}" for s in g.stats)
    resists = ", ".join(f"{s['label']} -> {s['key']}" for s in g.resists)
    skills = ", ".join(f"{s['label']} ({s['key']})" for s in g.skills)
    flags = ", ".join(f["label"] for f in g.flags)
    return (
        f"This is a screenshot from {g.name}, an EverQuest-style MMO. It shows an item inspect window: "
        "a dark panel with the item's name at the top, then lines such as flags (e.g. MAGIC ITEM, LORE ITEM), "
        "'Slot: ...', 'Skill: ... Atk Delay: ...', 'DMG: ...', 'AC: ...', stat bonuses like 'STR: +2', "
        "resists like 'SV Fire: +5', effects, 'WT: ... Size: ...', 'Class: ...' and 'Race: ...'.\n"
        "Read the window exactly and fill in the JSON. Rules:\n"
        "- Copy only what is written. Never guess a value that is not visible; leave it out or use null.\n"
        f"- flags: the flag words as shown. Known flags: {flags}.\n"
        f"- slots: from the 'Slot:' line. Allowed: {' '.join(g.slots)}.\n"
        f"- skill: the weapon skill as shown. Known skills: {skills}.\n"
        f"- stats: use these keys: {stats}. Numbers only, negative if shown with a minus.\n"
        f"- resists: use these keys: {resists}.\n"
        f"- classes: the class abbreviations from the 'Class:' line, exactly as shown ({classes}), "
        'or ["ALL"] if it says ALL.\n'
        "- races: the race abbreviations from the 'Race:' line as shown, or [\"ALL\"].\n"
        f"- size: one of {', '.join(g.sizes)}. weight: the WT number.\n"
        "- item_type: your best category for the item (weapons, armor, jewelry...), or empty.\n"
        "- raw_text (fill this first): every line of text in the window, one per line, exactly as written.\n"
        "If there is no item window in the image, return an empty name."
    )


def bank_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "qty": {"type": "integer"}},
                    "required": ["name", "qty"],
                },
            }
        },
        "required": ["items"],
    }


def bank_prompt() -> str:
    return (
        f"This is a screenshot from {hall.game.name}, an EverQuest-style MMO, showing a bank or bag window, "
        "or a list of items. List every item you can identify by name, with its stack count (1 if no number is "
        "shown). Only include names you can actually read (from labels, tooltips or a text list); do not guess "
        "from icons alone. Return JSON."
    )


# ----- image prep --------------------------------------------------------------------------------
def prepare_image(image: bytes) -> bytes:
    """Re-encode as PNG and shrink very large screenshots. Raises ReaderError for non-images."""
    try:
        img = Image.open(io.BytesIO(image))
        img.load()
    except Exception as e:  # noqa: BLE001
        raise ReaderError("That file isn't an image we can read. Use PNG, JPG or WebP.") from e
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    if max(img.size) > MAX_SIDE:
        img.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _parse_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    # Some models wrap JSON in a code fence or add thinking text; take the outermost object.
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ReaderError("The model didn't return JSON. Try another model or the test page to see its output.")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise ReaderError(f"The model returned broken JSON ({e.msg}).") from e
    if not isinstance(data, dict):
        raise ReaderError("The model returned JSON, but not an object.")
    return data


# ----- providers -----------------------------------------------------------------------------------
def _client(**kw: Any) -> httpx.AsyncClient:
    """One place to build the HTTP client, so tests can swap in a mock transport."""
    return httpx.AsyncClient(**kw)


_capabilities: dict[tuple[str, str], set[str]] = {}


async def ollama_capabilities(client: httpx.AsyncClient, model: str) -> set[str]:
    """What the model can do (vision, thinking...), from /api/show. Cached; empty if Ollama won't say."""
    key = (ollama_url(), model)
    if key not in _capabilities:
        try:
            r = await client.post(f"{ollama_url()}/api/show", json={"model": model})
            caps = set(r.json().get("capabilities") or []) if r.status_code == 200 else set()
        except (httpx.HTTPError, ValueError):
            caps = set()
        _capabilities[key] = caps
    return _capabilities[key]


async def ollama_chat(image: bytes, prompt: str, schema: dict[str, Any]) -> RawRead:
    model = ollama_model()
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt, "images": [base64.b64encode(image).decode()]}]
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "format": schema,
        "stream": False,
        # Room for a large screenshot (~2,500 image tokens at 1600 px) plus the prompt and answer.
        "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 3000},
        "keep_alive": "15m",
    }
    started = time.monotonic()
    try:
        async with _client(timeout=OLLAMA_TIMEOUT) as client:
            if "thinking" in await ollama_capabilities(client, model) and not setting("ollama_think"):
                # Reading a window doesn't need a chain of thought, and thinking models spend minutes on it
                # on a home GPU. Some (qwen3-vl on Ollama 0.35) ignore think=false, so we also start the
                # answer with an empty think block, which they then skip.
                body["think"] = False
                messages.append({"role": "assistant", "content": "<think>\n\n</think>\n\n"})
            r = await client.post(f"{ollama_url()}/api/chat", json=body)
    except httpx.TimeoutException as e:
        raise ReaderError(f"Ollama took longer than {OLLAMA_TIMEOUT:.0f} s. Is the GPU busy?") from e
    except httpx.HTTPError as e:
        raise ReaderError(f"Couldn't reach Ollama at {ollama_url()} ({e.__class__.__name__}).") from e
    if r.status_code == 404:
        raise ReaderError(f"Ollama doesn't have the model {model}. Run: ollama pull {model}")
    if r.status_code != 200:
        raise ReaderError(f"Ollama answered {r.status_code}: {r.text[:200]}")
    content = (r.json().get("message") or {}).get("content", "")
    return RawRead(_parse_json(content), f"ollama:{model}", time.monotonic() - started, raw=content)


def _anthropic_client(api_key: str):
    import anthropic

    return anthropic.AsyncAnthropic(api_key=api_key, timeout=120.0)


async def claude_tool_call(image: bytes, prompt: str, schema: dict[str, Any], tool_name: str) -> RawRead:
    key = anthropic_key()
    if not key:
        raise ReaderError("No Anthropic API key. Add one in the add-on's settings or set ANTHROPIC_API_KEY.")
    model = anthropic_model()
    started = time.monotonic()
    client = _anthropic_client(key)
    try:
        msg = await client.messages.create(
            model=model,
            max_tokens=2048,
            tools=[{"name": tool_name, "description": "Record what the screenshot shows.", "input_schema": schema}],
            tool_choice={"type": "tool", "name": tool_name},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": base64.b64encode(image).decode(),
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        )
    except Exception as e:  # noqa: BLE001 - the SDK raises many error types; all mean "didn't work"
        raise ReaderError(f"Claude request failed: {e.__class__.__name__}: {str(e)[:200]}") from e
    for block in msg.content:
        if getattr(block, "type", "") == "tool_use":
            data = dict(block.input or {})
            return RawRead(data, f"claude:{model}", time.monotonic() - started, raw=json.dumps(data))
    raise ReaderError("Claude answered without filling in the item.")


async def read_raw(image: bytes, kind: str = "item") -> RawRead:
    image = prepare_image(image)
    if kind == "bank":
        prompt, schema, tool = bank_prompt(), bank_schema(), "record_items"
    else:
        prompt, schema, tool = item_prompt(), item_schema(), "record_item"
    if provider() == "anthropic":
        return await claude_tool_call(image, prompt, schema, tool)
    return await ollama_chat(image, prompt, schema)


# ----- turning the model's answer into a ReaderResult ----------------------------------------------
def _tokens(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        v = re.split(r"[,\s/]+", v)
    return [str(x).strip() for x in v if str(x).strip()]


def item_fields(model_output: dict[str, Any]) -> dict[str, Any]:
    data = clean_model_output(model_output)
    return clean_model_output(merge_fields(data, fields_from_text(str(data.get("raw_text") or ""))))


def item_notes(data: dict[str, Any]) -> list[str]:
    g = hall.game
    notes = []
    if not str(data.get("name") or "").strip():
        notes.append("No item name found. Is the whole inspect window in the screenshot?")
    unknown = [c for c in _tokens(data.get("classes")) if c.upper() != "ALL" and not g.class_from_any(c)]
    if unknown:
        notes.append(f"Class names not in the game pack: {', '.join(unknown)}. Check the Class line.")
    if g.slots:
        bad = [s for s in _tokens(data.get("slots")) if s.upper() not in g.slots]
        if bad:
            notes.append(f"Slots not in the game pack: {', '.join(bad)}.")
    stat_keys = {s["key"] for s in g.stats}
    odd = [k for k in (data.get("stats") or {}) if k not in stat_keys]
    if odd:
        notes.append(f"Unrecognised stats kept as extra attributes: {', '.join(odd)}.")
    if not data.get("raw_text"):
        notes.append("The reader returned no raw text, so there is nothing to double-check against.")
    return notes


def clean_model_output(data: dict[str, Any]) -> dict[str, Any]:
    """Tidy the model's dict without judging it: drop nulls, zero stats and empty strings."""
    out: dict[str, Any] = {}
    for k, v in data.items():
        if v is None or v == "" or v == [] or v == {}:
            continue
        if k in ("stats", "resists") and isinstance(v, dict):
            v = {sk: sv for sk, sv in v.items() if sv not in (None, 0, "", "0")}
            if not v:
                continue
        out[k] = v
    out.setdefault("name", "")
    if isinstance(out.get("raw_text"), list):
        out["raw_text"] = "\n".join(str(x) for x in out["raw_text"])
    return out


def fields_from_text(text: str) -> dict[str, Any]:
    """Parse inspect-window text ("Slot: PRIMARY", "STR: +2"...) into fields.

    Small vision models transcribe the window reliably but sometimes leave structured fields empty,
    so we fill the gaps from their own transcript.
    """
    g = hall.game
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    out: dict[str, Any] = {"stats": {}, "resists": {}, "effects": []}
    if not lines:
        return out
    body = "\n".join(lines)
    first = lines[0]
    if ":" not in first and not any(f["label"].upper() in first.upper() for f in g.flags):
        out["name"] = first
    out["flags"] = [f["label"] for f in g.flags if re.search(rf"\b{re.escape(f['label'])}\b", body, re.I)]

    def after(label: str, stop: str = r"(?=\s+[A-Za-z][A-Za-z ]*:|$)") -> str | None:
        m = re.search(rf"\b{label}:\s*(.+?){stop}", body, re.I | re.M)
        return m.group(1).strip() if m else None

    slot = after("Slot", r"$")
    if slot:
        out["slots"] = [t for t in slot.upper().split() if not g.slots or t in g.slots]
    skill = after("Skill", r"(?=\s+Atk Delay:|$)")
    if skill:
        out["skill"] = skill
    for key, label in (("delay", r"Atk Delay"), ("damage", r"DMG"), ("ac", r"AC")):
        m = re.search(rf"\b{label}:\s*([+-]?\d+)", body, re.I)
        if m:
            out[key] = int(m.group(1))
    m = re.search(r"\bWT:\s*(\d+(?:\.\d+)?)", body, re.I)
    if m:
        out["weight"] = float(m.group(1))
    m = re.search(r"\bSize:\s*([A-Za-z]+)", body, re.I)
    if m:
        out["size"] = m.group(1).upper()
    labelled = [(s, "stats") for s in g.stats] + [(s, "resists") for s in g.resists]
    rest = body
    for s, bucket in sorted(labelled, key=lambda x: -len(x[0]["label"])):  # "HP Regen" before "HP"
        pat = rf"(?<![A-Za-z]){re.escape(s['label'])}:\s*([+-]?\d+)"
        m = re.search(pat, rest, re.I)
        if m:
            out[bucket][s["key"]] = int(m.group(1))
            rest = rest[: m.start()] + rest[m.end() :]
    for key, label in (("classes", "Class"), ("races", "Race")):
        v = after(label, r"$")
        if v:
            out[key] = ["ALL"] if v.strip().upper() == "ALL" else v.split()
    out["effects"] = [m.group(1).strip() for m in re.finditer(r"^Effect:\s*(.+)$", body, re.I | re.M)]
    return out


def merge_fields(model: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    """The model's values win; parsed values fill whatever it left empty."""
    out = dict(model)
    for k, v in parsed.items():
        if v in (None, "", [], {}):
            continue
        cur = out.get(k)
        if isinstance(v, dict):
            out[k] = {**v, **(cur if isinstance(cur, dict) else {})}
        elif cur in (None, "", [], {}):
            out[k] = v
    return out


async def read_item(image: bytes) -> ReaderResult | None:
    """The ``archive.readers`` entry point."""
    raw = await read_raw(image, "item")
    data = item_fields(raw.data)
    notes = item_notes(data)
    confidence = None if data.get("name") else 0.0
    return ReaderResult(
        fields=data,
        source=raw.source,
        raw_text=str(data.get("raw_text") or ""),
        confidence=confidence,
        notes=notes,
    )


async def read_bank(image: bytes) -> list[dict] | None:
    """The ``vault.readers`` entry point: ``[{"name": str, "qty": int}]`` or None when nothing was read."""
    raw = await read_raw(image, "bank")
    out: list[dict] = []
    for row in raw.data.get("items") or []:
        if not isinstance(row, dict):
            continue
        name = re.sub(r"\s+", " ", str(row.get("name") or "")).strip()
        if not name:
            continue
        try:
            qty = max(1, int(row.get("qty") or 1))
        except (TypeError, ValueError):
            qty = 1
        out.append({"name": name[:120], "qty": qty})
    return out or None


async def list_ollama_models() -> list[dict[str, Any]]:
    try:
        async with _client(timeout=10.0) as client:
            r = await client.get(f"{ollama_url()}/api/tags")
    except httpx.HTTPError as e:
        raise ReaderError(f"Couldn't reach Ollama at {ollama_url()} ({e.__class__.__name__}).") from e
    if r.status_code != 200:
        raise ReaderError(f"Ollama answered {r.status_code}.")
    return [
        {"name": m.get("name"), "size_gb": round((m.get("size") or 0) / 1e9, 1)} for m in r.json().get("models") or []
    ]
