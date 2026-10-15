"""Offline fixtures: all protocol requests stay in memory."""

import asyncio
import json
import socket
from copy import deepcopy
from pathlib import Path
from types import MappingProxyType
from unittest.mock import Mock

import pytest
from homeassistant import loader
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.gate_link_hass.adapters import app_client as app_protocol
from custom_components.gate_link_hass.adapters.mini_app_client import GateLinkHassClient
from custom_components.gate_link_hass.const import (
    CHECK_PATH,
    CLIENT_FACTORY,
    COMMUNITIES_PATH,
    CONF_PROTOCOL,
    CONF_SELECTED_GATES,
    DOMAIN,
    GATES_PATH,
    LOGIN_PATH,
    OPEN_PATH,
    PROTOCOL_APP,
    SMS_PATH,
)
from custom_components.gate_link_hass.models import AccountSession, Gate

CLOCK = 1800000000.0
PHONE = "13800000000"
RESPONSES = json.loads((Path(__file__).parent / "fixtures/responses.json").read_text())


class FakeResponse:
    def __init__(self, result, status=200, error=None, wait=None):
        self.result, self.status, self.error, self.wait = result, status, error, wait

    async def __aenter__(self):
        if self.wait is not None:
            await self.wait.wait()
        if self.error is not None:
            raise self.error
        return self

    async def __aexit__(self, *_):
        return False

    async def json(self, **_):
        if isinstance(self.result, Exception):
            raise self.result
        return deepcopy(self.result)


class FakeSession:
    def __init__(self):
        self.calls = []
        self.overrides = {}
        self.started = asyncio.Event()

    def request(self, method, url, **kwargs):
        from urllib.parse import parse_qs, urlsplit

        path = urlsplit(url).path
        self.calls.append((method, url, kwargs))
        self.started.set()
        if path in self.overrides:
            override = self.overrides[path]
            if callable(override):
                return override(kwargs)
            return override
        if path == app_protocol.LOGIN_PATH:
            source = RESPONSES["login"]["data"]
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "sessionId": source["aliPayminiLoginDTO"]["sessionId"],
                        "userId": source["userId"],
                        "expires": source["aliPayminiLoginDTO"]["expires"],
                    },
                }
            )
        if path == app_protocol.CHECK_PATH:
            return FakeResponse({"code": 0, "data": 7776000})
        if path == app_protocol.GATES_PATH:
            fields = {part[0]["name"]: part[2] for part in kwargs["data"]._fields}
            source = RESPONSES["gates_" + fields["communityId"]]["data"]
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "authorizeExpired": source["authorizeExpired"],
                        "buildingDoorList": source["userDoorDTOS"],
                        "customDoorList": [],
                        "gateDoorList": [source["commonlyUsedDoor"]],
                    },
                }
            )
        name = {
            SMS_PATH: "sms",
            LOGIN_PATH: "login",
            CHECK_PATH: "login",
            COMMUNITIES_PATH: "communities",
            OPEN_PATH: "open",
            app_protocol.COMMUNITIES_PATH: "communities",
            app_protocol.OPEN_PATH: "open",
        }.get(path)
        if path == GATES_PATH:
            name = "gates_" + parse_qs(kwargs["data"])["communityId"][0]
        assert name is not None, path
        return FakeResponse(RESPONSES[name])


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Tests must not resolve DNS or create real connections")

    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


@pytest.fixture
def transport():
    return FakeSession()


class Clock:
    def __init__(self):
        self.now = CLOCK

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def legacy_client(transport, clock):
    result = GateLinkHassClient(transport, clock=clock, sms_signer=lambda *_: "a" * 32)
    result.auth = AccountSession.from_login(RESPONSES["login"]["data"], CLOCK)
    return result


@pytest.fixture
def client(transport, clock):
    result = app_protocol.QinlinAppClient(transport, clock=clock)
    result.auth = AccountSession.from_login(RESPONSES["login"]["data"], CLOCK)
    return result


@pytest.fixture
async def hass(tmp_path, monkeypatch, transport, clock):
    (tmp_path / "custom_components").symlink_to(
        Path(__file__).resolve().parents[1] / "custom_components"
    )
    hass = HomeAssistant(str(tmp_path))
    hass.config_entries = ConfigEntries(hass, {})
    loader.async_setup(hass)
    hass.config.skip_pip = True
    await hass.config_entries.async_initialize()
    dr.async_setup(hass)
    await dr.async_load(hass)
    await er.async_load(hass)

    def factory(_session, *, timeout, device_id=None):
        return app_protocol.QinlinAppClient(
            transport, timeout=timeout, clock=clock, device_id=device_id
        )

    hass.data[DOMAIN] = {CLIENT_FACTORY: factory}
    monkeypatch.setattr(
        "custom_components.gate_link_hass.account.async_get_clientsession",
        lambda _: transport,
    )
    # Reminders are verified as calls, without setting up unrelated HA components.
    from homeassistant.components import persistent_notification

    monkeypatch.setattr(persistent_notification, "async_create", Mock())
    monkeypatch.setattr(persistent_notification, "async_dismiss", Mock())
    monkeypatch.setattr(ConfigEntry, "async_start_reauth", Mock())
    yield hass
    await hass.async_block_till_done()
    await hass.async_stop(force=True)


def make_entry(*, version=3, auth=None, gates=None, selected=None, invalidated=False):
    if auth is None:
        auth = AccountSession.from_login(RESPONSES["login"]["data"], CLOCK)
    if gates is None:
        gates = [Gate("101", "测试小区一", "1", "入口", "000000000001")]
    return ConfigEntry(
        version=version,
        minor_version=1,
        domain=DOMAIN,
        title="GateLink HASS · ****0000",
        data={
            **({CONF_PROTOCOL: PROTOCOL_APP} if version >= 3 else {}),
            "phone": PHONE,
            "auth": auth.to_dict(),
            "gates": [g.to_dict() for g in gates],
            "invalidated": invalidated,
        },
        options={
            CONF_SELECTED_GATES: [g.key for g in gates]
            if selected is None
            else selected,
            "timeout": 10,
            "lifetime_days": 30,
        },
        source="user",
        unique_id=auth.account_id,
        discovery_keys=MappingProxyType({}),
        subentries_data=[],
    )


@pytest.fixture
def entry(hass):
    entry = make_entry()
    hass.config_entries._entries[entry.entry_id] = entry
    return entry
