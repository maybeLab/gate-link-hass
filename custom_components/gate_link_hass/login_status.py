"""Account diagnostics and credential-free login check events."""

from enum import StrEnum

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .adapters.mini_app_client import GateLinkHassClient
from .const import CONF_PHONE, DOMAIN
from .models import ApiError, AuthenticationExpired

EVENT_LOGIN_CHECK = f"{DOMAIN}_login_check"
LOGIN_CHECK = "login_check"


class LoginCheckSource(StrEnum):
    STARTUP = "startup"
    DAILY = "daily"
    REFERENCE = "server_reference"
    RETRY = "retry"
    OPTIONS_OPEN = "options_open"
    OPTIONS_SAVE = "options_save"
    REAUTH = "reauth"
    OPEN_RESPONSE = "open_response"
    MANUAL = "manual"


# Only these safe keys may enter diagnostics, even for an injected client.
ERROR_LABELS = {
    "account_expired": ("登录已失效", "Sign-in has expired"),
    "timeout": ("请求超时", "Request timed out"),
    "cannot_connect": ("无法连接服务", "Cannot connect to the service"),
    "http_error": ("服务返回 HTTP 错误", "The service returned an HTTP error"),
    "invalid_response": ("服务响应格式异常", "Invalid service response"),
    "permission_denied": ("服务拒绝访问", "Access was denied"),
    "request_rejected": ("服务拒绝请求", "The service rejected the request"),
}


def safe_error_key(key: str) -> str:
    return key if key in ERROR_LABELS else "request_rejected"


def localized(hass: HomeAssistant, labels: tuple[str, str]) -> str:
    return labels[0 if hass.config.language.startswith("zh") else 1]


def masked_account(entry: ConfigEntry) -> str:
    return "****" + str(entry.data.get(CONF_PHONE, ""))[-4:]


def login_status_unique_id(entry: ConfigEntry) -> str:
    return f"{entry.unique_id or entry.data['auth']['account_id']}:login_status"


def login_status_signal(entry: ConfigEntry) -> str:
    return f"{DOMAIN}_{entry.entry_id}_login_status"


def account_device_info(hass: HomeAssistant, entry: ConfigEntry) -> DeviceInfo:
    account_id = entry.unique_id or entry.data["auth"]["account_id"]
    account = localized(hass, ("账号", "Account"))
    return DeviceInfo(
        identifiers={(DOMAIN, f"account:{account_id}")},
        name=f"GateLink HASS {account} {masked_account(entry)}",
        manufacturer="GateLink HASS",
        model="Account",
    )


@callback
def async_register_account(hass: HomeAssistant, entry: ConfigEntry) -> er.RegistryEntry:
    """Register before the first request so startup events have stable owners."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, **account_device_info(hass, entry)
    )
    return er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        login_status_unique_id(entry),
        config_entry=entry,
        device_id=device.id,
        suggested_object_id=f"gate_link_hass_account_{masked_account(entry)[4:]}_login_status",
        has_entity_name=True,
        entity_category=EntityCategory.DIAGNOSTIC,
        original_device_class=SensorDeviceClass.ENUM,
        original_icon="mdi:account-check",
        original_name=localized(hass, ("登录状态", "Login status")),
        translation_key="login_status",
    )


@callback
def async_record_login_check(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    source: LoginCheckSource,
    result: str,
    checked_at: float,
    error: str | None = None,
) -> None:
    """Store only the latest report; Recorder owns the activity history."""
    entity = async_register_account(hass, entry)
    report = {
        "checked_at": checked_at,
        "source": source.value,
        "result": result,
        "error": safe_error_key(error) if error is not None else None,
    }
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, LOGIN_CHECK: report}
    )
    hass.bus.async_fire(
        EVENT_LOGIN_CHECK,
        {
            **report,
            "account": masked_account(entry),
            "device_id": entity.device_id,
            "entity_id": entity.entity_id,
        },
    )
    async_dispatcher_send(hass, login_status_signal(entry))


async def async_check_login(
    hass: HomeAssistant,
    entry: ConfigEntry | None,
    client: GateLinkHassClient,
    source: LoginCheckSource,
    *,
    persist_auth: bool = True,
) -> None:
    """Observe a real check; unfinished initial setup has no account to record."""
    result, error = None, None
    try:
        await client.check_login()
    except AuthenticationExpired:
        result, error = "expired", "account_expired"
        raise
    except ApiError as err:
        result, error = "error", safe_error_key(err.key)
        raise
    else:
        result = "valid"
    finally:
        if entry is not None and result is not None:
            if result == "valid" and persist_auth:
                assert client.auth is not None
                hass.config_entries.async_update_entry(
                    entry, data={**entry.data, "auth": client.auth.to_dict()}
                )
            async_record_login_check(
                hass,
                entry,
                source=source,
                result=result,
                checked_at=client.clock(),
                error=error,
            )
