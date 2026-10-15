"""Runtime, server-authoritative login checks and account reminders."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.event import async_track_point_in_utc_time

from .adapters.app_client import QinlinAppClient
from .adapters.mini_app_client import GateLinkHassClient
from .const import (
    CLIENT_FACTORY,
    CONF_PHONE,
    CONF_PROTOCOL,
    CONF_SELECTED_GATES,
    DEFAULT_TIMEOUT,
    DOMAIN,
    PROTOCOL_APP,
)
from .login_status import (
    LoginCheckSource,
    async_check_login,
    async_record_login_check,
    login_status_signal,
    safe_error_key,
)
from .models import AccountSession, ApiError, AuthenticationExpired, Gate, identifier

_LOGGER = logging.getLogger(__name__)
LOGIN_CHECK_RETRY_SECONDS = 300
LOGIN_CHECK_TIME_ZONE = ZoneInfo("Asia/Shanghai")


def create_client(
    hass: HomeAssistant, timeout: int = DEFAULT_TIMEOUT, *, device_id: str | None = None
) -> GateLinkHassClient:
    """Use the App protocol unless a custom transport is explicitly injected."""
    factory = hass.data.get(DOMAIN, {}).get(CLIENT_FACTORY, QinlinAppClient)
    kwargs = {"device_id": identifier(device_id)} if device_id is not None else {}
    return factory(async_get_clientsession(hass), timeout=timeout, **kwargs)


def notification_id(entry: ConfigEntry) -> str:
    return f"{DOMAIN}_{entry.entry_id}_account_expired"


@callback
def mark_account_expired(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Persist early invalidation so a restart cannot revive a rejected token."""
    if not entry.data.get("invalidated"):
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "invalidated": True}
        )
        async_dispatcher_send(hass, login_status_signal(entry))
    notified = hass.data.setdefault(DOMAIN, {}).setdefault(
        "expired_notifications", set()
    )
    if entry.entry_id not in notified:
        notified.add(entry.entry_id)
        account = "****" + str(entry.data.get(CONF_PHONE, ""))[-4:]
        if hass.config.language.startswith("zh"):
            message = f"账号 {account} 已失效，开门功能已暂停。请在设置 → 设备与服务 → GateLink HASS 中重新登录。"
        else:
            message = f"Account {account} has expired. Opening is paused. Sign in again in Settings → Devices & services → GateLink HASS."
        persistent_notification.async_create(
            hass, message, title="GateLink HASS", notification_id=notification_id(entry)
        )
        entry.async_start_reauth(hass)


@callback
def clear_account_reminder(hass: HomeAssistant, entry: ConfigEntry) -> None:
    hass.data.setdefault(DOMAIN, {}).setdefault("expired_notifications", set()).discard(
        entry.entry_id
    )
    persistent_notification.async_dismiss(hass, notification_id(entry))


def gate_unique_id(account_id: str, gate_key: str) -> str:
    return f"{account_id}:{gate_key}:open"


