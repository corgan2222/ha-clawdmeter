"""Base entity for the Clawdmeter integration."""

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ACCOUNT_NAME, CONF_PLAN, DOMAIN
from .coordinator import (
    ClawdmeterConfigEntry,
    ClawdmeterDataUpdateCoordinator,
    account_title,
)


class ClawdmeterEntity(CoordinatorEntity[ClawdmeterDataUpdateCoordinator]):
    """Common base wiring every Clawdmeter entity to its account device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ClawdmeterDataUpdateCoordinator,
        entry: ClawdmeterConfigEntry,
        key: str,
    ) -> None:
        """Initialise the entity and attach it to the account device."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            entry_type=DeviceEntryType.SERVICE,
            manufacturer="Anthropic",
            model=entry.data.get(CONF_PLAN),
            name=account_title(
                entry.data.get(CONF_ACCOUNT_NAME), entry.data.get(CONF_PLAN)
            ),
            sw_version=coordinator.integration_version,
        )
