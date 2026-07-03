"""Tests for plugins/base.py + plugins/registry.py — the plugin foundation."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from uuid import uuid4

import pytest  # noqa: E402
from langchain_core.tools import tool  # noqa: E402

from secretaria.plugins import registry as reg  # noqa: E402
from secretaria.plugins.base import PluginSpec  # noqa: E402
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402

_ALL_ADDONS_OFF = {
    "reactivation_pack": False,
    "verified_identity": False,
    "multi_professional": False,
    "multi_unit": False,
    "ehr": False,
    "pix_whatsapp": False,
    "analytics_bi": False,
    "human_backup_24_7": False,
}


def _summary(**overrides) -> EntitlementSummary:
    base = dict(
        tenant_id=str(uuid4()),
        status="active",
        active=True,
        secretaria_enabled=True,
        plan="bronze",
        secretaria_tier="bronze_1",
        addons=dict(_ALL_ADDONS_OFF),
        limits={},
    )
    base.update(overrides)
    return EntitlementSummary(**base)


@tool
def _fake_pix_tool(x: str) -> str:
    """A fake tool standing in for a pix_whatsapp plugin capability."""
    return x


@tool
def _fake_ehr_tool(x: str) -> str:
    """A fake tool standing in for an ehr plugin capability."""
    return x


@pytest.fixture(autouse=True)
def _clean_registry():
    """Every test starts with an empty REGISTRY, real registrations restored after.

    The real plugin modules (plugins/reminders.py, plugins/human_backup.py)
    register themselves at import time (via plugins/__init__.py), so by the
    time this fixture first runs `reg.REGISTRY` already holds them. Snapshot
    + restore (rather than leaving it empty) so OTHER test modules that rely
    on the real registrations still see them regardless of test ordering.
    """
    original = dict(reg.REGISTRY)
    reg.REGISTRY.clear()
    yield
    reg.REGISTRY.clear()
    reg.REGISTRY.update(original)


def test_empty_registry_yields_nothing():
    summary = _summary(addons={**_ALL_ADDONS_OFF, "pix_whatsapp": True})
    assert reg.enabled_plugins(summary) == []
    assert reg.agent_tools_for(summary) == []


def test_register_is_keyed_by_id_and_idempotent():
    spec = PluginSpec(id="pix_whatsapp", entitlement_keys=("pix_whatsapp",))
    reg.register(spec)
    reg.register(spec)  # re-registering the same id must not duplicate
    assert reg.REGISTRY == {"pix_whatsapp": spec}


def test_enabled_plugins_filters_by_entitlement():
    reg.register(PluginSpec(id="pix_whatsapp", entitlement_keys=("pix_whatsapp",)))
    reg.register(PluginSpec(id="ehr", entitlement_keys=("ehr",)))

    entitled = _summary(addons={**_ALL_ADDONS_OFF, "pix_whatsapp": True})
    enabled = reg.enabled_plugins(entitled)
    assert [spec.id for spec in enabled] == ["pix_whatsapp"]


def test_disabled_plugin_contributes_nothing():
    reg.register(PluginSpec(id="ehr", entitlement_keys=("ehr",), agent_tools=(_fake_ehr_tool,)))
    not_entitled = _summary(addons=dict(_ALL_ADDONS_OFF))
    assert reg.enabled_plugins(not_entitled) == []
    assert reg.agent_tools_for(not_entitled) == []


def test_agent_tools_for_flattens_across_enabled_plugins():
    reg.register(
        PluginSpec(
            id="pix_whatsapp", entitlement_keys=("pix_whatsapp",), agent_tools=(_fake_pix_tool,)
        )
    )
    reg.register(PluginSpec(id="ehr", entitlement_keys=("ehr",), agent_tools=(_fake_ehr_tool,)))

    summary = _summary(addons={**_ALL_ADDONS_OFF, "pix_whatsapp": True, "ehr": True})
    tools = reg.agent_tools_for(summary)
    assert sorted(t.name for t in tools) == sorted([_fake_pix_tool.name, _fake_ehr_tool.name])


def test_tier_gated_plugin():
    reg.register(PluginSpec(id="bronze_2_feature", entitlement_keys=("bronze_2",)))
    bronze_1_tenant = _summary(secretaria_tier="bronze_1")
    bronze_2_tenant = _summary(secretaria_tier="bronze_2")
    assert reg.enabled_plugins(bronze_1_tenant) == []
    assert [s.id for s in reg.enabled_plugins(bronze_2_tenant)] == ["bronze_2_feature"]


def test_inactive_tenant_gets_no_plugins_even_if_addons_true():
    reg.register(PluginSpec(id="pix_whatsapp", entitlement_keys=("pix_whatsapp",)))
    summary = _summary(active=False, addons={**_ALL_ADDONS_OFF, "pix_whatsapp": True})
    assert reg.enabled_plugins(summary) == []


def test_any_of_entitlement_keys():
    """A plugin gated by multiple keys is enabled when ANY of them is entitled
    (bronze_1 tier OR reactivation_pack addon — the reminders plugin's shape)."""
    reg.register(PluginSpec(id="reminders", entitlement_keys=("bronze_1", "reactivation_pack")))

    via_tier = _summary(secretaria_tier="bronze_1", addons=dict(_ALL_ADDONS_OFF))
    via_addon = _summary(
        secretaria_tier="ferro", addons={**_ALL_ADDONS_OFF, "reactivation_pack": True}
    )
    via_higher_tier = _summary(secretaria_tier="bronze_2", addons=dict(_ALL_ADDONS_OFF))
    neither = _summary(secretaria_tier="ferro", addons=dict(_ALL_ADDONS_OFF))

    assert [s.id for s in reg.enabled_plugins(via_tier)] == ["reminders"]
    assert [s.id for s in reg.enabled_plugins(via_addon)] == ["reminders"]
    assert [s.id for s in reg.enabled_plugins(via_higher_tier)] == ["reminders"]
    assert reg.enabled_plugins(neither) == []


async def test_run_on_inbound_short_circuits_on_first_true():
    calls: list[str] = []

    async def _first(ctx):
        calls.append("first")
        return False

    async def _second(ctx):
        calls.append("second")
        return True

    async def _third(ctx):
        calls.append("third")
        return True

    reg.register(PluginSpec(id="first", entitlement_keys=("pix_whatsapp",), on_inbound=_first))
    reg.register(PluginSpec(id="second", entitlement_keys=("ehr",), on_inbound=_second))
    reg.register(PluginSpec(id="third", entitlement_keys=("analytics_bi",), on_inbound=_third))

    summary = _summary(
        addons={**_ALL_ADDONS_OFF, "pix_whatsapp": True, "ehr": True, "analytics_bi": True}
    )
    handled = await reg.run_on_inbound(summary, ctx=object())
    assert handled is True
    assert calls == ["first", "second"]  # third never runs


async def test_run_on_inbound_false_when_nothing_handles():
    reg.register(PluginSpec(id="pix_whatsapp", entitlement_keys=("pix_whatsapp",)))  # no hook
    summary = _summary(addons={**_ALL_ADDONS_OFF, "pix_whatsapp": True})
    assert await reg.run_on_inbound(summary, ctx=object()) is False
