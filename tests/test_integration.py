"""Real HA button setup, registry cleanup and safe setup failures."""

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from conftest import CLOCK, FakeResponse, make_entry
from homeassistant.components import persistent_notification
from homeassistant.exceptions import (
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.gate_link_hass import async_migrate_entry, async_setup_entry
from custom_components.gate_link_hass.account import AccountRuntime, gate_unique_id
from custom_components.gate_link_hass.adapters.app_client import (
    CHECK_PATH,
    COMMUNITIES_PATH,
    OPEN_PATH,
)
from custom_components.gate_link_hass.button import GateLinkHassButton
from custom_components.gate_link_hass.const import DOMAIN, PROTOCOL_APP
from custom_components.gate_link_hass.models import AccountSession, Gate


@pytest.mark.parametrize("selected", [["101:1"], ["101:1", "102:1"]])
async def test_actual_button_setup_and_press(hass, entry, transport, selected):
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            "gates": [
                *entry.data["gates"],
                Gate("102", "测试小区二", "1", "入口", "000000000003").to_dict(),
            ],
        },
        options={**entry.options, "selected_gates": selected},
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    registered = [
        item
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        if item.domain == "button"
    ]
    assert len(registered) == len(selected)
    assert len({item.unique_id for item in registered}) == len(selected)
    for item in registered:
        assert hass.states.get(item.entity_id).attributes["friendly_name"] == "入口"
    assert registered[0].platform == "gate_link_hass"
    entity_id = registered[0].entity_id
    assert registered[0].unique_id == "42:101:1:open"
    assert hass.states.get(entity_id).state != "unavailable"
    transport.calls.clear()
    await hass.services.async_call(
        "button", "press", {"entity_id": entity_id}, blocking=True
    )
    assert len(transport.calls) == 1
    assert transport.calls[0][1].endswith(OPEN_PATH)
    runtime = entry.runtime_data
    runtime.invalidate()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "unavailable"
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not runtime._listeners


def register_gate(hass, entry, gate):
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{entry.unique_id}:{gate.key}")},
        name=gate.label,
    )
    entity = er.async_get(hass).async_get_or_create(
        "button",
        DOMAIN,
        gate_unique_id(entry.unique_id, gate.key),
        config_entry=entry,
        device_id=device.id,
        original_name=gate.label,
    )
    return device, entity


async def test_unbinding_cleans_registry_and_keeps_selected_missing_gate(
    hass, transport
):
    keep = Gate("101", "测试小区一", "1", "入口", "000000000001")
    remove = Gate("102", "测试小区二", "1", "入口", "000000000003")
    entry = make_entry(gates=[keep, remove], selected=[keep.key])
    hass.config_entries._entries[entry.entry_id] = entry
    keep_device, keep_entity = register_gate(hass, entry, keep)
    removed_device, removed_entity = register_gate(hass, entry, remove)
    transport.overrides[COMMUNITIES_PATH] = FakeResponse({"code": 0, "data": []})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert er.async_get(hass).async_get(removed_entity.entity_id) is None
    assert dr.async_get(hass).async_get(removed_device.id) is None
    assert dr.async_get(hass).async_get(keep_device.id) is not None
    assert (
        er.async_get(hass).async_get(keep_entity.entity_id).unique_id == "42:101:1:open"
    )
    assert hass.states.get(keep_entity.entity_id).state != "unavailable"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unbinding_does_not_delete_other_entry_device(hass, entry, monkeypatch):
    other = make_entry(auth=AccountSession("99", "fixture-other", CLOCK, CLOCK + 100))
    hass.config_entries._entries[other.entry_id] = other
    registry = dr.async_get(hass)
    removed = registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "42:102:1")}
    )
    other_device = registry.async_get_or_create(
        config_entry_id=other.entry_id, identifiers={(DOMAIN, "42:102:1")}
    )
    assert removed.id != other_device.id
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())
    assert await async_setup_entry(hass, entry)
    preserved = registry.async_get(other_device.id)
    assert preserved is not None
    assert preserved.config_entry_id == other.entry_id
    assert registry.async_get(removed.id) is None
    await entry._async_process_on_unload(hass)


async def test_credential_refresh_keeps_button_identifiers(hass, entry, client):
    runtime = AccountRuntime(hass, entry, client)
    original = GateLinkHassButton(runtime, runtime.gates["101:1"])
    auth = replace(
        client.auth,
        session_id="fixture-renewed",
        issued_at=CLOCK + 10,
        server_expires_at=CLOCK + 200,
    )
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "auth": auth.to_dict()}
    )
    renewed = AccountRuntime(hass, entry, client)
    button = GateLinkHassButton(renewed, renewed.gates["101:1"])
    assert original.unique_id == button.unique_id == "42:101:1:open"
    assert original.device_info["identifiers"] == button.device_info["identifiers"]
    assert button.device_info["manufacturer"] == "GateLink HASS"
    assert button.available


