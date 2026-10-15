"""Phone verification, account reauthentication and cross-community binding."""

import re
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import probatio
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_TIMEOUT
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util import dt as dt_util

from .account import clear_account_reminder, create_client, mark_account_expired
from .adapters.mini_app_client import GateLinkHassClient
from .const import (
    CONF_DEVICE_ID,
    CONF_PHONE,
    CONF_PROTOCOL,
    CONF_SELECTED_GATES,
    CONF_VERIFICATION_CODE,
    DEFAULT_TIMEOUT,
    DOMAIN,
    PROTOCOL_APP,
)
from .login_status import LoginCheckSource, async_check_login
from .models import AccountSession, ApiError, AuthenticationExpired, Gate


def phone_schema(phone: str | None = None) -> probatio.Schema:
    key = (
        probatio.Required(CONF_PHONE, default=phone)
        if phone
        else probatio.Required(CONF_PHONE)
    )
    return probatio.Schema(
        {key: TextSelector(TextSelectorConfig(type=TextSelectorType.TEL))}
    )


def gate_schema(gates: dict[str, Gate]) -> probatio.Schema:
    return probatio.Schema(
        {
            probatio.Required(CONF_SELECTED_GATES): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        {"value": key, "label": gate.label}
                        for key, gate in gates.items()
                    ],
                    multiple=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            probatio.Required(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): NumberSelector(
                NumberSelectorConfig(min=1, max=60, step=1, mode=NumberSelectorMode.BOX)
            ),
        }
    )


def selected_options(
    data: dict[str, Any],
    catalog: dict[str, Gate],
    now: float,
    previous: set[str] | None = None,
) -> dict[str, Any]:
    selected = data.get(CONF_SELECTED_GATES, [])
    if (
        not isinstance(selected, list)
        or not selected
        or not all(isinstance(k, str) for k in selected)
    ):
        raise ApiError("select_gates")
    if not set(selected).issubset(catalog):
        raise ApiError("select_gates")
    if any(
        not catalog[key].available(now) and key not in (previous or set())
        for key in selected
    ):
        raise ApiError("permission_denied")
    value = data.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 1 <= value <= 60
        or int(value) != value
    ):
        raise ApiError("invalid_configuration")
    return {
        CONF_SELECTED_GATES: list(dict.fromkeys(selected)),
        CONF_TIMEOUT: int(data.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)),
    }


def merged_catalog(current: dict[str, Gate], entry: ConfigEntry) -> dict[str, Gate]:
    """Keep missing selected gates unavailable until explicitly unbound."""
    result = dict(current)
    for data in entry.data["gates"]:
        gate = Gate.from_dict(data)
        if gate.key in entry.options[CONF_SELECTED_GATES] and gate.key not in result:
            result[gate.key] = replace(gate, permission_valid=False)
    return result


class GateLinkHassConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 3

    def __init__(self) -> None:
        self._client: GateLinkHassClient | None = None
        self._phone = ""
        self._reauth: ConfigEntry | None = None
        self._catalog: dict[str, Gate] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            phone = str(user_input[CONF_PHONE]).strip()
            if not re.fullmatch(r"1[3-9][0-9]{9}", phone):
                errors[CONF_PHONE] = "invalid_phone"
            else:
                self._phone = phone
                self._client = create_client(self.hass)
                self._catalog = None
                try:
                    await self._client.send_sms(phone)
                except ApiError as err:
                    errors["base"] = err.key
                else:
                    return await self.async_step_verification()
        return self.async_show_form(
            step_id="user", data_schema=phone_schema(self._phone or None), errors=errors
        )

    async def async_step_verification(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            assert self._client is not None
            if user_input.get("resend"):
                try:
                    await self._client.send_sms(self._phone)
                except ApiError as err:
                    errors["base"] = err.key
            else:
                code = str(user_input.get(CONF_VERIFICATION_CODE, "")).strip()
                if not re.fullmatch(r"[0-9]{6}", code):
                    errors[CONF_VERIFICATION_CODE] = "invalid_verification_code"
                else:
                    try:
                        auth = await self._client.login(self._phone, code)
                    except ApiError as err:
                        errors["base"] = err.key
                    else:
                        await self.async_set_unique_id(auth.account_id)
                        if self._reauth is not None:
                            self._abort_if_unique_id_mismatch(reason="wrong_account")
                        else:
                            self._abort_if_unique_id_configured()
                        return await self.async_step_gates()
        return self.async_show_form(
            step_id="verification",
            errors=errors,
            data_schema=probatio.Schema(
                {
                    probatio.Optional(CONF_VERIFICATION_CODE): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                    probatio.Optional("resend", default=False): BooleanSelector(),
                }
            ),
            description_placeholders={"account": "****" + self._phone[-4:]},
        )

    async def async_step_gates(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._client is not None and self._client.auth is not None
        errors = {}
        if self._catalog is None:
            try:
                await async_check_login(
                    self.hass,
                    self._reauth,
                    self._client,
                    LoginCheckSource.REAUTH,
                    persist_auth=False,
                )
                self._catalog = await self._client.discover_gates()
                self._client.require_auth()
            except ApiError as err:
                self._catalog = None
                errors["base"] = err.key
            else:
                if self._reauth is not None:
                    catalog = merged_catalog(self._catalog, self._reauth)
                    clear_account_reminder(self.hass, self._reauth)
                    return self.async_update_reload_and_abort(
                        self._reauth,
                        data={
                            **self._reauth.data,
                            CONF_PHONE: self._phone,
                            CONF_PROTOCOL: PROTOCOL_APP,
                            CONF_DEVICE_ID: getattr(self._client, "device_id", None),
                            "auth": self._client.auth.to_dict(),
                            "gates": [g.to_dict() for g in catalog.values()],
                            "invalidated": False,
                        },
                    )
                self._catalog = {
                    key: gate
                    for key, gate in self._catalog.items()
                    if gate.available(self._client.clock())
                }
                if not self._catalog:
                    return self.async_abort(reason="no_gates")
        if not errors and user_input:
            try:
                await async_check_login(
                    self.hass, None, self._client, LoginCheckSource.MANUAL
                )
                options = selected_options(
                    user_input, self._catalog or {}, self._client.clock()
                )
            except ApiError as err:
                errors["base"] = err.key
            else:
                return self.async_create_entry(
                    title="GateLink HASS · ****" + self._phone[-4:],
                    data={
                        CONF_PHONE: self._phone,
                        CONF_PROTOCOL: PROTOCOL_APP,
                        CONF_DEVICE_ID: getattr(self._client, "device_id", None),
                        "auth": self._client.auth.to_dict(),
                        "gates": [g.to_dict() for g in (self._catalog or {}).values()],
                        "invalidated": False,
                    },
                    options=options,
                )
        return self.async_show_form(
            step_id="gates",
            errors=errors,
            data_schema=self.add_suggested_values_to_schema(
                gate_schema(self._catalog)
                if self._catalog is not None
                else probatio.Schema({}),
                user_input,
            ),
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        self._reauth = self._get_reauth_entry()
        self._phone = self._reauth.data[CONF_PHONE]
        return await self.async_step_user()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return GateLinkHassOptionsFlow()


class GateLinkHassOptionsFlow(OptionsFlowWithReload):
    def __init__(self) -> None:
        self._client: GateLinkHassClient | None = None
        self._catalog: dict[str, Gate] | None = None
        self._login_checked = False

    async def _async_account_summary(self) -> dict[str, str]:
        """Describe this account without exposing credentials or promising expiry."""
        entry = self.config_entry
        chinese = self.hass.config.language.startswith("zh")
        missing = "暂无记录" if chinese else "Not recorded"
        status = (
            ("本次检查通过" if chinese else "Check passed on this visit")
            if self._login_checked
            else ("本次未确认" if chinese else "Not confirmed on this visit")
        )
        time_zone = await dt_util.async_get_time_zone(self.hass.config.time_zone)

        def format_time(timestamp: float | None) -> str:
            if timestamp is None:
                return missing
            try:
                return datetime.fromtimestamp(timestamp, time_zone or UTC).isoformat(
                    sep=" ", timespec="seconds"
                )
            except OverflowError, OSError, ValueError:
                return missing

        auth = self._client.auth if self._client is not None else None
        selected = set(entry.options.get(CONF_SELECTED_GATES, []))
        return {
            "account": "****" + str(entry.data.get(CONF_PHONE, ""))[-4:],
            "user_id": auth.account_id
            if auth is not None
            else entry.unique_id or missing,
            "login_status": status,
            "issued_at": format_time(auth.issued_at if auth is not None else None),
            "checked_at": format_time(auth.checked_at if auth is not None else None),
            "community_count": str(len({key.partition(":")[0] for key in selected})),
            "gate_count": str(len(selected)),
            "time_zone": self.hass.config.time_zone,
        }

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self.config_entry
        errors = {}
        try:
            if self._client is None:
                client = create_client(
                    self.hass,
                    int(entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)),
                    device_id=entry.data.get(CONF_DEVICE_ID),
                )
                client.auth = AccountSession.from_dict(dict(entry.data["auth"]))
                self._client = client
            if (
                entry.data.get("invalidated")
                or entry.data.get(CONF_PROTOCOL) != PROTOCOL_APP
            ):
                raise AuthenticationExpired()
            self._client.require_auth()
            if self._catalog is None:
                self._login_checked = False
                await async_check_login(
                    self.hass, entry, self._client, LoginCheckSource.OPTIONS_OPEN
                )
                self._login_checked = True
                self._catalog = merged_catalog(
                    await self._client.discover_gates(), entry
                )
                self._client.require_auth()
            if user_input:
                self._login_checked = False
                await async_check_login(
                    self.hass, entry, self._client, LoginCheckSource.OPTIONS_SAVE
                )
                self._login_checked = True
                options = selected_options(
                    user_input,
                    self._catalog,
                    self._client.clock(),
                    set(entry.options[CONF_SELECTED_GATES]),
                )
                # HA reloads changed options automatically. Refresh the runtime
                # even when saving the same selection after permissions recover.
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={
                        **entry.data,
                        "auth": self._client.auth.to_dict(),
                        "gates": [g.to_dict() for g in self._catalog.values()],
                    },
                )
                if options == entry.options:
                    self.hass.config_entries.async_schedule_reload(entry.entry_id)
                return self.async_create_entry(title="", data=options)
        except AuthenticationExpired:
            mark_account_expired(self.hass, entry)
            return self.async_abort(reason="reauth_required")
        except ApiError as err:
            errors["base"] = (
                "invalid_configuration" if self._client is None else err.key
            )
        except KeyError, TypeError, ValueError:
            errors["base"] = "invalid_configuration"
        return self.async_show_form(
            step_id="init",
            errors=errors,
            data_schema=self.add_suggested_values_to_schema(
                gate_schema(self._catalog)
                if self._catalog is not None
                else probatio.Schema({}),
                user_input if user_input is not None else dict(entry.options),
            ),
            description_placeholders=await self._async_account_summary(),
        )
