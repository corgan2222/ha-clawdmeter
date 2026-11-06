"""Test the repair offered after a plan change."""

from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.clawdmeter.const import (
    CONF_PLAN,
    DOMAIN,
    PROFILE_ENDPOINT,
    USAGE_ENDPOINT,
)

from . import setup_integration
from .conftest import PROFILE_RESPONSE

OPUS_USAGE = "sensor.claude_corgan_max_weekly_opus_usage"

# An account without a five-hour window and without a per-model cap.
WEEKLY_ONLY = {
    "seven_day": {"utilization": 16, "resets_at": "2026-06-30T12:00:00+00:00"}
}
ULTRA_PROFILE = {**PROFILE_RESPONSE, "organization": {"plan_display_name": "Ultra"}}


def _register_opus(registry: er.EntityRegistry, entry: MockConfigEntry) -> None:
    """Register the Opus sensor an earlier version created for this entry."""
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_opus_usage",
        suggested_object_id="claude_corgan_max_weekly_opus_usage",
        config_entry=entry,
    )


def _issue_id(entry: MockConfigEntry) -> str:
    return f"stale_entities_{entry.entry_id}"


async def _start_fix_flow(hass: HomeAssistant, entry: MockConfigEntry) -> Any:
    """Open the fix flow of the entry's issue the way the frontend does."""
    assert await async_setup_component(hass, "repairs", {})
    return await repairs_flow_manager(hass).async_init(
        DOMAIN, data={"issue_id": _issue_id(entry)}
    )


