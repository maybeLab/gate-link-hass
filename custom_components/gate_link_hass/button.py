"""One stateless opening button per selected gate."""

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GateLinkHassConfigEntry
from .account import AccountRuntime, gate_unique_id
from .const import DOMAIN
from .models import ApiError, Gate


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GateLinkHassConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        [
            GateLinkHassButton(entry.runtime_data, gate)
            for gate in entry.runtime_data.gates.values()
        ]
    )


class GateLinkHassButton(ButtonEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_translation_key = "open_door"
    _attr_icon = "mdi:door-open"
    _attr_should_poll = False

    def __init__(self, runtime: AccountRuntime, gate: Gate) -> None:
        self._runtime = runtime
        self._key = gate.key
        account_id = runtime.account_id
        self._attr_unique_id = gate_unique_id(account_id, gate.key)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{account_id}:{gate.key}")},
            name=gate.name,
            manufacturer="GateLink HASS",
            model="Remote Gate",
        )

    @property
    def available(self) -> bool:
        return self._runtime.authenticated() and self._key in self._runtime.gates

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._runtime.subscribe(self.async_write_ha_state))

    async def async_press(self) -> None:
        try:
            await self._runtime.open(self._key)
        except ApiError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key=err.key
            ) from None
