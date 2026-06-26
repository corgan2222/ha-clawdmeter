"""Test the locally derived Clawdmeter metrics across polls."""

from datetime import UTC, datetime, timedelta
import logging
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.clawdmeter.const import USAGE_ENDPOINT

from . import setup_integration

BURN_5M = "sensor.claude_corgan_max_burn_rate_5_min"
BURN_30M = "sensor.claude_corgan_max_burn_rate_30_min"
USAGE_RATE = "sensor.claude_corgan_max_usage_rate"
ANIMATION_GROUP = "sensor.claude_corgan_max_animation_group"
PACE_FRAME = "sensor.claude_corgan_max_pace_frame"
RUNWAY_PACE = "sensor.claude_corgan_max_runway_pace"
RUNWAY_MARGIN = "sensor.claude_corgan_max_runway_margin"
TIME_TO_LIMIT = "sensor.claude_corgan_max_time_to_limit"
LIMIT_ETA = "sensor.claude_corgan_max_session_limit_eta"
BURN_PER_MIN = "sensor.claude_corgan_max_burn_rate_per_minute"
SESSION_PEAK = "sensor.claude_corgan_max_session_usage_peak_today"
RESETS_IN = "sensor.claude_corgan_max_session_resets_in"
LIMIT_REACHED = "binary_sensor.claude_corgan_max_limit_reached_before_reset"


def _session_payload(
    utilization: float, resets_at: str = "2026-06-25T13:05:00+00:00"
) -> dict[str, Any]:
    """Build a minimal usage payload with a session reset instant."""
    return {"five_hour": {"utilization": utilization, "resets_at": resets_at}}


