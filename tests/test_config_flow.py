"""Test the Clawdmeter config flow."""

from datetime import timedelta
from typing import Any

from aiohttp import ClientError
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.clawdmeter.const import (
    CONF_ACCOUNT_EMAIL,
    CONF_ACCOUNT_NAME,
    CONF_AUTH_CODE,
    CONF_PLAN,
    DOMAIN,
    OAUTH_TOKEN_URL,
)

from . import setup_integration
from .conftest import ACCOUNT_EMAIL, VALID_CODE, register_oauth_success

pytestmark = pytest.mark.usefixtures("mock_pkce")


async def _submit_code(hass: HomeAssistant, flow_id: str, code: str) -> dict[str, Any]:
    """Submit an authorization code into a running flow."""
    return await hass.config_entries.flow.async_configure(
        flow_id, {CONF_AUTH_CODE: code}
    )


@pytest.mark.usefixtures("mock_oauth", "mock_setup_entry")
async def test_user_flow(hass: HomeAssistant) -> None:
    """Test the happy-path user flow creates an entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert "url" in result["description_placeholders"]

    result = await _submit_code(hass, result["flow_id"], VALID_CODE)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Claude Corgan (Max)"
    assert result["result"].unique_id == ACCOUNT_EMAIL
    assert result["data"][CONF_ACCESS_TOKEN] == "access-token"
    assert result["data"][CONF_ACCOUNT_NAME] == "Corgan"
    assert result["data"][CONF_ACCOUNT_EMAIL] == ACCOUNT_EMAIL
    assert result["data"][CONF_PLAN] == "Max"


@pytest.mark.usefixtures("mock_setup_entry")
async def test_missing_code_recovers(
    hass: HomeAssistant, mock_oauth: AiohttpClientMocker
) -> None:
    """Test an empty code shows an error and the flow then recovers."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await _submit_code(hass, result["flow_id"], "   ")
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "missing_code"}

    result = await _submit_code(hass, result["flow_id"], VALID_CODE)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize(
    ("exchange_kwargs", "expected_error"),
    [
        ({"status": 400}, "exchange_failed"),
        ({"json": {"refresh_token": "no-access-token"}}, "exchange_failed"),
        ({"exc": ClientError()}, "cannot_connect"),
    ],
)
@pytest.mark.usefixtures("mock_setup_entry")
async def test_exchange_errors_recover(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    exchange_kwargs: dict[str, Any],
    expected_error: str,
) -> None:
    """Test token-exchange failures surface an error and then recover."""
    aioclient_mock.post(OAUTH_TOKEN_URL, **exchange_kwargs)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await _submit_code(hass, result["flow_id"], VALID_CODE)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected_error}

    # Restore working endpoints and confirm the flow can still finish.
    aioclient_mock.clear_requests()
    register_oauth_success(aioclient_mock)
    result = await _submit_code(hass, result["flow_id"], VALID_CODE)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize("code", ["auth-code#not-the-state", "auth-code"])
@pytest.mark.usefixtures("mock_setup_entry")
async def test_state_is_required(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, code: str
) -> None:
    """Test a code with a missing or mismatched state is refused (CSRF guard)."""
    register_oauth_success(aioclient_mock)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await _submit_code(hass, result["flow_id"], code)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "exchange_failed"}


@pytest.mark.parametrize(
    ("profile", "expected_title", "expected_unique_id"),
    [
        (
            {
                "account": {
                    "display_name": "Bob",
                    "email": "bob@x.com",
                    "has_claude_pro": True,
                }
            },
            "Claude Bob (Pro)",
            "bob@x.com",
        ),
        (
            {"account": {"full_name": "Sam", "email": "sam@x.com"}},
            "Claude Sam",
            "sam@x.com",
        ),
        ({"account": {"has_claude_max": True}}, "Claude (Max)", DOMAIN),
        ({"account": {}}, "Claude", DOMAIN),
    ],
)
@pytest.mark.usefixtures("mock_setup_entry")
async def test_account_variants(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    profile: dict[str, Any],
    expected_title: str,
    expected_unique_id: str,
) -> None:
    """Test the entry title and unique id derive from the profile."""
    register_oauth_success(aioclient_mock, profile)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await _submit_code(hass, result["flow_id"], VALID_CODE)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == expected_title
    assert result["result"].unique_id == expected_unique_id


@pytest.mark.usefixtures("mock_oauth")
async def test_duplicate_account_aborts(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Test configuring the same account twice aborts."""
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await _submit_code(hass, result["flow_id"], VALID_CODE)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.usefixtures("mock_oauth", "mock_setup_entry")
async def test_reauth_flow(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Test re-authentication updates the stored tokens."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={**mock_config_entry.data, CONF_ACCESS_TOKEN: "stale"},
    )

    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await _submit_code(hass, result["flow_id"], VALID_CODE)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_ACCESS_TOKEN] == "access-token"


@pytest.mark.usefixtures("mock_oauth", "mock_setup_entry")
async def test_reconfigure_flow(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Test reconfiguring re-authorizes and updates the entry."""
    mock_config_entry.add_to_hass(hass)

    result = await mock_config_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    result = await _submit_code(hass, result["flow_id"], VALID_CODE)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.data[CONF_ACCESS_TOKEN] == "access-token"


@pytest.mark.usefixtures("mock_usage")
async def test_options_flow_sets_interval(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Test the options flow updates the polling interval live."""
    await setup_integration(hass, mock_config_entry)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 120}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options[CONF_SCAN_INTERVAL] == 120
    assert mock_config_entry.runtime_data.update_interval == timedelta(seconds=120)
