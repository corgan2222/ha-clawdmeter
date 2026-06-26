"""Common fixtures for the Clawdmeter tests."""

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.clawdmeter.api import OAuthChallenge
from custom_components.clawdmeter.const import (
    CONF_ACCOUNT_EMAIL,
    CONF_ACCOUNT_NAME,
    CONF_EXPIRES_AT,
    CONF_PLAN,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    OAUTH_TOKEN_URL,
    PROFILE_ENDPOINT,
    USAGE_ENDPOINT,
)
from homeassistant.const import CONF_ACCESS_TOKEN

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

ACCOUNT_EMAIL = "corgan@example.com"

# Deterministic PKCE/state so config-flow tests can submit a matching code.
TEST_STATE = "fixed-test-state"
VALID_CODE = f"auth-code#{TEST_STATE}"

# An absolute expiry far in the future so the coordinator never refreshes the
# token during a test unless it explicitly arranges for it.
NEVER_EXPIRES = 9999999999.0


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> Generator[None]:
    """Load the clawdmeter custom integration in every test (PHACC)."""
    yield


@pytest.fixture(autouse=True)
def _mock_storage(hass_storage: dict[str, Any]) -> None:
    """Back the coordinator's Store with in-memory storage in every test."""


@pytest.fixture
def mock_pkce() -> Generator[None]:
    """Pin the PKCE challenge and state to deterministic values."""
    challenge = OAuthChallenge("verifier", "challenge", TEST_STATE)
    with patch(
        "custom_components.clawdmeter.config_flow.OAuthChallenge.create",
        return_value=challenge,
    ):
        yield


TOKEN_RESPONSE = {
    "access_token": "access-token",
    "refresh_token": "refresh-token",
    "expires_in": 3600,
}
PROFILE_RESPONSE = {
    "account": {
        "display_name": "Corgan",
        "email": ACCOUNT_EMAIL,
        "has_claude_max": True,
    }
}


def register_oauth_success(
    aioclient_mock: AiohttpClientMocker, profile: dict[str, Any] | None = None
) -> None:
    """Register a working token exchange and profile lookup."""
    aioclient_mock.post(OAUTH_TOKEN_URL, json=TOKEN_RESPONSE)
    aioclient_mock.get(PROFILE_ENDPOINT, json=profile or PROFILE_RESPONSE)


@pytest.fixture
def usage_payload() -> dict[str, Any]:
    """Return a representative usage API response."""
    return {
        "five_hour": {
            "utilization": 20,
            "resets_at": "2026-06-25T15:00:00+00:00",
        },
        "seven_day": {
            "utilization": 40,
            "resets_at": "2026-07-01T12:00:00+00:00",
        },
        "seven_day_sonnet": {
            "utilization": 15,
            "resets_at": "2026-06-30T12:00:00+00:00",
        },
        "seven_day_opus": {
            "utilization": 8,
            "resets_at": "2026-06-29T12:00:00+00:00",
        },
        "extra_usage": {
            "is_enabled": True,
            "utilization": 12,
            "used_credits": 1234,
            "monthly_limit": 5000,
            "decimal_places": 2,
            "currency": "EUR",
        },
        "spend": {
            "enabled": True,
            "percent": 12,
            "severity": "normal",
            "used": {"amount_minor": 1234, "exponent": 2, "currency": "EUR"},
            "limit": {"amount_minor": 5000, "exponent": 2, "currency": "EUR"},
        },
    }


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a configured Clawdmeter entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Claude Corgan (Max)",
        entry_id="01JQ8R9XYZ0123456789ABCDEF",
        unique_id=ACCOUNT_EMAIL,
        data={
            CONF_ACCESS_TOKEN: "access-token",
            CONF_REFRESH_TOKEN: "refresh-token",
            CONF_EXPIRES_AT: NEVER_EXPIRES,
            CONF_ACCOUNT_NAME: "Corgan",
            CONF_ACCOUNT_EMAIL: ACCOUNT_EMAIL,
            CONF_PLAN: "Max",
        },
    )


@pytest.fixture
def mock_usage(
    aioclient_mock: AiohttpClientMocker, usage_payload: dict[str, Any]
) -> AiohttpClientMocker:
    """Mock the usage endpoint."""
    aioclient_mock.get(USAGE_ENDPOINT, json=usage_payload)
    return aioclient_mock


@pytest.fixture
def mock_oauth(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    """Mock the token exchange and profile endpoints used by the config flow."""
    register_oauth_success(aioclient_mock)
    return aioclient_mock


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Override async_setup_entry for config flow tests."""
    with patch(
        "custom_components.clawdmeter.async_setup_entry", return_value=True
    ) as mock_setup_entry:
        yield mock_setup_entry
