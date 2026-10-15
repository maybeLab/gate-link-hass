"""Exercise Home Assistant flow managers with an entirely offline transport."""

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlsplit

import pytest
from conftest import CLOCK, PHONE, RESPONSES, FakeResponse, make_entry
from homeassistant.components import persistent_notification
from homeassistant.data_entry_flow import FlowResultType

from custom_components.gate_link_hass import async_migrate_entry
from custom_components.gate_link_hass.adapters.app_client import (
    CHECK_PATH,
    COMMUNITIES_PATH,
    LOGIN_PATH,
    SMS_PATH,
    QinlinAppClient,
)
from custom_components.gate_link_hass.adapters.mini_app_client import default_sms_signer
from custom_components.gate_link_hass.config_flow import selected_options
from custom_components.gate_link_hass.const import (
    CLIENT_FACTORY,
    DOMAIN,
    PROTOCOL_APP,
)
from custom_components.gate_link_hass.models import AccountSession, ApiError, Gate


@pytest.fixture(autouse=True)
def skip_entry_setup(hass, monkeypatch):
    # Test flows through HA itself; component setup is covered separately.
    monkeypatch.setattr(
        hass.config_entries, "async_setup", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", Mock())


async def start_login(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"phone": PHONE}
    )
    assert result["step_id"] == "verification"
    return result


async def start_selection(hass):
    result = await start_login(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"verification_code": "012345"}
    )
    assert result["step_id"] == "gates"
    return result


async def start_reauth(hass, entry):
    return await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": "reauth",
            "entry_id": entry.entry_id,
            "unique_id": entry.unique_id,
        },
        data=dict(entry.data),
    )


async def test_login_cross_community_selection(hass, transport):
    result = await start_selection(hass)
    schema = result["data_schema"]
    selector = next(
        value for key, value in schema.schema.items() if str(key) == "selected_gates"
    )
    assert {opt["value"]: opt["label"] for opt in selector.config["options"]} == {
        "101:1": "测试小区一 / 入口",
        "101:2": "测试小区一 / 常用门",
        "102:1": "测试小区二 / 入口",
    }
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "selected_gates": ["101:2", "102:1"],
            "timeout": 12,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.version == 3 and entry.unique_id == "42"
    assert entry.data["protocol"] == PROTOCOL_APP
    assert entry.data["device_id"]
    assert entry.options == {
        "selected_gates": ["101:2", "102:1"],
        "timeout": 12,
    }
    assert entry.data["auth"]["issued_at"] == CLOCK
    assert entry.data["auth"]["check_validity_seconds"] == 7776000
    assert "lifetime_days" not in {str(key) for key in schema.schema}
    assert "verification_code" not in entry.data
    assert "012345" not in str(entry.data)
    login_request = next(
        c for c in transport.calls if urlsplit(c[1]).path == LOGIN_PATH
    )
    assert json.loads(login_request[2]["data"])["smsCode"] == "012345"
    assert "code" not in json.loads(login_request[2]["data"])
    assert all("/open" not in c[1] for c in transport.calls)


async def test_validation_and_resend(hass, transport):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}, data={"phone": "123"}
    )
    assert result["errors"] == {"phone": "invalid_phone"}
    assert not transport.calls
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"phone": PHONE}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"verification_code": "123"}
    )
    assert result["errors"] == {"verification_code": "invalid_verification_code"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"resend": True}
    )
    assert result["step_id"] == "verification" and not result["errors"]
    assert [urlsplit(c[1]).path for c in transport.calls] == [SMS_PATH, SMS_PATH]
    transport.overrides[LOGIN_PATH] = FakeResponse({"code": 8, "message": "wrong code"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"verification_code": "012345"}
    )
    assert result["errors"] == {"base": "verification_failed"}


async def test_default_signer_sends_signed_sms_and_shows_verification(hass, transport):
    hass.data[DOMAIN].pop(CLIENT_FACTORY)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}, data={"phone": PHONE}
    )
    assert result["step_id"] == "verification"
    assert not result["errors"]
    assert len(transport.calls) == 1
    request = transport.calls[0][2]
    headers = request["headers"]
    assert len(headers["nonce"]) == 4 and headers["nonce"].isdigit()
    assert headers["qversioncode"] == "3146"
    assert headers["sign"] == default_sms_signer(
        request["data"], headers["timestamp"], headers["nonce"]
    )


async def test_disabled_signer_fails_without_request(hass, transport):
    hass.data[DOMAIN][CLIENT_FACTORY] = lambda _session, **kwargs: QinlinAppClient(
        transport, sms_signer=None, **kwargs
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}, data={"phone": PHONE}
    )
    assert result["errors"] == {"base": "signing_unavailable"}
    assert not transport.calls


async def test_duplicate_account(hass, entry):
    result = await start_login(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"verification_code": "012345"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert hass.config_entries.async_entries(DOMAIN) == [entry]


@pytest.mark.parametrize("denied", [False, True])
async def test_no_available_gates(hass, transport, denied):
    response = deepcopy(RESPONSES["communities"])
    if denied:
        for community in response["data"]:
            community["hasPermission"] = False
    else:
        response["data"] = []
    transport.overrides[COMMUNITIES_PATH] = FakeResponse(response)
    result = await start_login(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"verification_code": "012345"}
    )
    assert result["reason"] == "no_gates"


