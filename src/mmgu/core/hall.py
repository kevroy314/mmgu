"""The Hall: one object that ties the app together (config, game pack, modules, bus, permissions).

Modules receive it in their ``setup`` function and can also import ``hall`` directly.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from mmgu.config import Settings, get_settings
from mmgu.core import store
from mmgu.core.bus import EventBus
from mmgu.core.extensions import Extensions
from mmgu.core.gamepack import GamePack, load_game_pack
from mmgu.core.modules import Job, Module, discover_modules, import_models
from mmgu.core.permissions import CORE_PERMISSIONS, PermissionRegistry

if TYPE_CHECKING:
    from mmgu.bot.bridge import DiscordBridge

log = logging.getLogger(__name__)


class Hall:
    def __init__(self) -> None:
        self.settings: Settings = get_settings()
        self.bus = EventBus()
        self.ext = Extensions()
        self.perms = PermissionRegistry()
        self.modules: dict[str, Module] = {}
        self.game: GamePack = GamePack()
        self.discord: DiscordBridge | None = None
        self.jobs: list[tuple[str, Job]] = []
        self.booted = False

    # ----- boot -----------------------------------------------------------------------------
    def boot(self, settings: Settings | None = None) -> None:
        if settings is not None:
            self.settings = settings
        self.settings.ensure_dirs()
        self.game = load_game_pack(self.settings.game_pack, self.settings.data_dir)
        for p in CORE_PERMISSIONS:
            self.perms.add(p)
        modules = discover_modules()
        import_models(modules)
        for m in sorted(modules, key=lambda m: (m.kind != "core", m.id)):
            self.modules[m.id] = m
            for p in m.permissions:
                self.perms.add(p)
            for job in m.jobs:
                self.jobs.append((m.id, job))
        from mmgu.core.modules import BROKEN

        for m in list(self.modules.values()):
            if m.id in BROKEN:
                self.modules.pop(m.id)
                continue
            if m.setup:
                try:
                    m.setup(self)
                except Exception as e:  # noqa: BLE001
                    log.exception("setup failed for module %s", m.id)
                    BROKEN[m.id] = repr(e)
                    self.modules.pop(m.id)
        from mmgu.bot.bridge import DiscordBridge

        self.discord = DiscordBridge(self)
        self.booted = True

    # ----- module state ---------------------------------------------------------------------
    def is_enabled(self, module_id: str) -> bool:
        m = self.modules.get(module_id)
        if m is None:
            return False
        if m.kind == "core":
            return True
        state = store.get("modules.enabled", {}) or {}
        enabled = state.get(module_id, m.default_enabled and m.tos_risk == "none")
        if enabled and m.needs_acknowledgement and not store.get(f"modules.ack.{module_id}"):
            return False
        return bool(enabled) and all(self.is_enabled(r) for r in m.requires)

    @property
    def enabled_ids(self) -> set[str]:
        return {mid for mid in self.modules if self.is_enabled(mid)}

    def enabled_modules(self) -> list[Module]:
        return [m for m in self.modules.values() if self.is_enabled(m.id)]

    def setting(self, module_id: str, key: str) -> Any:
        """A module setting from the database, falling back to its declared default."""
        full = f"{module_id}.{key}"
        value = store.get(full)
        if value is not None:
            return value
        m = self.modules.get(module_id)
        if m:
            for f in m.settings:
                if f.key == key:
                    return f.default
        return None

    @property
    def guild_name(self) -> str:
        return store.get("hall.guild_name") or self.settings.guild_name

    @property
    def app_name(self) -> str:
        return store.get("hall.app_name") or self.settings.app_name


hall = Hall()
