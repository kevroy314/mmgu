"""Conventions every module must follow so modules can be added and removed independently."""

from __future__ import annotations

from mmgu.core.hall import hall
from mmgu.core.permissions import CORE_PERMISSIONS
from mmgu.db import Base

CORE_PERMS = {p.key for p in CORE_PERMISSIONS}
KNOWN_SLOTS = {
    "item.panels",
    "item.discord_fields",
    "member.panels",
    "hall.cards",
    "search.providers",
    "archive.readers",
    "vault.readers",
}


def test_tables_are_prefixed_with_their_module():
    for name, table in Base.metadata.tables.items():
        owner = table.info.get("module")
        assert owner, f"{name} has no owning module"
        assert name.startswith(f"{owner}_") or (owner == "core" and name.startswith("core_")), (
            f"table {name} belongs to {owner} but isn't prefixed {owner}_"
        )


def test_permissions_are_namespaced_and_nav_points_at_real_ones():
    for key, perm in hall.perms.perms.items():
        if key in CORE_PERMS:
            continue
        assert perm.module in hall.modules, f"{key} declares unknown module {perm.module}"
    for m in hall.modules.values():
        for n in m.nav:
            assert n.permission in hall.perms.perms, f"{m.id} nav uses unknown permission {n.permission}"
            assert n.href.startswith("/")


def test_module_ids_settings_and_channels_are_unique():
    ids = [m.id for m in hall.modules.values()]
    assert len(ids) == len(set(ids))
    for m in hall.modules.values():
        keys = [f.key for f in m.settings]
        assert len(keys) == len(set(keys)), f"{m.id} has duplicate setting keys"
    channels = [k for m in hall.modules.values() for k in m.channels]
    assert len(channels) == len(set(channels)), f"two modules claim the same Discord channel key: {channels}"


def test_addons_are_off_by_default_and_risky_ones_explain_themselves():
    for m in hall.modules.values():
        if m.kind == "addon":
            assert not m.default_enabled or m.tos_risk == "none", f"{m.id} must be off by default"
        if m.tos_risk != "none":
            assert len(m.tos_notice) > 80, f"{m.id} needs a real terms-of-service notice"
            assert m.kind == "addon", f"{m.id}: only add-ons may touch game data (ToS boundary)"


def test_templates_live_in_a_folder_named_after_the_module():
    for m in hall.modules.values():
        if m.templates_dir:
            for f in m.templates_dir.rglob("*.html"):
                rel = f.relative_to(m.templates_dir)
                assert rel.parts[0] == m.id, f"{m.id} template {rel} must live under templates/{m.id}/"


def test_extension_slots_are_known():
    for slot in hall.ext._slots:
        assert slot in KNOWN_SLOTS, f"unknown extension slot {slot}; add it to core/extensions.py docs and here"


def test_modules_requiring_others_name_real_modules():
    for m in hall.modules.values():
        for r in m.requires:
            assert r in hall.modules, f"{m.id} requires missing module {r}"


async def test_admin_urls_resolve(make_client):
    from tests.regression.world import enable_all_modules

    lead = await make_client()
    await enable_all_modules(lead)
    for m in hall.modules.values():
        if m.admin_url:
            assert (await lead.get(m.admin_url)).status_code == 200, m.admin_url
