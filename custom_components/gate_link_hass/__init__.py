"""Set up a GateLink HASS account and its selected gates."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_TIMEOUT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .account import (
    AccountRuntime,
    clear_account_reminder,
    create_client,
    gate_unique_id,
)
from .const import (
    CONF_DEVICE_ID,
    CONF_PROTOCOL,
    DEFAULT_TIMEOUT,
    DOMAIN,
    PROTOCOL_APP,
)
from .login_status import (
    account_device_info,
    async_register_account,
    login_status_unique_id,
    masked_account,
    safe_error_key,
)
from .models import ApiError, AuthenticationExpired

type GateLinkHassConfigEntry = ConfigEntry[AccountRuntime]
PLATFORMS = [Platform.BUTTON, Platform.SENSOR]
_LOGGER = logging.getLogger(__name__)


async def async_migrate_entry(
    hass: HomeAssistant, entry: GateLinkHassConfigEntry
) -> bool:
    """Preserve v2 bindings but require an App login before using credentials."""
    if entry.version == 2:
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_PROTOCOL: PROTOCOL_APP, "invalidated": True},
            version=3,
        )
        return True
    return entry.version == 3


async def async_setup_entry(
    hass: HomeAssistant, entry: GateLinkHassConfigEntry
) -> bool:
    try:
        runtime = AccountRuntime(
            hass,
            entry,
            create_client(
                hass,
                int(entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)),
                device_id=entry.data.get(CONF_DEVICE_ID),
            ),
        )
    except KeyError, TypeError, ValueError:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="invalid_configuration"
        ) from None
    except ApiError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key=err.key
        ) from None
    entry.runtime_data = runtime
    async_register_account(hass, entry)
    retry = False
    try:
        await runtime.refresh()
    except AuthenticationExpired:
        # Diagnostics stay loaded so rejected accounts retain a visible status.
        pass
    except ApiError as err:
        retry = True
        _LOGGER.warning(
            "Account %s refresh failed (%s); using saved gate bindings",
            masked_account(entry),
            safe_error_key(err.key),
        )
    account_id = runtime.account_id
    desired_entities = {gate_unique_id(account_id, key) for key in runtime.gates}
    desired_entities.add(login_status_unique_id(entry))
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.platform == DOMAIN and entity.unique_id not in desired_entities:
            registry.async_remove(entity.entity_id)
    devices = dr.async_get(hass)
    desired_devices = {(DOMAIN, f"{account_id}:{key}") for key in runtime.gates}
    desired_devices.update(account_device_info(hass, entry)["identifiers"])
    for device in dr.async_entries_for_config_entry(devices, entry.entry_id):
        if not device.identifiers.intersection(desired_devices):
            devices.async_remove_device(device.id)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if runtime.authenticated():
        runtime.start_login_check_timer(retry=retry)
        clear_account_reminder(hass, entry)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: GateLinkHassConfigEntry
) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
