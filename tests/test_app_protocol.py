"""Verify App protocol compatibility and injection into the real HA flow."""

import asyncio
import json
from copy import deepcopy
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest
from aiohttp import ClientConnectionError
from conftest import CLOCK, PHONE, RESPONSES, FakeResponse
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from custom_components.gate_link_hass.adapters.app_client import (
    CHECK_PATH,
    COMMUNITIES_PATH,
    GATES_PATH,
    LOGIN_PATH,
    OPEN_PATH,
    SMS_PATH,
    QinlinAppClient,
    encrypt_phone,
)
from custom_components.gate_link_hass.adapters.mini_app_client import GateLinkHassClient
from custom_components.gate_link_hass.const import CLIENT_FACTORY, DOMAIN
from custom_components.gate_link_hass.models import AccountSession, ApiError, Gate

APP_GATE_RESPONSES = {
    "101": {
        "code": 0,
        "data": {
            "buildingDoorList": [{"doorControlId": 1, "doorControlName": "楼栋门"}],
            "customDoorList": [{"doorControlId": 2, "doorControlName": "自定义门"}],
            "gateDoorList": [
                {"doorControlId": 3, "doorControlName": "小区门"},
                {"doorControlId": 1, "doorControlName": "楼栋门"},
            ],
        },
    },
    "102": {
        "code": 0,
        "data": {
            "buildingDoorList": [],
            "customDoorList": [],
            "gateDoorList": [{"doorControlId": 1, "doorControlName": "入口"}],
        },
    },
}


class AppSession:
    def __init__(self):
        self.calls = []
        self.overrides = {}
        self.started = asyncio.Event()

    def request(self, method, url, **kwargs):
        path = urlsplit(url).path
        self.calls.append((method, url, kwargs))
        self.started.set()
        if path in self.overrides:
            return self.overrides[path]
        if path == LOGIN_PATH:
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "sessionId": "app:fixture",
                        "userId": 42,
                        "expires": 2592000,
                    },
                }
            )
        if path == GATES_PATH:
            fields = {part[0]["name"]: part[2] for part in kwargs["data"]._fields}
            return FakeResponse(APP_GATE_RESPONSES[fields["communityId"]])
        if path == CHECK_PATH:
            return FakeResponse({"code": 0, "data": 7776000})
        name = {
            SMS_PATH: "sms",
            COMMUNITIES_PATH: "communities",
            OPEN_PATH: "open",
        }[path]
        return FakeResponse(RESPONSES[name])


@pytest.fixture
def app_transport():
    return AppSession()


@pytest.fixture
def app_client(app_transport, clock):
    client = QinlinAppClient(
        app_transport, clock=clock, nonce_factory=lambda size: "12345"[:size]
    )
    client.auth = AccountSession("42", "app:fixture", CLOCK, CLOCK + 2592000)
    return client


def test_app_sign_and_phone_encryption_vectors(app_client):
    # Fixed protocol vectors with the published key and canonical open fields.
    assert encrypt_phone(PHONE) == "CC40EC23839A080382482E2EEAB652D1"
    fields = app_client.signed_fields(
        {"appChannel": 1, "communityId": 101, "doorControlId": 2}, "app:fixture"
    )
    assert fields["sign"] == "7472D204616B575355BBEA455E68496E"
    assert "token" not in fields


async def test_sms_and_login_wire_format(app_client, app_transport):
    await app_client.send_sms(PHONE)
    sms = app_transport.calls[0]
    assert sms[1] == "https://gateway2.qinlinkeji.com" + SMS_PATH
    assert sms[2]["params"] == {}
    assert json.loads(sms[2]["data"]) == {"mobile": PHONE}
    assert sms[2]["headers"]["nonce"] == "1234"
    assert sms[2]["headers"]["qversioncode"] == "3146"
    assert "Referer" not in sms[2]["headers"]
    auth = await app_client.login(PHONE, "012345")
    request = app_transport.calls[-1][2]
    body = json.loads(request["data"])
    assert body["mobile"] == encrypt_phone(PHONE)
    assert body["smsCode"] == "012345" and body["appChannel"] == 1
    assert body["version"] == "5.2.6" and len(body["sign"]) == 32
    assert request["params"] == {"sessionId": ""}
    assert auth.account_id == "42"
    assert auth.server_expires_at == CLOCK + 2592000
    assert PHONE not in request["data"]