async def _push_usage(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
    utilization: float,
    resets_at: str = "2026-06-25T13:05:00+00:00",
) -> None:
    """Replace the usage payload and trigger a coordinator refresh."""
    aioclient_mock.clear_requests()
    aioclient_mock.get(USAGE_ENDPOINT, json=_session_payload(utilization, resets_at))
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def test_derived_metrics(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test burn rate, projection and runway derive from the sample window."""
    freezer.move_to("2026-06-25T12:00:00+00:00")
    aioclient_mock.get(USAGE_ENDPOINT, json=_session_payload(10))
    await setup_integration(hass, mock_config_entry)

    # A single sample cannot show a slope yet, so the burn rate reads 0 (not
    # "unknown") and the creature stays idle.
    assert float(hass.states.get(BURN_5M).state) == 0.0
    assert hass.states.get(ANIMATION_GROUP).state == "idle"

    # Five minutes later usage has climbed ten points.
    freezer.tick(timedelta(minutes=5))
    await _push_usage(hass, aioclient_mock, mock_config_entry, 20)

    # 10 points over 5 minutes is 120 %/h on both windows.
    assert float(hass.states.get(BURN_5M).state) == pytest.approx(120.0, abs=0.5)
    assert float(hass.states.get(BURN_30M).state) == pytest.approx(120.0, abs=0.5)
    # 10 points over 5 minutes is 2 %/min, which is the liveliest mood.
    assert float(hass.states.get(USAGE_RATE).state) == pytest.approx(2.0, abs=0.05)
    assert hass.states.get(ANIMATION_GROUP).state == "heavy"
    # 120 %/h is 2 %/min on the fast window.
    assert float(hass.states.get(BURN_PER_MIN).state) == pytest.approx(2.0, abs=0.05)
    # Highest session usage seen today so far.
    assert float(hass.states.get(SESSION_PEAK).state) == pytest.approx(20.0)
    # (100 - 20) / 120 %/h = 40 minutes to the limit, i.e. at 12:45.
    assert float(hass.states.get(TIME_TO_LIMIT).state) == pytest.approx(40.0, abs=1.0)
    assert hass.states.get(LIMIT_ETA).state == "2026-06-25T12:45:00+00:00"
    # The reset is 60 minutes out, so the limit is hit first (40 < 60).
    assert float(hass.states.get(RESETS_IN).state) == pytest.approx(60.0, abs=0.5)
    assert float(hass.states.get(RUNWAY_PACE).state) == pytest.approx(1.5, abs=0.05)
    assert float(hass.states.get(RUNWAY_MARGIN).state) == pytest.approx(-20.0, abs=1.0)
    assert hass.states.get(PACE_FRAME).state == "red"
    assert hass.states.get(LIMIT_REACHED).state == "on"


EXTRA_ENABLED = "binary_sensor.claude_corgan_max_extra_usage_enabled"
EXTRA_USAGE = "sensor.claude_corgan_max_extra_usage"
EXTRA_CREDITS = "sensor.claude_corgan_max_extra_usage_credits"
EXTRA_LIMIT = "sensor.claude_corgan_max_extra_usage_limit"
EXTRA_SEVERITY = "sensor.claude_corgan_max_extra_usage_status"
WEEK_PACE = "sensor.claude_corgan_max_weekly_pace"


@pytest.mark.parametrize(
    ("overage", "enabled", "usage", "credit_state", "limit", "currency", "severity"),
    [
        (
            {
                "spend": {
                    "enabled": True,
                    "percent": 30,
                    "severity": "warning",
                    "used": {"amount_minor": 250, "exponent": 2, "currency": "USD"},
                    "limit": {"amount_minor": 1000, "exponent": 2, "currency": "USD"},
                }
            },
            "on",
            "30",
            "2.5",
            "10.0",
            "USD",
            "warning",
        ),
        (
            {
                "spend": {
                    "enabled": True,
                    "percent": 5,
                    "used": {"amount_minor": 100, "exponent": 2, "currency": "USD"},
                    "limit": 500,
                }
            },
            "on",
            "5",
            "1.0",
            "5.0",
            "USD",
            STATE_UNKNOWN,
        ),
        (
            {
                "extra_usage": {
                    "is_enabled": True,
                    "utilization": 5,
                    "used_credits": 100,
                    "monthly_limit": 500,
                    "decimal_places": 2,
                },
                "spend": {
                    "used": {"amount_minor": 100, "exponent": 2, "currency": "GBP"}
                },
            },
            "on",
            "5",
            "1.0",
            "5.0",
            "GBP",
            STATE_UNKNOWN,
        ),
        (
            {"extra_usage": {"is_enabled": False}},
            "off",
            STATE_UNKNOWN,
            STATE_UNKNOWN,
            STATE_UNKNOWN,
            None,
            STATE_UNKNOWN,
        ),
        (
            {},
            STATE_UNKNOWN,
            STATE_UNKNOWN,
            STATE_UNKNOWN,
            STATE_UNKNOWN,
            None,
            STATE_UNKNOWN,
        ),
    ],
    ids=["spend", "spend-flat-limit", "extra-no-currency", "disabled", "absent"],
)
async def test_overage_variants(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    overage: dict[str, Any],
    enabled: str,
    usage: str,
    credit_state: str,
    limit: str,
    currency: str | None,
    severity: str,
) -> None:
    """Test both overage API shapes plus the disabled and absent cases."""
    freezer.move_to("2026-06-25T12:00:00+00:00")
    aioclient_mock.get(USAGE_ENDPOINT, json=_session_payload(10) | overage)
    await setup_integration(hass, mock_config_entry)

    assert hass.states.get(EXTRA_ENABLED).state == enabled
    assert hass.states.get(EXTRA_USAGE).state == usage
    assert hass.states.get(EXTRA_CREDITS).state == credit_state
    assert hass.states.get(EXTRA_LIMIT).state == limit
    assert hass.states.get(EXTRA_SEVERITY).state == severity
    credits_unit = hass.states.get(EXTRA_CREDITS).attributes.get("unit_of_measurement")
    assert credits_unit == currency
    # No weekly section was supplied, so the weekly pace stays unknown.
    assert hass.states.get(WEEK_PACE).state == STATE_UNKNOWN


async def test_runway_already_over_limit(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test the pace ratio saturates once usage is already at the limit."""
    freezer.move_to("2026-06-25T12:00:00+00:00")
    aioclient_mock.get(USAGE_ENDPOINT, json=_session_payload(95))
    await setup_integration(hass, mock_config_entry)

    freezer.tick(timedelta(minutes=5))
    await _push_usage(hass, aioclient_mock, mock_config_entry, 100)

    assert float(hass.states.get(TIME_TO_LIMIT).state) == pytest.approx(0.0, abs=0.5)
    # Zero time-to-limit means the ETA is now (12:05).
    assert hass.states.get(LIMIT_ETA).state == "2026-06-25T12:05:00+00:00"
    assert float(hass.states.get(RUNWAY_PACE).state) == pytest.approx(99.0, abs=0.5)
    assert hass.states.get(PACE_FRAME).state == "red"


async def test_usage_rate_warmup_window(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test the usage rate warms up before reporting and trims to its window."""
    freezer.move_to("2026-06-25T12:00:00+00:00")
    aioclient_mock.get(USAGE_ENDPOINT, json=_session_payload(10))
    await setup_integration(hass, mock_config_entry)

    # Only one minute of history: the burn rate already reads, but the usage
    # rate is still warming up (so it reports 0) and the mood stays idle.
    freezer.tick(timedelta(minutes=1))
    await _push_usage(hass, aioclient_mock, mock_config_entry, 14)
    assert float(hass.states.get(BURN_5M).state) == pytest.approx(240.0, abs=1.0)
    assert float(hass.states.get(USAGE_RATE).state) == 0.0
    assert hass.states.get(ANIMATION_GROUP).state == "idle"

    # A later sample pushes the oldest point out of the five-minute window.
    freezer.tick(timedelta(minutes=5))
    await _push_usage(hass, aioclient_mock, mock_config_entry, 20)
    assert float(hass.states.get(BURN_5M).state) == pytest.approx(72.0, abs=1.0)
    assert float(hass.states.get(USAGE_RATE).state) == pytest.approx(1.67, abs=0.05)
    assert hass.states.get(ANIMATION_GROUP).state == "heavy"


async def test_idle_state_is_graph_friendly(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test the full idle policy when nobody is using Claude (flat usage).

    Rate metrics read 0 (continuous graphs), the runway is calm, and only the
    projections that have no meaning while idle stay unknown.
    """
    freezer.move_to("2026-06-25T12:00:00+00:00")
    aioclient_mock.get(USAGE_ENDPOINT, json=_session_payload(40))
    await setup_integration(hass, mock_config_entry)

    freezer.tick(timedelta(minutes=5))
    await _push_usage(hass, aioclient_mock, mock_config_entry, 40)

    # Rate metrics are a concrete 0, not "unknown".
    assert float(hass.states.get(BURN_5M).state) == 0.0
    assert float(hass.states.get(BURN_30M).state) == 0.0
    assert float(hass.states.get(BURN_PER_MIN).state) == 0.0
    assert float(hass.states.get(USAGE_RATE).state) == 0.0
    assert float(hass.states.get(RUNWAY_PACE).state) == pytest.approx(0.0)
    # The day's peak is still tracked while idle.
    assert float(hass.states.get(SESSION_PEAK).state) == 40.0
    # Calm verdicts.
    assert hass.states.get(PACE_FRAME).state == "green"
    assert hass.states.get(LIMIT_REACHED).state == "off"
    assert hass.states.get(ANIMATION_GROUP).state == "idle"
    # Projections that are undefined while idle stay unknown on purpose.
    assert hass.states.get(TIME_TO_LIMIT).state == STATE_UNKNOWN
    assert hass.states.get(LIMIT_ETA).state == STATE_UNKNOWN
    assert hass.states.get(RUNWAY_MARGIN).state == STATE_UNKNOWN


async def test_session_peak_resets_at_midnight(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test the daily peak holds the day's high and resets on a new local day."""
    freezer.move_to("2026-06-25T12:00:00+00:00")
    aioclient_mock.get(USAGE_ENDPOINT, json=_session_payload(30))
    await setup_integration(hass, mock_config_entry)
    assert float(hass.states.get(SESSION_PEAK).state) == 30.0

    # Usage climbs: the peak follows.
    freezer.tick(timedelta(hours=2))
    await _push_usage(hass, aioclient_mock, mock_config_entry, 70)
    assert float(hass.states.get(SESSION_PEAK).state) == 70.0

    # Usage drops: the peak holds the day's high.
    freezer.tick(timedelta(hours=2))
    await _push_usage(hass, aioclient_mock, mock_config_entry, 20)
    assert float(hass.states.get(SESSION_PEAK).state) == 70.0

    # A new calendar day: the peak resets to the current value.
    freezer.tick(timedelta(days=1))
    await _push_usage(hass, aioclient_mock, mock_config_entry, 15)
    assert float(hass.states.get(SESSION_PEAK).state) == 15.0


@pytest.mark.parametrize(
    ("utilization", "resets_at", "usage_rate", "pace", "animation", "frame"),
    [
        (40.8, "2026-06-25T13:05:00+00:00", 0.16, 0.162, "normal", "green"),
        (41.25, "2026-06-25T13:05:00+00:00", 0.25, 0.255, "active", "green"),
        (52, "2026-06-25T12:24:00+00:00", 2.4, 0.95, "heavy", "orange"),
    ],
    ids=["normal-green", "active-green", "heavy-orange"],
)
async def test_animation_and_frame_bands(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    utilization: float,
    resets_at: str,
    usage_rate: float,
    pace: float,
    animation: str,
    frame: str,
) -> None:
    """Test the intermediate animation moods and pace-frame bands.

    Pins the underlying usage rate and pace ratio as well as the bands, so a
    calculation drift that stays inside a band still fails the test.
    """
    freezer.move_to("2026-06-25T12:00:00+00:00")
    aioclient_mock.get(USAGE_ENDPOINT, json=_session_payload(40, resets_at))
    await setup_integration(hass, mock_config_entry)

    freezer.tick(timedelta(minutes=5))
    await _push_usage(hass, aioclient_mock, mock_config_entry, utilization, resets_at)

    assert float(hass.states.get(USAGE_RATE).state) == pytest.approx(
        usage_rate, abs=0.01
    )
    assert float(hass.states.get(RUNWAY_PACE).state) == pytest.approx(pace, abs=0.01)
    assert hass.states.get(ANIMATION_GROUP).state == animation
    assert hass.states.get(PACE_FRAME).state == frame


SESSION_USAGE = "sensor.claude_corgan_max_session_usage"
SESSION_RESET = "sensor.claude_corgan_max_session_reset"


@pytest.mark.parametrize(
    ("utilization", "expected"),
    [
        ("20", "20.0"),
        ("oops", STATE_UNKNOWN),
        (True, STATE_UNKNOWN),
        ([], STATE_UNKNOWN),
    ],
    ids=["numeric-string", "garbage-string", "bool", "wrong-type"],
)
async def test_utilization_coercion(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    utilization: object,
    expected: str,
) -> None:
    """Test odd utilization types are coerced or dropped instead of crashing."""
    freezer.move_to("2026-06-25T12:00:00+00:00")
    aioclient_mock.get(USAGE_ENDPOINT, json={"five_hour": {"utilization": utilization}})
    await setup_integration(hass, mock_config_entry)

    assert hass.states.get(SESSION_USAGE).state == expected
    # No resets_at was supplied, so the reset timestamp stays unknown.
    assert hass.states.get(SESSION_RESET).state == STATE_UNKNOWN


async def test_naive_reset_timestamp_is_handled(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test a reset instant without a UTC offset is treated as UTC, not crashed."""
    freezer.move_to("2026-06-25T12:00:00+00:00")
    aioclient_mock.get(USAGE_ENDPOINT, json=_session_payload(10, "2026-06-25T13:05:00"))
    await setup_integration(hass, mock_config_entry)

    assert float(hass.states.get(RESETS_IN).state) == pytest.approx(65.0, abs=0.5)


async def test_missing_session_section(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test a payload without the session block leaves session metrics unknown."""
    freezer.move_to("2026-06-25T12:00:00+00:00")
    aioclient_mock.get(
        USAGE_ENDPOINT,
        json={
            "seven_day": {"utilization": 40, "resets_at": "2026-07-01T12:00:00+00:00"}
        },
    )
    await setup_integration(hass, mock_config_entry)

    assert (
        hass.states.get("sensor.claude_corgan_max_session_usage").state == STATE_UNKNOWN
    )
    assert hass.states.get(RESETS_IN).state == STATE_UNKNOWN
    # The burn rate has no samples to slope, so it reads 0 rather than unknown.
    assert float(hass.states.get(BURN_5M).state) == 0.0
    assert hass.states.get(WEEK_PACE).state != STATE_UNKNOWN
    # With no session usage yet, the day's peak is unknown.
    assert hass.states.get(SESSION_PEAK).state == STATE_UNKNOWN

    # The first real session reading the same day seeds the peak.
    await _push_usage(hass, aioclient_mock, mock_config_entry, 50)
    assert float(hass.states.get(SESSION_PEAK).state) == 50.0


async def test_logs_when_usage_data_missing(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test a clear log explains an empty usage payload, then recovery."""
    aioclient_mock.get(USAGE_ENDPOINT, json={})
    with caplog.at_level(logging.INFO):
        await setup_integration(hass, mock_config_entry)
    assert "no session or weekly data" in caplog.text

    caplog.clear()
    await _push_usage(hass, aioclient_mock, mock_config_entry, 20)
    assert "returning session/weekly data again" in caplog.text


async def test_restores_state_after_restart(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test the burn-rate window and daily peak survive a restart."""
    freezer.move_to("2026-06-25T12:00:00+00:00")
    earlier = datetime(2026, 6, 25, 11, 58, tzinfo=UTC).timestamp()
    hass_storage[f"clawdmeter.{mock_config_entry.entry_id}"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"clawdmeter.{mock_config_entry.entry_id}",
        "data": {
            "samples": [{"ts": earlier, "pct": 10}],
            "peak_value": 88,
            "peak_date": "2026-06-25",
        },
    }
    aioclient_mock.get(USAGE_ENDPOINT, json=_session_payload(20))
    await setup_integration(hass, mock_config_entry)

    # Restored sample + the first poll = a real burn rate at once (10 points over
    # 2 minutes = 300 %/h) instead of the cold-start 0.
    assert float(hass.states.get(BURN_5M).state) == pytest.approx(300.0, abs=5.0)
    # The restored daily peak survives the lower current reading.
    assert float(hass.states.get(SESSION_PEAK).state) == 88.0


@pytest.mark.usefixtures("mock_usage")
async def test_persists_state_on_shutdown(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test the burn-rate window and peak are written for the next restart."""
    freezer.move_to("2026-06-25T12:00:00+00:00")
    await setup_integration(hass, mock_config_entry)

    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()

    data = hass_storage[f"clawdmeter.{mock_config_entry.entry_id}"]["data"]
    assert data["peak_value"] == 20
    assert data["peak_date"] == "2026-06-25"
    assert len(data["samples"]) == 1


async def test_history_is_trimmed_to_retention(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test samples older than the retention window are dropped."""
    freezer.move_to("2026-06-25T12:00:00+00:00")
    aioclient_mock.get(USAGE_ENDPOINT, json=_session_payload(10))
    await setup_integration(hass, mock_config_entry)

    for utilization in (30, 50):
        freezer.tick(timedelta(minutes=20))
        await _push_usage(hass, aioclient_mock, mock_config_entry, utilization)

    # The first (now >30 min old) sample is trimmed, so the 30 min rate spans
    # only the two retained points: 20 points over 20 minutes is 60 %/h.
    assert float(hass.states.get(BURN_30M).state) == pytest.approx(60.0, abs=0.5)


async def test_session_reset_flushes_window(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test a sharp usage drop restarts the burn-rate window, not going negative."""
    freezer.move_to("2026-06-25T12:00:00+00:00")
    aioclient_mock.get(USAGE_ENDPOINT, json=_session_payload(80))
    await setup_integration(hass, mock_config_entry)

    freezer.tick(timedelta(minutes=5))
    await _push_usage(hass, aioclient_mock, mock_config_entry, 90)
    assert float(hass.states.get(BURN_5M).state) == pytest.approx(120.0, abs=0.5)

    # The session resets: usage collapses to ~0, flushing the window to one
    # point, so both burn rates fall back to 0 and the creature calms down.
    freezer.tick(timedelta(minutes=5))
    await _push_usage(hass, aioclient_mock, mock_config_entry, 3)
    assert float(hass.states.get(BURN_5M).state) == 0.0
    assert float(hass.states.get(BURN_30M).state) == 0.0
    assert hass.states.get(ANIMATION_GROUP).state == "idle"


@pytest.mark.parametrize(
    ("drop_to", "burn30_after_recovery"),
    [(45.0, 60.0), (44.9, 181.0)],
    ids=["exactly-5-points-kept", "over-5-points-flushed"],
)
async def test_reset_drop_threshold_boundary(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    drop_to: float,
    burn30_after_recovery: float,
) -> None:
    """Test the strict 5-point reset threshold.

    A 5.0-point dip keeps the pre-dip sample, so the 30 min slope after a
    recovery still spans it; a 5.1-point dip flushes the window, leaving a
    steeper post-reset slope.
    """
    freezer.move_to("2026-06-25T12:00:00+00:00")
    aioclient_mock.get(USAGE_ENDPOINT, json=_session_payload(50))
    await setup_integration(hass, mock_config_entry)

    freezer.tick(timedelta(minutes=5))
    await _push_usage(hass, aioclient_mock, mock_config_entry, drop_to)
    freezer.tick(timedelta(minutes=5))
    await _push_usage(hass, aioclient_mock, mock_config_entry, 60)

    assert float(hass.states.get(BURN_30M).state) == pytest.approx(
        burn30_after_recovery, abs=2.0
    )
