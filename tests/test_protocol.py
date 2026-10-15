"""Protocol requests and error handling, without real HTTP traffic."""

import json
import string
from copy import deepcopy
from urllib.parse import parse_qsl, urlsplit

import pytest
from aiohttp import ClientConnectionError
from conftest import CLOCK, PHONE, RESPONSES, FakeResponse

from custom_components.gate_link_hass.adapters.mini_app_client import (
    GateLinkHassClient,
    default_sms_signer,
    placeholder_auth_code,
)
from custom_components.gate_link_hass.const import (
    COMMUNITIES_PATH,
    GATES_PATH,
    OPEN_PATH,
    REFERER_GATES,
    REFERER_LOGIN,
    SMS_PATH,
    USER_AGENT_GATES,
    USER_AGENT_LOGIN,
)
from custom_components.gate_link_hass.models import (
    AccountSession,
    ApiError,
    AuthenticationExpired,
)


@pytest.fixture
def client(legacy_client):
    return legacy_client


async def test_sms_headers_and_compact_json(client, transport):
    await client.send_sms(PHONE)
    method, url, request = transport.calls[0]
    assert method == "POST" and urlsplit(url).path == SMS_PATH
    assert request["params"] == {"sessionId": ""}
    assert request["data"] == json.dumps({"mobile": PHONE}, separators=(",", ":"))
    headers = request["headers"]
    assert headers["User-Agent"] == USER_AGENT_LOGIN
    assert headers["Referer"] == REFERER_LOGIN
    assert headers["Content-Type"] == "application/json"
    assert headers["version"] == "v2" and len(headers["appid"]) == 16
    assert headers["timestamp"] == str(int(CLOCK * 1000))
    assert headers["nonce"] == "9999"
    assert headers["sign"] == "A" * 32
    assert "Content-Length" not in headers
    assert request["allow_redirects"] is False


async def test_missing_signer_never_sends(transport):
    client = GateLinkHassClient(transport, sms_signer=None)
    with pytest.raises(ApiError, match="signing_unavailable"):
        await client.send_sms(PHONE)
    assert not transport.calls


async def test_default_sms_signer_is_used_without_injection(transport, clock):
    client = GateLinkHassClient(transport, clock=clock)
    await client.send_sms(PHONE)
    assert len(transport.calls) == 1
    request = transport.calls[0][2]
    assert request["headers"]["sign"] == "B27381898E9AA10841497A6E4DB54C1E"
    assert request["headers"]["nonce"] == "9999"
    assert request["headers"]["timestamp"] == "1800000000000"
    assert request["data"] == '{"mobile":"13800000000"}'
    assert "appsecret" not in request["data"]
    assert "appsecret" not in request["headers"]
    clock.now += 1
    await client.send_sms(PHONE)
    assert transport.calls[-1][2]["headers"]["timestamp"] == "1800000001000"
    assert transport.calls[-1][2]["headers"]["sign"] != request["headers"]["sign"]


def test_sms_signature_uses_sorted_fields_instead_of_json_format():
    expected = "B27381898E9AA10841497A6E4DB54C1E"
    for body in ('{"mobile":"13800000000"}', '{ "mobile" : "13800000000" }'):
        assert default_sms_signer(body, "1800000000000", "9999") == expected


@pytest.mark.parametrize(
    "body",
    ["[]", "{}", "null", '{"mobile":123}', '{"mobile":"13800000000","unexpected":1}'],
)
def test_sms_signer_rejects_unsupported_body(body):
    with pytest.raises(ValueError):
        default_sms_signer(body, "1800000000000", "9999")