async def test_discovery_multipart_and_single_empty_body_open(
    app_client, app_transport
):
    gates = await app_client.discover_gates()
    assert set(gates) == {"101:1", "101:2", "101:3", "102:1"}
    assert {gate.name for gate in gates.values()} == {
        "楼栋门",
        "自定义门",
        "小区门",
        "入口",
    }
    community_request = app_transport.calls[0][2]
    assert json.loads(community_request["data"])["method"] == "communityInfo"
    for call in app_transport.calls[1:]:
        assert urlsplit(call[1]).path == "/api/app/user/v2/queryAllUserDoorByCache"
        assert call[2]["params"] == {"sessionId": "app:fixture"}
        assert call[2]["data"].is_multipart
        assert "Content-Type" not in call[2]["headers"]
    auth = app_client.auth
    await app_client.check_login()
    assert app_client.auth.session_id == auth.session_id
    assert app_client.auth.check_validity_seconds == 7776000
    app_transport.calls.clear()
    await app_client.open_door(gates["101:2"])
    assert len(app_transport.calls) == 1
    request = app_transport.calls[0][2]
    assert request["data"] == b"" and request["allow_redirects"] is False
    assert request["params"]["sign"] == "7472D204616B575355BBEA455E68496E"
    assert request["params"]["communityId"] == 101
    assert request["params"]["doorControlId"] == 2
    assert "macAddress" not in request["params"]


async def test_missing_server_expiry_is_preserved_as_unknown(app_client, app_transport):
    app_transport.overrides[LOGIN_PATH] = FakeResponse(
        {"code": 0, "data": '{"sessionId":"app:fixture","userId":42}'}
    )
    auth = await app_client.login(PHONE, "012345")
    assert auth.server_expires_at is None
    assert auth.deadline(5) == CLOCK + 5 * 86400
    assert AccountSession.from_dict(auth.to_dict()) == auth


@pytest.mark.parametrize("server_expires_at", [None, CLOCK + 2592000])
async def test_app_check_login_saves_timing_reference_without_replacing_token(
    app_client, app_transport, server_expires_at
):
    auth = AccountSession("42", "app:fixture", CLOCK, server_expires_at)
    app_client.auth = auth
    deadline = auth.deadline(app_client.lifetime_days)
    await app_client.check_login()
    assert len(app_transport.calls) == 1
    method, url, request = app_transport.calls[0]
    assert method == "POST"
    assert url == "https://mobileapi3.qinlinkeji.com/api/app/v1/checkLogin"
    assert request["params"] == {"sessionId": "app:fixture"}
    assert request["headers"]["Content-Type"] == "application/json; charset=utf-8"
    assert json.loads(request["data"]) == app_client.signed_fields(
        {"method": "checkLogin"}, "app:fixture"
    )
    assert request["allow_redirects"] is False
    assert app_client.auth.checked_at == CLOCK
    assert app_client.auth.check_validity_seconds == 7776000
    assert app_client.auth.next_check_at == CLOCK + 7776000
    assert app_client.auth.issued_at == auth.issued_at
    assert app_client.auth.session_id == auth.session_id
    assert app_client.auth.server_expires_at == server_expires_at
    assert AccountSession.from_dict(app_client.auth.to_dict()) == app_client.auth
    assert auth.deadline(app_client.lifetime_days) == deadline


@pytest.mark.parametrize(
    "response,key",
    [
        (FakeResponse({"code": 0, "data": 0}), "account_expired"),
        (FakeResponse({"code": 0, "data": -1}), "account_expired"),
        (FakeResponse({"code": 401, "success": False}), "account_expired"),
        (
            FakeResponse({"code": 401, "success": True, "data": 7776000}),
            "account_expired",
        ),
        (FakeResponse({}, status=401), "account_expired"),
        (FakeResponse({"code": 0, "data": None}), "invalid_response"),
        (FakeResponse({"code": 0, "data": True}), "invalid_response"),
        (FakeResponse({"code": 0, "data": "7776000"}), "invalid_response"),
        (FakeResponse({"code": 0, "data": 7776000.0}), "invalid_response"),
        (FakeResponse({"code": 0, "data": {}}), "invalid_response"),
        (FakeResponse({"code": 0, "data": 10**100}), "invalid_response"),
        (FakeResponse({}, error=TimeoutError()), "timeout"),
        (
            FakeResponse({}, error=ClientConnectionError("private URL")),
            "cannot_connect",
        ),
    ],
)
async def test_app_check_login_rejects_expired_or_invalid_responses(
    app_client, app_transport, response, key
):
    app_transport.overrides[CHECK_PATH] = response
    auth = app_client.auth
    with pytest.raises(ApiError) as caught:
        await app_client.check_login()
    assert caught.value.key == key
    assert "private" not in str(caught.value)
    assert app_client.auth is auth
    assert len(app_transport.calls) == 1


