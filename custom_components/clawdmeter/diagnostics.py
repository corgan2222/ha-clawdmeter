"""Diagnostics support for the Clawdmeter integration."""

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_ACCESS_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ClawdmeterClient, ClawdmeterError
from .coordinator import ClawdmeterConfigEntry

TO_REDACT = {
    "access_token",
    "refresh_token",
    "account_email",
    "account_name",
    "email",
    "full_name",
    "first_name",
    "last_name",
    "display_name",
    "name",
    "phone_number",
    "id",
    "uuid",
    "account_uuid",
    "organization",
    "organization_name",
    "organization_uuid",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ClawdmeterConfigEntry
) -> dict[str, Any]:
    """Return a redacted dump of the raw API responses and what we keep.

    Sensitive values are redacted but their keys are kept, so it shows exactly
    which fields the API returns versus the ones the integration stores. The
    usage response is the one captured during normal polling (no extra call);
    the profile is fetched once here, only when diagnostics are downloaded.
    """
    coordinator = entry.runtime_data

    client = ClawdmeterClient(async_get_clientsession(hass))
    try:
        raw_profile: Any = await client.async_get_profile_raw(
            entry.data[CONF_ACCESS_TOKEN]
        )
    except ClawdmeterError as err:
        raw_profile = {"error": str(err)}

    return async_redact_data(
        {
            "config_entry": {
                "data": dict(entry.data),
                "options": dict(entry.options),
            },
            "stored": asdict(coordinator.data),
            "raw_usage_api": coordinator.raw_usage or {},
            "raw_profile_api": raw_profile,
        },
        TO_REDACT,
    )
