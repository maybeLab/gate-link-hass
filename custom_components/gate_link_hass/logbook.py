"""Describe login checks in Home Assistant Activity."""

from collections.abc import Callable

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .login_status import ERROR_LABELS, EVENT_LOGIN_CHECK, localized

SOURCE_LABELS = {
    "startup": ("启动检查", "Startup check"),
    "daily": ("每日检查", "Daily check"),
    "server_reference": ("服务端参考时间检查", "Server reference check"),
    "retry": ("失败重试", "Retry"),
    "options_open": ("打开配置页面", "Opening account settings"),
    "options_save": ("保存配置", "Saving account settings"),
    "reauth": ("重新认证检查", "Reauthentication check"),
    "open_response": ("开门接口响应", "Opening response"),
    "manual": ("登录检查", "Login check"),
}
RESULT_LABELS = {
    "valid": ("登录有效", "Sign-in valid"),
    "expired": ("登录失效，需要重新登录", "Sign-in expired; sign in again"),
    "error": (
        "本次检查失败，未确认登录状态",
        "Check failed; sign-in status unconfirmed",
    ),
}


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, str]]], None],
) -> None:
    @callback
    def describe(event: Event) -> dict[str, str]:
        data = event.data
        device = dr.async_get(hass).async_get(data["device_id"])
        name = (
            device and (device.name_by_user or device.name)
        ) or f"GateLink HASS {data['account']}"
        source = localized(
            hass, SOURCE_LABELS.get(data.get("source"), SOURCE_LABELS["manual"])
        )
        result = localized(
            hass, RESULT_LABELS.get(data.get("result"), RESULT_LABELS["error"])
        )
        message = f"{data['account']} · {source}：{result}"
        if (error := data.get("error")) in ERROR_LABELS and error != "account_expired":
            message += f" ({localized(hass, ERROR_LABELS[error])})"
        return {
            "name": name,
            "message": message,
            "entity_id": data["entity_id"],
            "icon": "mdi:account-check",
        }

    async_describe_event(DOMAIN, EVENT_LOGIN_CHECK, describe)
