"""Sensor platform for the Clawdmeter integration."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, override

from homeassistant.components.sensor import (
    DOMAIN as SENSOR_DOMAIN,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from .const import (
    ANIMATION_GROUPS,
    ATTR_ALLOWANCE,
    ATTR_MODEL,
    ATTR_MODELS,
    ATTR_RESETS_AT,
    ATTR_SURFACES,
    ATTR_WINDOW,
    ATTR_WINDOW_STARTED_AT,
    DOMAIN,
    KNOWN_SURFACES,
    OVERAGE_SEVERITIES,
    PACE_FRAMES,
)
from .coordinator import (
    ClawdmeterConfigEntry,
    ClawdmeterData,
    ClawdmeterDataUpdateCoordinator,
)
from .entity import ClawdmeterEntity
from .repairs import async_create_stale_entities_issue

PARALLEL_UPDATES = 0

PERCENT_PER_HOUR = "%/h"
PERCENT_PER_MINUTE = "%/min"
PACE_RATIO = "x"
# Key prefix of the per-surface share sensors. It differs from "surface_" so a
# surface named "usage" cannot collide with the surface_usage sensor.
SHARE_KEY_PREFIX = "share_"


@dataclass(frozen=True, kw_only=True)
class ClawdmeterSensorEntityDescription(SensorEntityDescription):
    """Describes a Clawdmeter sensor."""

    value_fn: Callable[[ClawdmeterData], StateType | datetime]
    unit_fn: Callable[[ClawdmeterData], str | None] | None = None
    attrs_fn: Callable[[ClawdmeterData], dict[str, Any] | None] | None = None
    # Whether this account has ever reported what the sensor shows. Checked at
    # setup, and only for a sensor this install does not have yet.
    exists_fn: Callable[[ClawdmeterDataUpdateCoordinator], bool] = lambda _: True


def _window_attrs(data: ClawdmeterData) -> dict[str, Any] | None:
    """Name the window a session-derived entity is really reporting.

    Everything downstream of the session figure inherits its horizon, so each
    of those entities says so rather than leaving the reader to guess whether
    a five-hour or a weekly number is on screen.
    """
    if not data.session_window:
        return None
    return {ATTR_WINDOW: data.session_window}


def _model_attrs(data: ClawdmeterData) -> dict[str, Any] | None:
    """Describe the per-model weekly cap behind the headline percentage."""
    if data.model_name is None:
        return None
    return {
        ATTR_MODEL: data.model_name,
        ATTR_RESETS_AT: data.model_reset,
        ATTR_ALLOWANCE: data.model_allowance,
        ATTR_MODELS: data.model_usages,
    }


def _surface_attrs(data: ClawdmeterData) -> dict[str, Any] | None:
    """List every surface's share of the week next to the Claude Code one.

    The breakdown's ``as_of`` is left out on purpose: it changes on every poll
    and would make the recorder write a new state each time.
    """
    if not data.surface_usages:
        return None
    return {
        ATTR_SURFACES: data.surface_usages,
        ATTR_WINDOW_STARTED_AT: data.surface_window_start,
    }


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
        attrs_fn=_window_attrs,
    ),
    ClawdmeterSensorEntityDescription(
        key="session_reset",
        translation_key="session_reset",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.session_reset,
        attrs_fn=_window_attrs,
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
        exists_fn=lambda coordinator: "sonnet" in coordinator.models_seen,
    ),
    ClawdmeterSensorEntityDescription(
        key="sonnet_reset",
        translation_key="sonnet_reset",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.sonnet_reset,
        exists_fn=lambda coordinator: "sonnet" in coordinator.models_seen,
    ),
    ClawdmeterSensorEntityDescription(
        key="opus_usage",
        translation_key="opus_usage",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.opus_usage,
        exists_fn=lambda coordinator: "opus" in coordinator.models_seen,
    ),
    ClawdmeterSensorEntityDescription(
        key="opus_reset",
        translation_key="opus_reset",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.opus_reset,
        exists_fn=lambda coordinator: "opus" in coordinator.models_seen,
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
        # Extra usage already reports its percentage; the amounts behind it are
        # rarely wanted, so a new install starts with them off.
        entity_registry_enabled_default=False,
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
        entity_registry_enabled_default=False,
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
        attrs_fn=_window_attrs,
    ),
    ClawdmeterSensorEntityDescription(
        key="session_peak_today",
        translation_key="session_peak_today",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda data: data.session_peak_today,
        attrs_fn=_window_attrs,
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
        attrs_fn=_window_attrs,
    ),
    ClawdmeterSensorEntityDescription(
        key="limit_eta",
        translation_key="limit_eta",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda data: data.limit_eta,
        attrs_fn=_window_attrs,
    ),
    ClawdmeterSensorEntityDescription(
        key="runway_pace",
        translation_key="runway_pace",
        native_unit_of_measurement=PACE_RATIO,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda data: data.runway_pace,
        attrs_fn=_window_attrs,
    ),
    ClawdmeterSensorEntityDescription(
        key="runway_margin",
        translation_key="runway_margin",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda data: data.runway_margin,
        attrs_fn=_window_attrs,
    ),
    ClawdmeterSensorEntityDescription(
        key="animation_group",
        translation_key="animation_group",
        device_class=SensorDeviceClass.ENUM,
        options=ANIMATION_GROUPS,
        value_fn=lambda data: data.animation_group,
    ),
    ClawdmeterSensorEntityDescription(
        key="model_usage",
        translation_key="model_usage",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.model_usage,
        attrs_fn=_model_attrs,
        exists_fn=lambda coordinator: bool(coordinator.models_seen),
    ),
    ClawdmeterSensorEntityDescription(
        key="surface_usage",
        translation_key="surface_usage",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.surface_usage,
        attrs_fn=_surface_attrs,
        exists_fn=lambda coordinator: bool(coordinator.surfaces_seen),
    ),
    ClawdmeterSensorEntityDescription(
        key="pace_frame",
        translation_key="pace_frame",
        device_class=SensorDeviceClass.ENUM,
        options=PACE_FRAMES,
        value_fn=lambda data: data.pace_frame,
        attrs_fn=_window_attrs,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClawdmeterConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Clawdmeter sensors."""
    coordinator = entry.runtime_data
    entity_registry = er.async_get(hass)
    entities: list[SensorEntity] = []
    without_data: list[str] = []
    for description in SENSORS:
        entity_id = entity_registry.async_get_entity_id(
            SENSOR_DOMAIN, DOMAIN, f"{entry.entry_id}_{description.key}"
        )
        # A sensor this install already has is always kept, so an existing
        # setup stays exactly as it was. A new one is held back while the
        # account has never reported what it would show, instead of being
        # created to sit at unknown.
        if description.exists_fn(coordinator):
            entities.append(ClawdmeterSensor(coordinator, entry, description))
        elif entity_id is not None:
            entities.append(ClawdmeterSensor(coordinator, entry, description))
            without_data.append(entity_id)

    # One share sensor per surface key, kept by the same rule as above.
    share_prefix = f"{entry.entry_id}_{SHARE_KEY_PREFIX}"
    registered = {
        reg.unique_id.removeprefix(share_prefix): reg.entity_id
        for reg in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        if reg.domain == SENSOR_DOMAIN and reg.unique_id.startswith(share_prefix)
    }
    without_data.extend(
        entity_id
        for surface, entity_id in registered.items()
        if surface not in coordinator.surfaces_seen
    )
    surfaces_added = set(registered) | coordinator.surfaces_seen
    entities.extend(
        ClawdmeterSurfaceSensor(coordinator, entry, surface)
        for surface in sorted(surfaces_added)
    )
    async_add_entities(entities)

    @callback
    def _add_new_surfaces() -> None:
        """Add a sensor for a surface key that first appears after setup."""
        new = coordinator.surfaces_seen - surfaces_added
        if not new:
            return
        surfaces_added.update(new)
        async_add_entities(
            ClawdmeterSurfaceSensor(coordinator, entry, surface)
            for surface in sorted(new)
        )

    entry.async_on_unload(coordinator.async_add_listener(_add_new_surfaces))

    # After a plan change the user decides whether those go.
    if without_data and coordinator.plan_changed_from is not None:
        async_create_stale_entities_issue(
            hass, entry, coordinator.plan_changed_from, without_data
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
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the description's extra attributes, if it defines any."""
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)

    @property
    @override
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit, allowing a per-account dynamic unit (currency)."""
        if self.entity_description.unit_fn is not None:
            return self.entity_description.unit_fn(self.coordinator.data)
        return self.entity_description.native_unit_of_measurement


class ClawdmeterSurfaceSensor(ClawdmeterEntity, SensorEntity):
    """One surface's share of the week's usage, in points of 100."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: ClawdmeterDataUpdateCoordinator,
        entry: ClawdmeterConfigEntry,
        surface: str,
    ) -> None:
        """Initialise the sensor for one surface key."""
        super().__init__(coordinator, entry, f"{SHARE_KEY_PREFIX}{surface}")
        self._surface = surface
        if surface in KNOWN_SURFACES:
            self._attr_translation_key = f"surface_share_{surface}"
        else:
            self._attr_translation_key = "surface_share"
            self._attr_translation_placeholders = {"surface": surface.replace("_", " ")}

    @property
    @override
    def native_value(self) -> float | None:
        """Return this surface's share, or None when the last poll had none."""
        return self.coordinator.data.surface_usages.get(self._surface)
