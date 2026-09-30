"""Roles and permissions.

Ranks are ordered (a higher rank can do everything a lower rank can). Duties are extra hats a
member can wear on top of their rank (Banker, Crafter, Streamer). Each permission names the
lowest rank that has it plus any duties that also grant it. Leaders can override any
permission from the Steward's Office; overrides live in the ``permissions.overrides`` setting.
"""

from __future__ import annotations

from dataclasses import dataclass, field

RANKS: list[tuple[str, str]] = [
    ("guest", "Guest"),
    ("recruit", "Recruit"),
    ("member", "Member"),
    ("officer", "Officer"),
    ("leader", "Leader"),
]
RANK_ORDER = {key: i for i, (key, _) in enumerate(RANKS)}
RANK_LABEL = dict(RANKS)

DUTIES: list[tuple[str, str, str]] = [
    ("banker", "Banker", "Keeps the guild's bank characters and approves withdrawals."),
    ("crafter", "Crafter", "Takes crafting orders from the Notice Board."),
    ("streamer", "Streamer", "Can push cards and alerts to the stream overlay."),
    ("raidlead", "Raid Leader", "Runs events, takes attendance and awards loot."),
]
DUTY_LABEL = {k: label for k, label, _ in DUTIES}
ALL_ROLES = [k for k, _ in RANKS] + [k for k, _, _ in DUTIES]


@dataclass(frozen=True)
class Permission:
    key: str
    label: str
    min_rank: str = "member"
    duties: tuple[str, ...] = ()
    module: str = "core"


@dataclass
class PermissionRegistry:
    perms: dict[str, Permission] = field(default_factory=dict)

    def add(self, perm: Permission) -> None:
        if perm.min_rank not in RANK_ORDER:
            raise ValueError(f"{perm.key}: unknown rank {perm.min_rank}")
        self.perms[perm.key] = perm

    def rule(self, key: str, overrides: dict | None = None) -> tuple[str, tuple[str, ...]]:
        perm = self.perms.get(key)
        if perm is None:
            # Unknown permissions are leader-only rather than silently open.
            return "leader", ()
        o = (overrides or {}).get(key) or {}
        return o.get("min_rank", perm.min_rank), tuple(o.get("duties", perm.duties))

    def allowed(self, key: str, rank: str, duties: set[str], overrides: dict | None = None) -> bool:
        min_rank, extra = self.rule(key, overrides)
        if RANK_ORDER.get(rank, 0) >= RANK_ORDER[min_rank]:
            return True
        return bool(duties.intersection(extra))

    def grants(self, rank: str, duties: set[str], overrides: dict | None = None) -> set[str]:
        return {k for k in self.perms if self.allowed(k, rank, duties, overrides)}


CORE_PERMISSIONS = [
    Permission("hall.view", "Enter the hall (see the dashboard)", "recruit"),
    Permission("members.view", "See the member list", "recruit"),
    Permission("members.manage", "Change members' ranks and duties", "officer"),
    Permission("members.notes", "Read and write officer notes", "officer"),
    Permission("characters.edit_any", "Edit anyone's characters", "officer"),
    Permission("audit.view", "Read the audit log", "officer"),
    Permission("admin.settings", "Change hall settings and channels", "leader"),
    Permission("admin.modules", "Turn modules and add-ons on or off", "leader"),
    Permission("admin.permissions", "Change who can do what", "leader"),
    Permission("proposals.review", "Approve or reject machine-suggested changes", "officer"),
    Permission("api.tokens", "Create personal API tokens", "member"),
]


def best_rank(roles: set[str], default: str) -> str:
    ranks = [r for r in roles if r in RANK_ORDER]
    if not ranks:
        return default
    return max(ranks, key=lambda r: RANK_ORDER[r])
