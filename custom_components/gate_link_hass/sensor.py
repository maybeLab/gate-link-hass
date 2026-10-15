"""One diagnostic login status entity per configured account."""

from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GateLinkHassConfigEntry
from .login_status import (
    ERROR_LABELS,
    LOGIN_CHECK,
    account_device_info,
    localized,
    login_status_signal,
    login_status_unique_id,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GateLinkHassConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([GateLinkHassLoginStatus(entry)])


class GateLinkHassLoginStatus(SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "login_status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_options = ["valid", "expired", "error", "not_checked"]
    _attr_icon = "mdi:account-check"
    _attr_should_poll = False

    def __init__(self, entry: GateLinkHassConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = login_status_unique_id(entry)

    @property
    def device_info(self) -> DeviceInfo:
        return account_device_info(self.hass, self._entry)

    @property
    def native_value(self) -> str:
        if self._entry.data.get("invalidated"):
            return "expired"
        report = self._entry.data.get(LOGIN_CHECK, {})
        if report.get("result") in self._attr_options:
            return report["result"]
        return (
            "valid"
            if self._entry.data["auth"].get("checked_at") is not None
            else "not_checked"
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        report = self._entry.data.get(LOGIN_CHECK, {})
        error = report.get("error")

        def timestamp(value: float | None) -> str | None:
            return (
                datetime.fromtimestamp(value, UTC).isoformat()
                if value is not None
                else None
            )

        return {
            "last_check_at": timestamp(report.get("checked_at")),
            "last_success_at": timestamp(self._entry.data["auth"].get("checked_at")),
            "last_check_source": report.get("source"),
            "last_error": error,
            "last_error_reason": localized(self.hass, ERROR_LABELS[error])
            if error in ERROR_LABELS
            else None,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, login_status_signal(self._entry), self.async_write_ha_state
            )
        )