async def test_button_error_is_translated_and_safe(hass, entry, client, transport):
    runtime = AccountRuntime(hass, entry, client)
    button = GateLinkHassButton(runtime, runtime.gates["101:1"])
    transport.overrides[OPEN_PATH] = FakeResponse({}, error=TimeoutError())
    with pytest.raises(HomeAssistantError) as caught:
        await button.async_press()
    assert caught.value.translation_key == "timeout"
    assert client.auth.session_id not in str(caught.value)


async def test_setup_expired_session_notifies_and_stops(hass, entry, clock, transport):
    clock.now += 30 * 86400
    transport.overrides[CHECK_PATH] = FakeResponse({"code": 401})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.data["invalidated"]
    assert len(transport.calls) == 1 and transport.calls[0][1].endswith(CHECK_PATH)
    persistent_notification.async_create.assert_called_once()
    persistent_notification.async_dismiss.assert_not_called()
    registered = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    for entity in registered:
        assert hass.states.get(entity.entity_id).state == (
            "expired" if entity.domain == "sensor" else "unavailable"
        )
    assert not entry.runtime_data._timer_enabled
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_past_local_deadline_checks_server_and_loads(
    hass, entry, clock, transport
):
    clock.now += 365 * 86400
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert not entry.data["invalidated"]
    assert entry.data["auth"]["checked_at"] == clock.now
    assert entry.data["auth"]["check_validity_seconds"] == 7776000
    persistent_notification.async_create.assert_not_called()
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_malformed_configuration_is_safe(hass, entry, transport):
    hass.config_entries.async_update_entry(entry, data={**entry.data, "auth": {}})
    with pytest.raises(ConfigEntryNotReady):
        await async_setup_entry(hass, entry)
    assert not transport.calls


async def test_setup_malformed_gate_snapshot_is_safe(hass, entry, transport):
    hass.config_entries.async_update_entry(entry, data={**entry.data, "gates": [None]})
    with pytest.raises(ConfigEntryNotReady):
        await async_setup_entry(hass, entry)
    assert not transport.calls


async def test_saving_options_refreshes_cached_permissions(hass, entry, transport):
    transport.overrides[COMMUNITIES_PATH] = FakeResponse({"code": 0, "data": []})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)[0]
    assert not entry.runtime_data.gates["101:1"].permission_valid
    assert hass.states.get(entity.entity_id).state != "unavailable"
    transport.overrides.clear()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        {key: value for key, value in entry.options.items() if key != "lifetime_days"},
    )
    await hass.async_block_till_done()
    assert entry.runtime_data.gates["101:1"].permission_valid
    assert hass.states.get(entity.entity_id).state != "unavailable"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_old_entry_is_rejected_without_http(hass, transport):
    entry = make_entry(version=1)
    hass.config_entries._entries[entry.entry_id] = entry
    assert not await async_migrate_entry(hass, entry)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert not transport.calls


async def test_v2_migration_preserves_binding_and_requires_app_reauth(hass, transport):
    entry = make_entry(version=2)
    hass.config_entries._entries[entry.entry_id] = entry
    original_options = dict(entry.options)
    original_gates = entry.data["gates"]
    device, entity = register_gate(hass, entry, Gate.from_dict(original_gates[0]))
    assert await async_migrate_entry(hass, entry)
    assert entry.version == 3
    assert entry.data["protocol"] == PROTOCOL_APP
    assert entry.data["invalidated"]
    assert entry.options == original_options and entry.data["gates"] == original_gates
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert not transport.calls
    entry.async_start_reauth.assert_called_once_with(hass)
    assert dr.async_get(hass).async_get(device.id) is not None
    assert er.async_get(hass).async_get(entity.entity_id) is not None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_v3_without_app_marker_requires_reauth_before_http(
    hass, entry, transport
):
    data = dict(entry.data)
    data.pop("protocol")
    hass.config_entries.async_update_entry(entry, data=data)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert not transport.calls
    assert entry.data["invalidated"]
    assert await hass.config_entries.async_unload(entry.entry_id)


def test_ui_documents_and_fixture_are_generic():
    root = Path(__file__).resolve().parents[1]
    files = [
        root / "README.md",
        root / "custom_components/gate_link_hass/manifest.json",
        root / "custom_components/gate_link_hass/strings.json",
        *root.glob("custom_components/gate_link_hass/translations/*.json"),
    ]
    forbidden = ("webhook", "demo", "支付宝", "alipay", "lark", "feishu")
    for path in files:
        body = path.read_text().lower()
        assert not any(word in body for word in forbidden), path
        if path.suffix == ".json":
            # README names the supported service; HA keeps GateLink HASS branding.
            assert "qinlin" not in body, path
            json.loads(body)
    fixture = json.loads((root / "tests/fixtures/responses.json").read_text())
    assert fixture["login"]["data"]["aliPayminiLoginDTO"]["sessionId"].startswith(
        "fixture-"
    )