async def test_app_check_login_calls_server_after_old_local_deadline(
    app_client, app_transport
):
    now = app_client.auth.deadline(app_client.lifetime_days) + 365 * 86400
    app_client.clock = lambda: now
    await app_client.check_login()
    assert len(app_transport.calls) == 1
    assert app_client.auth.next_check_at == now + 7776000
    await app_client.get_communities()
    assert app_client.require_auth().session_id == "app:fixture"


@pytest.mark.parametrize(
    "metadata",
    [
        {"checked_at": CLOCK},
        {"check_validity_seconds": 86400},
        {"checked_at": CLOCK, "check_validity_seconds": True},
        {"checked_at": CLOCK, "check_validity_seconds": -1},
        {"checked_at": CLOCK, "check_validity_seconds": "86400"},
    ],
)
def test_malformed_cached_check_reference_is_rejected(app_client, metadata):
    data = {**app_client.auth.to_dict(), **metadata}
    with pytest.raises(ApiError, match="invalid_response"):
        AccountSession.from_dict(data)


async def test_login_user_id_is_resolved_from_community_response(
    app_client, app_transport
):
    app_transport.overrides[LOGIN_PATH] = FakeResponse(
        {"code": 0, "data": {"flag": 0, "sessionId": "app:fixture"}}
    )
    communities = deepcopy(RESPONSES["communities"])
    communities["data"][0]["userId"] = 42
    app_transport.overrides[COMMUNITIES_PATH] = FakeResponse(communities)
    app_client.auth = None
    auth = await app_client.login(PHONE, "012345")
    assert auth.account_id == "42" and auth.server_expires_at is None
    assert app_transport.calls[-1][2]["params"] == {"sessionId": "app:fixture"}


@pytest.mark.parametrize(
    "response,key",
    [
        (FakeResponse({}, status=401), "account_expired"),
        (FakeResponse({"code": 9, "message": "session expired"}), "account_expired"),
        (FakeResponse({"code": 401, "message": "private session"}), "account_expired"),
        (FakeResponse({}, status=403), "permission_denied"),
        (FakeResponse({}, status=307), "http_error"),
        (FakeResponse({"code": 0, "data": {"openDoorState": 0}}), "open_failed"),
        (FakeResponse({"code": 200, "data": {"openDoorState": 1}}), "request_rejected"),
        (FakeResponse({}, error=TimeoutError()), "timeout"),
        (
            FakeResponse({}, error=ClientConnectionError("private URL")),
            "cannot_connect",
        ),
    ],
)
async def test_failed_open_is_safe_and_not_retried(
    app_client, app_transport, response, key
):
    app_transport.overrides[OPEN_PATH] = response
    with pytest.raises(ApiError) as caught:
        await app_client.open_door(Gate("101", "小区", "1", "门", ""))
    assert caught.value.key == key
    assert "private" not in str(caught.value)
    assert len(app_transport.calls) == 1


async def test_cached_revoked_permission_does_not_block_app_open(
    app_client, app_transport
):
    communities = deepcopy(RESPONSES["communities"])
    communities["data"][0]["hasPermission"] = False
    app_transport.overrides[COMMUNITIES_PATH] = FakeResponse(communities)
    gates = await app_client.discover_gates()
    assert not gates["101:1"].available(CLOCK)
    assert gates["102:1"].available(CLOCK)
    app_transport.calls.clear()
    app_transport.overrides[OPEN_PATH] = FakeResponse({}, status=403)
    with pytest.raises(ApiError, match="permission_denied"):
        await app_client.open_door(gates["101:1"])
    assert len(app_transport.calls) == 1
    assert app_transport.calls[0][1].endswith(OPEN_PATH)


async def test_cached_expiry_does_not_block_app_open(app_client, app_transport):
    await app_client.open_door(
        Gate("101", "小区", "1", "门", "", permission_expires_at=CLOCK - 1)
    )
    assert len(app_transport.calls) == 1
    assert app_transport.calls[0][1].endswith(OPEN_PATH)