class AccountRuntime:
    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: GateLinkHassClient
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.client = client
        client.auth = AccountSession.from_dict(dict(entry.data["auth"]))
        self.account_id = client.auth.account_id
        self._expired = bool(entry.data.get("invalidated")) or (
            entry.data.get(CONF_PROTOCOL) != PROTOCOL_APP
        )
        self._listeners: set[Callable[[], None]] = set()
        self._locks: dict[str, asyncio.Lock] = {}
        self._check_lock = asyncio.Lock()
        self._timer_enabled = False
        self._retry_login_check = False
        self._scheduled_source = LoginCheckSource.REFERENCE
        self._cancel_login_check: Callable[[], None] | None = None
        catalog = {
            gate.key: gate for gate in (Gate.from_dict(g) for g in entry.data["gates"])
        }
        selected = entry.options[CONF_SELECTED_GATES]
        if not selected or not set(selected).issubset(catalog):
            raise ApiError("invalid_configuration")
        self.gates = {key: catalog[key] for key in selected}
        entry.async_on_unload(
            async_dispatcher_connect(
                hass, login_status_signal(entry), self._async_account_updated
            )
        )

    @property
    def expired(self) -> bool:
        return not self.authenticated()

    def authenticated(self) -> bool:
        return (
            not self._expired
            and not self.entry.data.get("invalidated")
            and self.client.auth is not None
        )

    @callback
    def _async_account_updated(self) -> None:
        """Apply rejection discovered by a separate options-flow client."""
        if self.entry.data.get("invalidated") and not self._expired:
            self._expired = True
            self._cancel_timer()
            self._changed()

    @callback
    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    @callback
    def _changed(self) -> None:
        for listener in tuple(self._listeners):
            listener()

    @callback
    def invalidate(self, *, source: LoginCheckSource | None = None) -> None:
        self._cancel_timer()
        if not self._expired:
            if source is not None:
                async_record_login_check(
                    self.hass,
                    self.entry,
                    source=source,
                    result="expired",
                    checked_at=self.client.clock(),
                    error="account_expired",
                )
            self._expired = True
            self._changed()
        mark_account_expired(self.hass, self.entry)

    @callback
    def start_login_check_timer(self, *, retry: bool = False) -> None:
        if not self._timer_enabled:
            self._timer_enabled = True
            self.entry.async_on_unload(self._stop_login_checks)
        self._schedule_login_check(retry=retry or self._retry_login_check)

    @callback
    def _cancel_timer(self) -> None:
        if self._cancel_login_check is not None:
            self._cancel_login_check()
            self._cancel_login_check = None

    @callback
    def _stop_login_checks(self) -> None:
        self._timer_enabled = False
        self._cancel_timer()

    @callback
    def _schedule_login_check(self, *, retry: bool = False) -> None:
        self._cancel_timer()
        if not self._timer_enabled or not self.authenticated():
            return
        assert self.client.auth is not None
        now = self.client.clock()
        daily_check = datetime.fromtimestamp(now, LOGIN_CHECK_TIME_ZONE).replace(
            hour=6, minute=0, second=0, microsecond=0
        )
        if daily_check.timestamp() <= now:
            daily_check += timedelta(days=1)
        when = (
            now + LOGIN_CHECK_RETRY_SECONDS
            if retry
            else max(now + 1, self.client.auth.next_check_at or now)
        )
        # Keep the daily check fixed even when a request updates the reference
        # or a failed check needs an earlier retry. One timer handles both.
        self._scheduled_source = (
            LoginCheckSource.RETRY if retry else LoginCheckSource.REFERENCE
        )
        if daily_check.timestamp() <= when:
            when = daily_check.timestamp()
            self._scheduled_source = LoginCheckSource.DAILY
        self._cancel_login_check = async_track_point_in_utc_time(
            self.hass, self._async_scheduled_check, datetime.fromtimestamp(when, UTC)
        )

    async def _async_scheduled_check(self, _: datetime) -> None:
        if not self._timer_enabled:
            return
        try:
            await self.check_login(source=self._scheduled_source)
        except AuthenticationExpired:
            pass  # check_login has already persisted rejection and requested reauth.
        except ApiError as err:
            _LOGGER.warning(
                "Scheduled login check failed (%s); retrying within %s seconds",
                safe_error_key(err.key),
                LOGIN_CHECK_RETRY_SECONDS,
            )

    async def check_login(
        self, *, source: LoginCheckSource = LoginCheckSource.MANUAL
    ) -> None:
        async with self._check_lock:
            if not self.authenticated():
                self.invalidate()
                raise AuthenticationExpired()
            try:
                await async_check_login(self.hass, self.entry, self.client, source)
            except AuthenticationExpired:
                self.invalidate()
                raise
            except ApiError:
                self._retry_login_check = True
                self._schedule_login_check(retry=True)
                raise
            self._retry_login_check = False
            self._schedule_login_check()

    async def refresh(self) -> None:
        if not self.authenticated():
            self.invalidate()
            raise AuthenticationExpired()
        try:
            await self.check_login(source=LoginCheckSource.STARTUP)
            discovered = await self.client.discover_gates()
            if not self.authenticated():
                raise AuthenticationExpired()
        except AuthenticationExpired:
            self.invalidate()
            raise
        self.gates = {
            key: discovered.get(key, replace(old, permission_valid=False))
            for key, old in self.gates.items()
        }
        self._changed()

    async def open(self, key: str) -> None:
        """Send one opening request using the bound gate and saved session."""
        if not self.authenticated():
            self.invalidate()
            raise AuthenticationExpired()
        lock = self._locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            raise ApiError("request_in_progress")
        async with lock:
            try:
                if key not in self.gates:
                    raise ApiError("permission_denied")
                await self.client.open_door(self.gates[key])
            except AuthenticationExpired:
                self.invalidate(source=LoginCheckSource.OPEN_RESPONSE)
                raise
