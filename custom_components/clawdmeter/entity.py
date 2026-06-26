"""Base entity for the Clawdmeter integration."""

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ACCOUNT_NAME, CONF_PLAN, DOMAIN
from .coordinator import ClawdmeterConfigEntry, ClawdmeterDataUpdateCoordinator


def account_title(name: str | None, plan: str | None) -> str:
    """Build a "Claude <name> (<plan>)" label for an account.

    Drives both the config entry title and the device name, so entity ids end up
    as ``claude_<name>_<plan>_<type>`` (e.g. ``claude_stefan_max_extra_usage``).
    """
    parts = ["Claude"]
    if name:
        parts.append(name)
    if plan:
        parts.append(f"({plan})")
    return " ".join(parts)


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
        )
