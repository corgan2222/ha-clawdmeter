"""The Clawdmeter integration."""

from datetime import timedelta

from homeassistant.const import CONF_SCAN_INTERVAL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .api import ClawdmeterClient
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN, STORAGE_VERSION
from .coordinator import ClawdmeterConfigEntry, ClawdmeterDataUpdateCoordinator

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ClawdmeterConfigEntry) -> bool:
    """Set up Clawdmeter from a config entry."""
    client = ClawdmeterClient(async_get_clientsession(hass))
    coordinator = ClawdmeterDataUpdateCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: ClawdmeterConfigEntry
) -> None:
    """Apply a changed polling interval without dropping the burn-rate window."""
    interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    entry.runtime_data.update_interval = timedelta(seconds=interval)


async def async_unload_entry(hass: HomeAssistant, entry: ClawdmeterConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ClawdmeterConfigEntry) -> None:
    """Drop the persisted burn-rate/peak state when the account is removed."""
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()