async def test_login_form_and_original_lifetime(client, transport):
    auth = await client.login(PHONE, "012345")
    request = transport.calls[0][2]
    fields = dict(parse_qsl(request["data"], keep_blank_values=True))
    assert list(fields) == ["code", "mobile", "securityCode"]
    assert fields["securityCode"] == "012345" and fields["mobile"] == PHONE
    assert len(fields["code"]) == 32 and fields["code"].isalnum()
    assert request["params"] == {"sessionId": "null"}
    assert request["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert auth.server_expires_at == CLOCK + 2592000
    client.clock = lambda: CLOCK + 100
    await client.check_login()
    assert client.auth is auth
    assert transport.calls[-1][2]["headers"]["User-Agent"] == USER_AGENT_GATES
    assert dict(parse_qsl(transport.calls[-1][2]["data"]))["code"] != fields["code"]


async def test_cross_community_merge_and_open(client, transport):
    gates = await client.discover_gates()
    assert set(gates) == {"101:1", "101:2", "102:1"}
    assert gates["101:1"].name == gates["102:1"].name
    assert gates["101:1"].label != gates["102:1"].label
    await client.open_door(gates["101:2"])
    assert [urlsplit(c[1]).path for c in transport.calls] == [
        COMMUNITIES_PATH,
        GATES_PATH,
        GATES_PATH,
        OPEN_PATH,
    ]
    for method, _, request in transport.calls:
        assert method == "POST"
        assert request["headers"]["User-Agent"] == USER_AGENT_GATES
        assert request["headers"]["Referer"] == REFERER_GATES
        assert request["params"]["sessionId"] == client.auth.session_id
    assert transport.calls[0][2]["data"] == ""
    assert dict(parse_qsl(transport.calls[-1][2]["data"], keep_blank_values=True)) == {
        "doorControlId": "2",
        "macAddress": "000000000002",
        "openType": "",
    }


@pytest.mark.parametrize(
    "status,body,error,key",
    [
        (401, {}, None, "account_expired"),
        (403, {}, None, "permission_denied"),
        (503, {}, None, "http_error"),
        (307, {}, None, "http_error"),
        (200, [], None, "invalid_response"),
        (200, ValueError(), None, "invalid_response"),
        (200, {"code": 9, "message": "session expired"}, None, "account_expired"),
        (200, {"code": 9, "message": "permission missing"}, None, "request_rejected"),
        (200, {"code": 0, "data": {"openDoorState": 0}}, None, "open_failed"),
        (200, {}, TimeoutError(), "timeout"),
        (200, {}, ClientConnectionError("private URL or token"), "cannot_connect"),
    ],
)
async def test_failures_are_sanitized_and_never_retried(
    client, transport, status, body, error, key
):
    gates = await client.discover_gates()
    transport.calls.clear()
    transport.overrides[OPEN_PATH] = FakeResponse(body, status=status, error=error)
    with pytest.raises(ApiError) as caught:
        await client.open_door(gates["101:1"])
    assert caught.value.key == key
    assert "private" not in str(caught.value)
    assert len(transport.calls) == 1


async def test_expired_session_and_permission_are_distinct(client, transport):
    gates = await client.discover_gates()
    client.clock = lambda: gates["101:1"].permission_expires_at
    client.auth = AccountSession(
        "42", "fixture-token", client.clock() - 1, client.clock() + 100
    )
    client.lifetime_days = 365
    transport.calls.clear()
    with pytest.raises(ApiError, match="permission_denied"):
        await client.open_door(gates["101:1"])
    assert not transport.calls
    client.clock = lambda: client.auth.server_expires_at
    with pytest.raises(AuthenticationExpired):
        await client.open_door(gates["101:1"])
    assert not transport.calls


async def test_empty_and_revoked_communities(client, transport):
    transport.overrides[COMMUNITIES_PATH] = FakeResponse({"code": 0, "data": []})
    assert await client.discover_gates() == {}
    communities = deepcopy(RESPONSES["communities"])
    communities["data"][0]["hasPermission"] = False
    transport.overrides[COMMUNITIES_PATH] = FakeResponse(communities)
    gates = await client.discover_gates()
    assert not gates["101:1"].available(CLOCK)
    assert gates["102:1"].available(CLOCK)


def test_placeholder_shape_and_session_storage():
    code = placeholder_auth_code()
    assert len(code) == 32 and all(
        c in string.ascii_letters + string.digits for c in code
    )
    auth = AccountSession.from_login(RESPONSES["login"]["data"], CLOCK)
    assert auth == AccountSession.from_dict(auth.to_dict())
    assert auth.deadline(1) == CLOCK + 86400
    assert auth.deadline(365) == auth.server_expires_at
    assert auth.expired(auth.server_expires_at)
    assert auth.session_id not in repr(auth)


@pytest.mark.parametrize(
    "expires", [None, 0, -1, True, "2592000", float("nan"), float("inf"), 10**1000]
)
def test_invalid_session_lifetimes(expires):
    data = deepcopy(RESPONSES["login"]["data"])
    data["aliPayminiLoginDTO"]["expires"] = expires
    with pytest.raises(ApiError):
        AccountSession.from_login(data, CLOCK)


@pytest.mark.parametrize("provider", ["auth", "signer"])
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_provider_failures_are_safe_and_send_nothing(
    transport, provider, asynchronous
):
    def failure(*_):
        raise RuntimeError("private credential in provider error")

    async def async_failure(*args):
        return failure(*args)

    adapter = async_failure if asynchronous else failure
    client = GateLinkHassClient(
        transport,
        **{"auth_code_provider" if provider == "auth" else "sms_signer": adapter},
    )
    with pytest.raises(ApiError) as caught:
        if provider == "auth":
            await client.login(PHONE, "012345")
        else:
            await client.send_sms(PHONE)
    assert caught.value.key == (
        "authorization_unavailable" if provider == "auth" else "signing_unavailable"
    )
    assert "private" not in str(caught.value)
    assert not transport.calls


async def test_async_auth_and_signer_adapters(transport):
    async def code():
        return "A" * 32

    async def sign(body, timestamp, nonce):
        assert json.loads(body) == {"mobile": PHONE}
        assert len(timestamp) == 13 and len(nonce) == 4
        return "b" * 32

    client = GateLinkHassClient(
        transport, clock=lambda: CLOCK, auth_code_provider=code, sms_signer=sign
    )
    await client.send_sms(PHONE)
    await client.login(PHONE, "012345")
    assert dict(parse_qsl(transport.calls[1][2]["data"]))["code"] == "A" * 32


@pytest.mark.parametrize("data", [[], None, {"code": False}, {"code": "0"}])
def test_malformed_login_data_rejected(data):
    with pytest.raises(ApiError, match="invalid_response"):
        AccountSession.from_login(data, CLOCK)