async def test_catalog_failure_retries_without_new_login(hass, transport):
    transport.overrides[COMMUNITIES_PATH] = FakeResponse({}, error=TimeoutError())
    result = await start_selection(hass)
    assert result["errors"] == {"base": "timeout"}
    transport.overrides.clear()
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "gates" and not result["errors"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"selected_gates": ["101:1"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert sum(urlsplit(c[1]).path == LOGIN_PATH for c in transport.calls) == 1


@pytest.mark.parametrize("selection", [[]])
async def test_selection_validation(hass, selection):
    result = await start_selection(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"selected_gates": selection}
    )
    assert result["errors"] == {"base": "select_gates"}


def test_unknown_gate_selection_is_rejected():
    with pytest.raises(ApiError, match="select_gates"):
        selected_options({"selected_gates": ["missing"]}, {}, CLOCK)


@pytest.mark.parametrize("rejected", [False, True])
async def test_selection_checks_server_after_old_deadline(
    hass, clock, transport, rejected
):
    result = await start_selection(hass)
    clock.now += 30 * 86400
    if rejected:
        transport.overrides[CHECK_PATH] = FakeResponse({"code": 401})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"selected_gates": ["101:1"]}
    )
    if rejected:
        assert result["errors"] == {"base": "account_expired"}
        assert not hass.config_entries.async_entries(DOMAIN)
    else:
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["result"].data["auth"]["checked_at"] == clock.now


async def test_reauth_preserves_identity_binding_and_missing_gate(
    hass, entry, clock, transport
):
    original_options = dict(entry.options)
    original_id = entry.entry_id
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "invalidated": True}
    )
    clock.now += 86400
    transport.overrides[COMMUNITIES_PATH] = FakeResponse({"code": 0, "data": []})
    result = await start_reauth(hass, entry)
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"phone": PHONE}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"verification_code": "012345"}
    )
    assert result["reason"] == "reauth_successful"
    assert entry.entry_id == original_id and entry.unique_id == "42"
    assert entry.options == original_options
    assert entry.data["auth"]["issued_at"] == clock.now
    assert not entry.data["invalidated"]
    assert entry.data["gates"][0]["permission_valid"] is False
    hass.config_entries.async_schedule_reload.assert_called_once_with(original_id)
    persistent_notification.async_dismiss.assert_called_once()


async def test_reauth_wrong_account_does_not_replace_credentials(
    hass, entry, transport
):
    original_data = dict(entry.data)
    response = deepcopy(RESPONSES["login"])
    response["data"]["userId"] = 99
    transport.overrides[LOGIN_PATH] = FakeResponse(response)
    result = await start_reauth(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"phone": PHONE}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"verification_code": "012345"}
    )
    assert result["reason"] == "wrong_account"
    assert entry.data == original_data
    hass.config_entries.async_schedule_reload.assert_not_called()


async def test_v2_migration_can_reauthenticate_with_app_and_keep_bindings(hass):
    entry = make_entry(version=2)
    hass.config_entries._entries[entry.entry_id] = entry
    original_options = dict(entry.options)
    original_id = entry.entry_id
    assert await async_migrate_entry(hass, entry)
    result = await start_reauth(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"phone": PHONE}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"verification_code": "012345"}
    )
    assert result["reason"] == "reauth_successful"
    assert entry.version == 3 and entry.entry_id == original_id
    assert entry.options == original_options and entry.unique_id == "42"
    assert entry.data["protocol"] == PROTOCOL_APP
    assert entry.data["device_id"] and not entry.data["invalidated"]


async def test_options_preserve_missing_gate_then_unbind(hass, entry, transport):
    # Community 101 is gone; its selected gate must remain selectable for unbinding.
    response = deepcopy(RESPONSES["communities"])
    response["data"] = response["data"][1:]
    transport.overrides[COMMUNITIES_PATH] = FakeResponse(response)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    assert result["description_placeholders"]["community_count"] == "1"
    assert result["description_placeholders"]["gate_count"] == "1"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "selected_gates": ["102:1"],
            "timeout": 3,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["selected_gates"] == ["102:1"]
    assert "lifetime_days" not in entry.options
    hass.config_entries.async_schedule_reload.assert_called_once_with(entry.entry_id)


@pytest.mark.parametrize("invalidated", [False, True])
async def test_options_expired_account_requests_reauth_without_http(
    hass, clock, transport, invalidated
):
    entry = make_entry(invalidated=invalidated)
    hass.config_entries._entries[entry.entry_id] = entry
    if not invalidated:
        clock.now += 30 * 86400
        transport.overrides[CHECK_PATH] = FakeResponse({"code": 401})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["reason"] == "reauth_required"
    assert len(transport.calls) == (0 if invalidated else 1)
    entry.async_start_reauth.assert_called_once_with(hass)