@pytest.mark.account_refresh
async def test_plan_change_offers_to_remove_sensors_without_data(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    entity_registry: er.EntityRegistry,
    issue_registry: ir.IssueRegistry,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test a plan change lists the sensors left without data, removed on confirm."""
    freezer.move_to("2026-06-25T12:00:00+00:00")
    mock_config_entry.add_to_hass(hass)
    _register_opus(entity_registry, mock_config_entry)
    aioclient_mock.get(PROFILE_ENDPOINT, json=ULTRA_PROFILE)
    aioclient_mock.get(USAGE_ENDPOINT, json=WEEKLY_ONLY)
    await setup_integration(hass, mock_config_entry)

    issue = issue_registry.async_get_issue(DOMAIN, _issue_id(mock_config_entry))
    assert issue is not None
    assert issue.is_fixable
    assert issue.translation_placeholders["old_plan"] == "Max"
    assert issue.translation_placeholders["new_plan"] == "Ultra"
    assert OPUS_USAGE in issue.translation_placeholders["entities"]
    # Nothing is removed before the user says so.
    assert hass.states.get(OPUS_USAGE) is not None

    result = await _start_fix_flow(hass, mock_config_entry)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["confirm", "ignore"]
    result = await repairs_flow_manager(hass).async_configure(
        result["flow_id"], {"next_step_id": "confirm"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    assert entity_registry.async_get(OPUS_USAGE) is None
    assert issue_registry.async_get_issue(DOMAIN, _issue_id(mock_config_entry)) is None


@pytest.mark.account_refresh
async def test_keeping_the_sensors_ignores_the_issue(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    entity_registry: er.EntityRegistry,
    issue_registry: ir.IssueRegistry,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test choosing to keep the sensors leaves them and ignores the issue."""
    mock_config_entry.add_to_hass(hass)
    _register_opus(entity_registry, mock_config_entry)
    aioclient_mock.get(PROFILE_ENDPOINT, json=ULTRA_PROFILE)
    aioclient_mock.get(USAGE_ENDPOINT, json=WEEKLY_ONLY)
    await setup_integration(hass, mock_config_entry)

    result = await _start_fix_flow(hass, mock_config_entry)
    result = await repairs_flow_manager(hass).async_configure(
        result["flow_id"], {"next_step_id": "ignore"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "ignored"

    assert entity_registry.async_get(OPUS_USAGE) is not None
    issue = issue_registry.async_get_issue(DOMAIN, _issue_id(mock_config_entry))
    assert issue.dismissed_version is not None


@pytest.mark.account_refresh
async def test_no_issue_without_a_plan_change(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    entity_registry: er.EntityRegistry,
    issue_registry: ir.IssueRegistry,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test an install whose plan did not change is left exactly as it was."""
    mock_config_entry.add_to_hass(hass)
    _register_opus(entity_registry, mock_config_entry)
    aioclient_mock.get(PROFILE_ENDPOINT, json=PROFILE_RESPONSE)
    aioclient_mock.get(USAGE_ENDPOINT, json=WEEKLY_ONLY)
    await setup_integration(hass, mock_config_entry)

    assert issue_registry.async_get_issue(DOMAIN, _issue_id(mock_config_entry)) is None
    assert hass.states.get(OPUS_USAGE) is not None


async def test_plan_change_learns_the_windows_again(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test a Max account moved to Ultra drops the five-hour window it remembered.

    The window is remembered so a poll that omits it between resets is not
    mistaken for a plan without one. After a plan change that memory is wrong,
    and keeping it would leave the session sensors empty for good. This path
    also covers a change made through reconfigure, which rewrites the entry.
    """
    freezer.move_to("2026-06-25T12:00:00+00:00")
    now = dt_util.utcnow()
    hass_storage[f"clawdmeter.{mock_config_entry.entry_id}"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"clawdmeter.{mock_config_entry.entry_id}",
        "data": {
            # Two minutes old, from the five-hour window the old plan had.
            "samples": [{"ts": now.timestamp() - 120, "pct": 3}],
            "peak_value": 80,
            "peak_date": "2026-06-25",
            "session_seen": True,
            "plan": "Max",
        },
    }
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, data={**mock_config_entry.data, CONF_PLAN: "Ultra"}
    )
    aioclient_mock.get(USAGE_ENDPOINT, json=WEEKLY_ONLY)
    await setup_integration(hass, mock_config_entry)

    # Created fresh under the new plan, so the ids carry it.
    session = hass.states.get("sensor.claude_corgan_ultra_session_usage")
    assert float(session.state) == 16.0
    assert session.attributes["window"] == "weekly"
    ttl = hass.states.get("sensor.claude_corgan_ultra_time_to_limit")
    assert ttl.state not in ("unknown", "unavailable")
    # Neither the old window's sample nor its peak carries over: 3 % two
    # minutes ago against 16 % now would read as a burn of 390 %/h.
    burn = hass.states.get("sensor.claude_corgan_ultra_burn_rate_30_min")
    assert float(burn.state) == 0.0
    peak = hass.states.get("sensor.claude_corgan_ultra_session_usage_peak_today")
    assert float(peak.state) == 16.0


@pytest.mark.account_refresh
async def test_plan_change_survives_a_failed_first_start(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    entity_registry: er.EntityRegistry,
    issue_registry: ir.IssueRegistry,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """Test a plan change is still handled when the first start fails.

    The profile lookup updates the entry right away. If the usage poll after
    it fails, the retried setup finds the new plan already in the entry, so
    the change must have been pinned before the lookup.
    """
    hass_storage[f"clawdmeter.{mock_config_entry.entry_id}"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"clawdmeter.{mock_config_entry.entry_id}",
        # Written by 0.1.0, which did not store the plan yet.
        "data": {"samples": [], "session_seen": True},
    }
    mock_config_entry.add_to_hass(hass)
    _register_opus(entity_registry, mock_config_entry)
    aioclient_mock.get(PROFILE_ENDPOINT, json=ULTRA_PROFILE)
    aioclient_mock.get(USAGE_ENDPOINT, status=500)
    await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert mock_config_entry.data[CONF_PLAN] == "Ultra"

    aioclient_mock.clear_requests()
    aioclient_mock.get(PROFILE_ENDPOINT, json=ULTRA_PROFILE)
    aioclient_mock.get(USAGE_ENDPOINT, json=WEEKLY_ONLY)
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    issue = issue_registry.async_get_issue(DOMAIN, _issue_id(mock_config_entry))
    assert issue is not None
    assert issue.translation_placeholders["old_plan"] == "Max"
    session_id = entity_registry.async_get_entity_id(
        "sensor", DOMAIN, f"{mock_config_entry.entry_id}_session_usage"
    )
    assert hass.states.get(session_id).attributes["window"] == "weekly"
