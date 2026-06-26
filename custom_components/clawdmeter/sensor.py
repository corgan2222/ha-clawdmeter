"""Sensor platform for the Clawdmeter integration."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import override

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from .const import ANIMATION_GROUPS, OVERAGE_SEVERITIES, PACE_FRAMES
from .coordinator import (
    ClawdmeterConfigEntry,
    ClawdmeterData,
    ClawdmeterDataUpdateCoordinator,
)
from .entity import ClawdmeterEntity

PARALLEL_UPDATES = 0

PERCENT_PER_HOUR = "%/h"
PERCENT_PER_MINUTE = "%/min"
PACE_RATIO = "x"


@dataclass(frozen=True, kw_only=True)
class ClawdmeterSensorEntityDescription(SensorEntityDescription):
    """Describes a Clawdmeter sensor."""

    value_fn: Callable[[ClawdmeterData], StateType | datetime]
    unit_fn: Callable[[ClawdmeterData], str | None] | None = None


# Sensors carrying a raw value straight from the usage/profile API are filed as
# diagnostics; the locally computed projections are the primary sensors.
SENSORS: tuple[ClawdmeterSensorEntityDescription, ...] = (
    ClawdmeterSensorEntityDescription(
        key="session_usage",
        translation_key="session_usage",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.session_usage,
    ),
    ClawdmeterSensorEntityDescription(
        key="session_reset",
        translation_key="session_reset",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.session_reset,
    ),
    ClawdmeterSensorEntityDescription(
        key="week_usage",
        translation_key="week_usage",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.week_usage,
    ),
    ClawdmeterSensorEntityDescription(
        key="week_reset",
        translation_key="week_reset",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.week_reset,
    ),
    ClawdmeterSensorEntityDescription(
        key="sonnet_usage",
        translation_key="sonnet_usage",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.sonnet_usage,
    ),
    ClawdmeterSensorEntityDescription(
        key="sonnet_reset",
        translation_key="sonnet_reset",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.sonnet_reset,
    ),
    ClawdmeterSensorEntityDescription(
        key="opus_usage",
        translation_key="opus_usage",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.opus_usage,
    ),
    ClawdmeterSensorEntityDescription(
        key="opus_reset",
        translation_key="opus_reset",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.opus_reset,
    ),
    ClawdmeterSensorEntityDescription(
        key="extra_usage",
        translation_key="extra_usage",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.extra_usage,
    ),
    ClawdmeterSensorEntityDescription(
        key="extra_credits",
        translation_key="extra_credits",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        unit_fn=lambda data: data.extra_currency,
        value_fn=lambda data: data.extra_credits,
    ),
    ClawdmeterSensorEntityDescription(
        key="extra_limit",
        translation_key="extra_limit",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        unit_fn=lambda data: data.extra_currency,
        value_fn=lambda data: data.extra_limit,
    ),
    ClawdmeterSensorEntityDescription(
        key="extra_severity",
        translation_key="extra_severity",
        device_class=SensorDeviceClass.ENUM,
        options=OVERAGE_SEVERITIES,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=(
            lambda data: (
                data.extra_severity
                if data.extra_severity in OVERAGE_SEVERITIES
                else None
            )
        ),
    ),
    ClawdmeterSensorEntityDescription(
        key="account",
        translation_key="account",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.account_name,
    ),
    ClawdmeterSensorEntityDescription(
        key="plan",
        translation_key="plan",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.plan,
    ),
    ClawdmeterSensorEntityDescription(
        key="session_reset_in",
        translation_key="session_reset_in",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda data: data.session_reset_in,
    ),
    ClawdmeterSensorEntityDescription(
        key="session_peak_today",
        translation_key="session_peak_today",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda data: data.session_peak_today,
    ),
    ClawdmeterSensorEntityDescription(
        key="week_pace",
        translation_key="week_pace",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda data: data.week_pace,
    ),
    ClawdmeterSensorEntityDescription(
        key="burn_rate_5m",
        translation_key="burn_rate_5m",
        native_unit_of_measurement=PERCENT_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda data: data.burn_rate_fast,
    ),
    ClawdmeterSensorEntityDescription(
        key="burn_rate_30m",
        translation_key="burn_rate_30m",
        native_unit_of_measurement=PERCENT_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda data: data.burn_rate_slow,
    ),
    ClawdmeterSensorEntityDescription(
        key="burn_rate_per_min",
        translation_key="burn_rate_per_min",
        native_unit_of_measurement=PERCENT_PER_MINUTE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda data: data.burn_rate_fast / 60,
    ),
    ClawdmeterSensorEntityDescription(
        key="usage_rate",
        translation_key="usage_rate",
        native_unit_of_measurement=PERCENT_PER_MINUTE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda data: data.usage_rate,
    ),
    ClawdmeterSensorEntityDescription(
        key="time_to_limit",
        translation_key="time_to_limit",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda data: data.time_to_limit,
    ),
    ClawdmeterSensorEntityDescription(
        key="limit_eta",
        translation_key="limit_eta",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda data: data.limit_eta,
    ),
    ClawdmeterSensorEntityDescription(
        key="runway_pace",
        translation_key="runway_pace",
        native_unit_of_measurement=PACE_RATIO,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda data: data.runway_pace,
    ),
    ClawdmeterSensorEntityDescription(
        key="runway_margin",
        translation_key="runway_margin",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda data: data.runway_margin,
    ),
    ClawdmeterSensorEntityDescription(
        key="animation_group",
        translation_key="animation_group",
        device_class=SensorDeviceClass.ENUM,
        options=ANIMATION_GROUPS,
        value_fn=lambda data: data.animation_group,
    ),
    ClawdmeterSensorEntityDescription(
        key="pace_frame",
        translation_key="pace_frame",
        device_class=SensorDeviceClass.ENUM,
        options=PACE_FRAMES,
        value_fn=lambda data: data.pace_frame,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClawdmeterConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Clawdmeter sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        ClawdmeterSensor(coordinator, entry, description) for description in SENSORS
    )


class ClawdmeterSensor(ClawdmeterEntity, SensorEntity):
    """A single Clawdmeter usage or projection sensor."""

    entity_description: ClawdmeterSensorEntityDescription

    def __init__(
        self,
        coordinator: ClawdmeterDataUpdateCoordinator,
        entry: ClawdmeterConfigEntry,
        description: ClawdmeterSensorEntityDescription,
    ) -> None:
        """Initialise the sensor from its description."""
        super().__init__(coordinator, entry, description.key)
        self.entity_description = description

    @property
    @override
    def native_value(self) -> StateType | datetime:
        """Return the current value for this sensor."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    @override
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit, allowing a per-account dynamic unit (currency)."""
        if self.entity_description.unit_fn is not None:
            return self.entity_description.unit_fn(self.coordinator.data)
        return self.entity_description.native_unit_of_measurement
