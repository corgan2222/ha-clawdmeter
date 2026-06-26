"""Test the Clawdmeter setup and coordinator lifecycle."""

from typing import Any

from aiohttp import ClientError
import pytest

from custom_components.clawdmeter.const import (
    CONF_EXPIRES_AT,
    CONF_REFRESH_TOKEN,
    OAUTH_TOKEN_URL,
    USAGE_ENDPOINT,
)
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_ACCESS_TOKEN
from homeassistant.core import HomeAssistant

from . import setup_integration
from .conftest import TOKEN_RESPONSE

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker


@pytest.mark.usefixtures("mock_usage")
async def test_setup_and_unload(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Test a config entry loads its platforms and unloads cleanly."""
    await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_config_entry.runtime_data is not None

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED


async def test_auth_failure_starts_reauth(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test a rejected token aborts setup and starts a reauth flow."""
    aioclient_mock.get(USAGE_ENDPOINT, status=401)
    await setup_integration(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == SOURCE_REAUTH


@pytest.mark.parametrize("usage_kwargs", [{"exc": ClientError()}, {"status": 500}])
async def test_connection_failure_is_retried(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    usage_kwargs: dict[str, object],
) -> None:
    """Test a transient error puts the entry into a retry state."""
    aioclient_mock.get(USAGE_ENDPOINT, **usage_kwargs)
    await setup_integration(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


@pytest.mark.parametrize(
    ("token_kwargs", "expected_state"),
    [
        ({"status": 401}, ConfigEntryState.SETUP_ERROR),
        ({"exc": ClientError()}, ConfigEntryState.SETUP_RETRY),
    ],
)
@pytest.mark.usefixtures("mock_usage")
async def test_token_refresh_failure(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    token_kwargs: dict[str, object],
    expected_state: ConfigEntryState,
) -> None:
    """Test a failing token refresh aborts or retries setup as appropriate."""
    aioclient_mock.post(OAUTH_TOKEN_URL, **token_kwargs)
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={**mock_config_entry.data, CONF_EXPIRES_AT: 0},
    )

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is expected_state


async def test_missing_refresh_token_starts_reauth(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Test an expired token with no refresh token forces a reauth."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={**mock_config_entry.data, CONF_REFRESH_TOKEN: "", CONF_EXPIRES_AT: 0},
    )

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR


async def test_remove_entry_clears_storage(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """Test the persisted burn-rate/peak state is removed with the entry."""
    key = f"clawdmeter.{mock_config_entry.entry_id}"
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {"samples": [], "peak_value": None, "peak_date": None},
    }
    mock_config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage


@pytest.mark.usefixtures("mock_usage")
async def test_expired_token_is_refreshed(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test an expired access token is refreshed before fetching usage."""
    aioclient_mock.post(OAUTH_TOKEN_URL, json=TOKEN_RESPONSE)
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={
            **mock_config_entry.data,
            CONF_ACCESS_TOKEN: "stale-token",
            CONF_EXPIRES_AT: 0,
        },
    )

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_config_entry.data[CONF_ACCESS_TOKEN] == "access-token"