async def test_default_app_transport_in_ha_config_flow_without_auth_code(
    hass, app_transport, monkeypatch
):
    # Simulate observed login data and full-door categories with fictional data.
    app_transport.overrides[LOGIN_PATH] = FakeResponse(
        {"code": 0, "data": {"flag": 0, "sessionId": "app:fixture"}}
    )
    communities = deepcopy(RESPONSES["communities"])
    communities["data"][0]["userId"] = 42
    app_transport.overrides[COMMUNITIES_PATH] = FakeResponse(communities)
    doors = deepcopy(APP_GATE_RESPONSES["101"])
    doors["data"]["authorizeExpired"] = 1
    app_transport.overrides[GATES_PATH] = FakeResponse(doors)

    hass.data[DOMAIN].pop(CLIENT_FACTORY)
    monkeypatch.setattr(
        "custom_components.gate_link_hass.account.async_get_clientsession",
        lambda _: app_transport,
    )
    auth_code = AsyncMock(side_effect=AssertionError("App must not request auth code"))
    monkeypatch.setattr(GateLinkHassClient, "_code", auth_code)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}, data={"phone": PHONE}
    )
    assert result["step_id"] == "verification"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"verification_code": "012345"}
    )
    assert result["step_id"] == "gates"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"selected_gates": ["101:2"], "timeout": 10},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == "42"
    assert result["result"].version == 3
    assert result["result"].data["device_id"]
    assert result["result"].data["auth"]["session_id"] == "app:fixture"
    assert result["result"].data["auth"]["server_expires_at"] is None
    assert all(urlsplit(call[1]).path != OPEN_PATH for call in app_transport.calls)

    entry = result["result"]
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert isinstance(entry.runtime_data.client, QinlinAppClient)
    assert entry.runtime_data.client.device_id == entry.data["device_id"]
    assert any(urlsplit(call[1]).path == CHECK_PATH for call in app_transport.calls)
    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    assert len(entities) == 2
    button = next(entity for entity in entities if entity.domain == "button")
    assert button.unique_id == "42:101:2:open"
    assert hass.states.get(button.entity_id).state != "unavailable"
    await hass.services.async_call(
        "button", "press", {"entity_id": button.entity_id}, blocking=True
    )
    assert sum(urlsplit(call[1]).path == OPEN_PATH for call in app_transport.calls) == 1
    auth_code.assert_not_called()
    assert all(
        call[2]["headers"]["qversioncode"] == "3146" for call in app_transport.calls
    )
    assert not any("/alimini/" in call[1] for call in app_transport.calls)
    for _, url, request in app_transport.calls:
        path = urlsplit(url).path
        if path in (LOGIN_PATH, CHECK_PATH, COMMUNITIES_PATH):
            assert json.loads(request["data"])["version"] == "5.2.6"
        elif path == GATES_PATH:
            fields = {part[0]["name"]: part[2] for part in request["data"]._fields}
            assert fields["version"] == "5.2.6"
        elif path == OPEN_PATH:
            assert request["params"]["version"] == "5.2.6"
    assert await hass.config_entries.async_unload(entry.entry_id)


def test_app_gate_without_mac_can_be_restored():
    gate = Gate("101", "小区", "1", "入口", "")
    assert Gate.from_dict(gate.to_dict()) == gate


async def test_app_authorize_flag_does_not_use_unverified_mini_app_mapping(
    app_client, app_transport
):
    doors = deepcopy(APP_GATE_RESPONSES["101"])
    doors["data"]["authorizeExpired"] = 1
    app_transport.overrides[GATES_PATH] = FakeResponse(doors)
    gates = await app_client.discover_gates()
    assert gates["101:1"].available(CLOCK)
    app_client.clock = lambda: gates["101:1"].permission_expires_at
    assert not gates["101:1"].available(app_client.clock())


async def test_empty_full_door_list_does_not_query_favorites(app_client, app_transport):
    app_transport.overrides[GATES_PATH] = FakeResponse(
        {
            "code": 0,
            "data": {
                "buildingDoorList": [],
                "customDoorList": [],
                "gateDoorList": [],
            },
        }
    )
    assert await app_client.discover_gates() == {}
    paths = [urlsplit(call[1]).path for call in app_transport.calls]
    assert paths == [
        COMMUNITIES_PATH,
        "/api/app/user/v2/queryAllUserDoorByCache",
        "/api/app/user/v2/queryAllUserDoorByCache",
    ]
