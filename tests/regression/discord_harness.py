"""A stand-in for Discord: registers every module's commands and drives them like a user would.

No network. Interactions record what the bot said, which modal it opened and which buttons it attached.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import discord
from discord import app_commands

from mmgu.core.hall import hall
from tests.regression.world import png


@dataclass
class Said:
    content: str | None
    embed: Any = None
    view: Any = None
    ephemeral: bool | None = None

    @property
    def text(self) -> str:
        parts = [self.content or ""]
        if self.embed is not None:
            parts += [self.embed.title or "", self.embed.description or ""]
            parts += [f"{f.name} {f.value}" for f in self.embed.fields]
        return "\n".join(parts)

    def custom_ids(self) -> list[str]:
        if self.view is None:
            return []
        return [getattr(c, "custom_id", None) for c in self.view.children if getattr(c, "custom_id", None)]


class FakeResponse:
    def __init__(self, inter: FakeInteraction):
        self.inter = inter
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, content=None, *, embed=None, view=None, ephemeral=None, **_):
        self._done = True
        self.inter.said.append(Said(content, embed, view, ephemeral))

    async def defer(self, *_, **__):
        self._done = True

    async def edit_message(self, *, content=None, embed=None, view=None, **_):
        self._done = True
        self.inter.said.append(Said(content, embed, view, None))
        self.inter.edited = True

    async def send_modal(self, modal):
        self._done = True
        self.inter.modal = modal


class FakeAttachment:
    def __init__(self, data: bytes, content_type: str = "image/png", filename: str = "shot.png"):
        self._data, self.content_type, self.filename = data, content_type, filename

    async def read(self) -> bytes:
        return self._data


class FakeInteraction:
    def __init__(self, user: SimpleNamespace, data: dict | None = None, namespace: dict | None = None):
        self.user = user
        self.data = data or {}
        self.namespace = SimpleNamespace(**(namespace or {}))
        self.channel_id = 777
        self.guild_id = 888
        self.said: list[Said] = []
        self.modal = None
        self.edited = False
        self.response = FakeResponse(self)
        self.followup = SimpleNamespace(send=self._followup)
        self.message = SimpleNamespace(id=555, channel=SimpleNamespace(id=777), embeds=[], edit=self._noop)
        self.type = discord.InteractionType.application_command

    async def _followup(self, content=None, *, embed=None, view=None, ephemeral=None, **_):
        self.said.append(Said(content, embed, view, ephemeral))

    async def _noop(self, **_):
        return None

    async def original_response(self):
        return self.message

    @property
    def replied(self) -> bool:
        return bool(self.said) or self.modal is not None


class FakeBot:
    """Collects commands and button handlers exactly like HallBot does."""

    guild_obj = None

    def __init__(self):
        self.commands: list[Any] = []
        self.handlers: dict[str, Any] = {}

    def add_command(self, cmd):
        self.commands.append(cmd)

    def on_component(self, key):
        def deco(fn):
            self.handlers[key] = fn
            return fn

        return deco

    async def load(self) -> FakeBot:
        import inspect

        from mmgu.bot import core_commands

        await core_commands.setup(self)
        for m in hall.enabled_modules():
            if m.bot_setup:
                res = m.bot_setup(self)
                if inspect.isawaitable(res):
                    await res
        return self

    def all_commands(self):
        def walk(cmds):
            for c in cmds:
                if isinstance(c, app_commands.Group):
                    yield from walk(c.commands)
                else:
                    yield c

        return list(walk(self.commands))

    def command(self, qualified: str):
        for c in self.all_commands():
            if c.qualified_name == qualified:
                return c
        raise KeyError(qualified)


def user(uid: int, name: str) -> SimpleNamespace:
    return SimpleNamespace(id=uid, name=name, display_name=name, display_avatar=None, mention=f"<@{uid}>")


async def run(cmd, inter: FakeInteraction, **kwargs) -> FakeInteraction:
    if isinstance(cmd, app_commands.ContextMenu):
        await cmd.callback(inter, kwargs["target"])
    else:
        await cmd.callback(inter, **kwargs)
    return inter


async def press(bot: FakeBot, custom_id: str, inter: FakeInteraction) -> FakeInteraction:
    parts = custom_id.split(":")
    await bot.handlers[":".join(parts[1:3])](inter, parts[3:])
    return inter


def modal_data(modal, values: dict[str, str] | None = None) -> dict:
    """Fill a modal's text inputs (by label substring, else first-come) and build submit data."""
    values = values or {}
    comps = []
    for child in modal.children:
        val = ""
        for k, v in values.items():
            if k.lower() in (child.label or "").lower():
                val = v
        comps.append({"components": [{"custom_id": child.custom_id, "value": val}]})
    return {"custom_id": modal.custom_id, "components": comps}


def sample_args(cmd: app_commands.Command, flavour: str = "plausible") -> dict[str, Any]:
    """Arguments for any command, from its declared option types."""
    out: dict[str, Any] = {}
    for py_name, p in cmd._params.items():
        t = p.type
        if p.choices:
            out[py_name] = p.choices[0]
        elif t is discord.AppCommandOptionType.string:
            out[py_name] = {"plausible": "Rusty Scimitar", "junk": "‽" * 300}[flavour]
        elif t is discord.AppCommandOptionType.integer:
            out[py_name] = {"plausible": 1, "junk": -99999}[flavour]
        elif t is discord.AppCommandOptionType.number:
            out[py_name] = 1.0
        elif t is discord.AppCommandOptionType.boolean:
            out[py_name] = False
        elif t is discord.AppCommandOptionType.attachment:
            out[py_name] = FakeAttachment(png() if flavour == "plausible" else b"not an image")
        else:  # user, member, channel, role, mentionable
            if p.required:
                out[py_name] = None
    return out


@dataclass
class Stub:
    calls: list = field(default_factory=list)
