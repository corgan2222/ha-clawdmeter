"""Binary sensor platform for the Clawdmeter integration."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import override

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import (
    ClawdmeterConfigEntry,
    ClawdmeterData,
    ClawdmeterDataUpdateCoordinator,
)
from .entity import ClawdmeterEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class ClawdmeterBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes a Clawdmeter binary sensor."""

    value_fn: Callable[[ClawdmeterData], bool | None]


BINARY_SENSORS: tuple[ClawdmeterBinarySensorEntityDescription, ...] = (
    ClawdmeterBinarySensorEntityDescription(
        key="extra_enabled",
        translation_key="extra_enabled",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.extra_enabled,
    ),
    ClawdmeterBinarySensorEntityDescription(
        key="runway_over",
        translation_key="runway_over",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda data: data.runway_over,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClawdmeterConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Clawdmeter binary sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        ClawdmeterBinarySensor(coordinator, entry, description)
        for description in BINARY_SENSORS
    )


class ClawdmeterBinarySensor(ClawdmeterEntity, BinarySensorEntity):
    """A single Clawdmeter binary sensor."""

    entity_description: ClawdmeterBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: ClawdmeterDataUpdateCoordinator,
        entry: ClawdmeterConfigEntry,
        description: ClawdmeterBinarySensorEntityDescription,
    ) -> None:
        """Initialise the binary sensor from its description."""
        super().__init__(coordinator, entry, description.key)
        self.entity_description = description

    @property
    @override
    def is_on(self) -> bool | None:
        """Return the current state of the binary sensor."""
        return self.entity_description.value_fn(self.coordinator.data)
