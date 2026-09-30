"""The module system. Every feature, including the built-in rooms, is a Module.

A module is a Python package with a ``module.py`` that defines ``MODULE = Module(...)``.
Built-in modules live in ``mmgu/modules``; opt-in add-ons live in ``mmgu/addons``; third-party
packages can register through the ``mmgu.modules`` entry point group.

To add a whole new system after launch: ``mmgu new-module <id>`` scaffolds one.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import logging
import pkgutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from mmgu.core.permissions import Permission

if TYPE_CHECKING:
    from fastapi import APIRouter

    from mmgu.core.hall import Hall

log = logging.getLogger(__name__)

TosRisk = Literal["none", "caution", "high"]


@dataclass
class NavItem:
    label: str  # the in-world name ("The Archive")
    plain: str  # what it actually is ("Item catalog")
    href: str
    icon: str = "scroll"
    permission: str = "hall.view"
    order: int = 100


@dataclass
class SettingField:
    key: str  # stored as "<module>.<key>"
    label: str
    type: Literal["text", "int", "bool", "select", "channel", "secret", "textarea"] = "text"
    default: Any = None
    help: str = ""
    options: list[str] = field(default_factory=list)


@dataclass
class Job:
    name: str
    every_seconds: int
    fn: Callable[[], Awaitable[None]]


@dataclass
class Module:
    id: str
    name: str
    plain_name: str
    description: str
    kind: Literal["core", "feature", "addon"] = "feature"
    requires: list[str] = field(default_factory=list)
    default_enabled: bool = True
    tos_risk: TosRisk = "none"
    tos_notice: str = ""
    nav: list[NavItem] = field(default_factory=list)
    permissions: list[Permission] = field(default_factory=list)
    settings: list[SettingField] = field(default_factory=list)
    jobs: list[Job] = field(default_factory=list)
    # Discord channels this module posts to; the leader picks the channel in settings.
    channels: dict[str, str] = field(default_factory=dict)
    router: Callable[[], APIRouter] | None = None
    setup: Callable[[Hall], None] | None = None
    bot_setup: Callable[[Any], Awaitable[None] | None] | None = None
    package_dir: Path | None = None
    version: str = "0.1.0"
    admin_url: str | None = None  # settings/setup page, linked from the Steward's Office

    @property
    def templates_dir(self) -> Path | None:
        if self.package_dir and (self.package_dir / "templates").is_dir():
            return self.package_dir / "templates"
        return None

    @property
    def static_dir(self) -> Path | None:
        if self.package_dir and (self.package_dir / "static").is_dir():
            return self.package_dir / "static"
        return None

    @property
    def stylesheets(self) -> list[str]:
        """URLs of the module's own CSS files; the base template loads them on every page."""
        if not self.static_dir:
            return []
        return [f"/static/m/{self.id}/{p.name}" for p in sorted(self.static_dir.glob("*.css"))]

    @property
    def needs_acknowledgement(self) -> bool:
        return self.tos_risk != "none"


BROKEN: dict[str, str] = {}  # module package -> error, shown in the Steward's Office


def _load_package_modules(package: str) -> list[Module]:
    found: list[Module] = []
    pkg = importlib.import_module(package)
    for info in pkgutil.iter_modules(pkg.__path__):
        if not info.ispkg:
            continue
        try:
            mod = importlib.import_module(f"{package}.{info.name}.module")
        except ModuleNotFoundError as e:
            if e.name == f"{package}.{info.name}.module":
                continue
            log.exception("module %s failed to load", info.name)
            BROKEN[info.name] = repr(e)
            continue
        except Exception as e:  # noqa: BLE001 - one broken module must not take down the hall
            log.exception("module %s failed to load", info.name)
            BROKEN[info.name] = repr(e)
            continue
        module: Module = mod.MODULE
        module.package_dir = Path(mod.__file__).parent
        found.append(module)
    return found


def discover_modules() -> list[Module]:
    modules = _load_package_modules("mmgu.modules") + _load_package_modules("mmgu.addons")
    for ep in importlib.metadata.entry_points(group="mmgu.modules"):
        try:
            module = ep.load()
            if not module.package_dir:
                module.package_dir = Path(importlib.import_module(ep.module).__file__).parent
            modules.append(module)
        except Exception:  # noqa: BLE001
            log.exception("could not load third-party module %s", ep.name)
    ids = [m.id for m in modules]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise RuntimeError(f"duplicate module ids: {dupes}")
    return modules


def import_models(modules: list[Module]) -> None:
    """Import each module's models so their tables join the shared metadata."""
    importlib.import_module("mmgu.core.models")
    for m in modules:
        if m.package_dir and (m.package_dir / "models.py").exists():
            pkg = m.package_dir.name
            parent = m.package_dir.parent.name
            if parent in ("modules", "addons"):
                try:
                    importlib.import_module(f"mmgu.{parent}.{pkg}.models")
                except Exception as e:  # noqa: BLE001
                    log.exception("models for module %s failed to load", m.id)
                    BROKEN[m.id] = repr(e)