async def test_malformed_stored_session_gives_translated_error(hass, entry):
    hass.config_entries.async_update_entry(entry, data={**entry.data, "auth": {}})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_configuration"}
    assert result["description_placeholders"]["issued_at"] == "Not recorded"
    assert result["description_placeholders"]["checked_at"] == "Not recorded"


@pytest.mark.parametrize(
    ("language", "time_zone", "timestamp", "status"),
    [
        ("en", "UTC", "2027-01-15 08:00:00+00:00", "Check passed on this visit"),
        ("zh-Hans", "Asia/Shanghai", "2027-01-15 16:00:00+08:00", "本次检查通过"),
    ],
)
async def test_options_account_summary_is_localized_and_hides_credentials(
    hass, transport, language, time_zone, timestamp, status
):
    hass.config.language = language
    await hass.config.async_set_time_zone(time_zone)
    gates = [
        Gate("101", "测试小区一", "1", "入口", "000000000001"),
        Gate("101", "测试小区一", "2", "常用门", "000000000002"),
        Gate("102", "测试小区二", "1", "入口", "000000000003"),
    ]
    entry = make_entry(gates=gates, selected=["101:1", "102:1"])
    hass.config_entries._entries[entry.entry_id] = entry
    result = await hass.config_entries.options.async_init(entry.entry_id)
    placeholders = result["description_placeholders"]
    assert placeholders == {
        "account": "****0000",
        "user_id": "42",
        "login_status": status,
        "issued_at": timestamp,
        "checked_at": timestamp,
        "community_count": "2",
        "gate_count": "2",
        "time_zone": time_zone,
    }
    translations = Path(__file__).resolve().parents[1] / (
        f"custom_components/gate_link_hass/translations/{language}.json"
    )
    description = json.loads(translations.read_text())["options"]["step"]["init"][
        "description"
    ].format_map(placeholders)
    assert PHONE not in description
    assert entry.data["auth"]["session_id"] not in description
    assert timestamp in description and status in description
    assert sum(urlsplit(call[1]).path == CHECK_PATH for call in transport.calls) == 1
    assert all(
        "/open" not in call[1] and "/sms/" not in call[1] for call in transport.calls
    )


@pytest.mark.parametrize("checked_before", [False, True])
async def test_options_summary_check_failure_does_not_claim_validity(
    hass, entry, transport, checked_before
):
    if checked_before:
        auth = replace(
            AccountSession.from_dict(dict(entry.data["auth"])),
            checked_at=CLOCK - 60,
            check_validity_seconds=7776000,
        )
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "auth": auth.to_dict()}
        )
    original_data = dict(entry.data)
    transport.overrides[CHECK_PATH] = FakeResponse({}, error=TimeoutError())
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["errors"] == {"base": "timeout"}
    summary = result["description_placeholders"]
    assert summary["login_status"] == "Not confirmed on this visit"
    assert summary["checked_at"] == (
        "2027-01-15 07:59:00+00:00" if checked_before else "Not recorded"
    )
    assert {
        key: value for key, value in entry.data.items() if key != "login_check"
    } == original_data
    assert entry.data["login_check"]["result"] == "error"
    assert entry.data["login_check"]["error"] == "timeout"
    transport.overrides.clear()
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert not result["errors"]
    assert (
        result["description_placeholders"]["login_status"]
        == "Check passed on this visit"
    )


async def test_options_saving_unchanged_selection_reloads_once(hass, entry):
    # Legacy options are ignored and removed on the next save.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {key: value for key, value in entry.options.items() if key != "lifetime_days"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    hass.config_entries.async_schedule_reload.assert_called_once_with(entry.entry_id)
    assert "lifetime_days" not in entry.options


async def test_options_past_local_deadline_remain_usable(hass, entry, clock):
    clock.now += 365 * 86400
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init" and not result["errors"]
    assert entry.data["auth"]["checked_at"] == clock.now
    assert entry.data["auth"]["check_validity_seconds"] == 7776000
    entry.async_start_reauth.assert_not_called()


async def test_options_discovery_failure_can_retry(hass, entry, transport):
    transport.overrides[COMMUNITIES_PATH] = FakeResponse({}, error=TimeoutError())
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["errors"] == {"base": "timeout"}
    transport.overrides.clear()
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["step_id"] == "init" and not result["errors"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {key: value for key, value in entry.options.items() if key != "lifetime_days"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_reauth_cannot_restore_session_rejected_during_discovery(
    hass, entry, clock, transport
):
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "invalidated": True}
    )

    def response(_):
        clock.now += 30 * 86400
        return FakeResponse({"code": 401})

    transport.overrides[COMMUNITIES_PATH] = response
    result = await start_reauth(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"phone": PHONE}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"verification_code": "012345"}
    )
    assert result["errors"] == {"base": "account_expired"}
    assert entry.data["invalidated"]
    persistent_notification.async_dismiss.assert_not_called()
    hass.config_entries.async_schedule_reload.assert_not_called()
