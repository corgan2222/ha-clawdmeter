"""Test the Clawdmeter sensor and binary sensor platforms."""

from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
import pytest
from syrupy.assertion import SnapshotAssertion

from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from . import setup_integration

from pytest_homeassistant_custom_component.common import MockConfigEntry, snapshot_platform

DIAGNOSTIC = "sensor.claude_corgan_max_session_usage"
COMPUTED = "sensor.claude_corgan_max_burn_rate_5_min"
ACCOUNT = "sensor.claude_corgan_max_account"
PLAN = "sensor.claude_corgan_max_plan"
USAGE_RATE = "sensor.claude_corgan_max_usage_rate"


@pytest.mark.usefixtures("mock_usage")
@pytest.mark.parametrize("platform", [Platform.SENSOR, Platform.BINARY_SENSOR])
async def test_all_entities(
    hass: HomeAssistant,
    snapshot: SnapshotAssertion,
    entity_registry: er.EntityRegistry,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    platform: Platform,
) -> None:
    """Test all entities are created and match the snapshot.

    This pins entity structure and the single-poll states (derived rolling-window
    metrics are still unknown here); their computed values are covered in
    test_coordinator.
    """
    freezer.move_to("2026-06-25T12:00:00+00:00")
    with patch("custom_components.clawdmeter.PLATFORMS", [platform]):
        await setup_integration(hass, mock_config_entry)

    await snapshot_platform(hass, entity_registry, snapshot, mock_config_entry.entry_id)


@pytest.mark.usefixtures("mock_usage")
async def test_account_entities_and_categories(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test the account/plan sensors and the diagnostic vs computed split."""
    await setup_integration(hass, mock_config_entry)

    assert hass.states.get(ACCOUNT).state == "Corgan"
    assert hass.states.get(PLAN).state == "Max"

    # Raw API values (incl. account/plan) are diagnostics.
    assert (
        entity_registry.async_get(ACCOUNT).entity_category is EntityCategory.DIAGNOSTIC
    )
    assert entity_registry.async_get(PLAN).entity_category is EntityCategory.DIAGNOSTIC
    assert (
        entity_registry.async_get(DIAGNOSTIC).entity_category
        is EntityCategory.DIAGNOSTIC
    )
    # Computed projections are primary sensors (no category).
    assert entity_registry.async_get(COMPUTED).entity_category is None
    assert entity_registry.async_get(USAGE_RATE).entity_category is None
