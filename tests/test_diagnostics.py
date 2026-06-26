"""Test Clawdmeter diagnostics."""

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from syrupy.assertion import SnapshotAssertion

from custom_components.clawdmeter.const import PROFILE_ENDPOINT
from custom_components.clawdmeter.diagnostics import (
    async_get_config_entry_diagnostics,
)

from . import setup_integration
from .conftest import PROFILE_RESPONSE


@pytest.mark.usefixtures("mock_usage")
async def test_diagnostics(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    snapshot: SnapshotAssertion,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test the redacted dump of raw API responses versus stored data."""
    freezer.move_to("2026-06-25T12:00:00+00:00")
    aioclient_mock.get(PROFILE_ENDPOINT, json=PROFILE_RESPONSE)
    await setup_integration(hass, mock_config_entry)

    assert await async_get_config_entry_diagnostics(hass, mock_config_entry) == snapshot


@pytest.mark.usefixtures("mock_usage")
async def test_diagnostics_profile_unavailable(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test a failed profile fetch is reported instead of breaking the dump."""
    aioclient_mock.get(PROFILE_ENDPOINT, status=401)
    await setup_integration(hass, mock_config_entry)

    result = await async_get_config_entry_diagnostics(hass, mock_config_entry)
    assert "error" in result["raw_profile_api"]
    assert result["raw_usage_api"]["five_hour"]["utilization"] == 20
