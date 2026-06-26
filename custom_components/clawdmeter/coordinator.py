"""Data update coordinator for the Clawdmeter integration."""

from collections import deque
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import time
from typing import Any, override

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import ClawdmeterAuthError, ClawdmeterClient, ClawdmeterConnectionError
from .const import (
    BURN_WINDOW_FAST,
    BURN_WINDOW_SLOW,
    CONF_ACCOUNT_NAME,
    CONF_EXPIRES_AT,
    CONF_PLAN,
    CONF_REFRESH_TOKEN,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    FRAME_GREEN,
    FRAME_ORANGE,
    FRAME_RED,
    GROUP_ACTIVE,
    GROUP_ACTIVE_RATE,
    GROUP_HEAVY,
    GROUP_HEAVY_RATE,
    GROUP_IDLE,
    GROUP_NORMAL,
    GROUP_NORMAL_RATE,
    LOGGER,
    PACE_GREEN_MAX,
    PACE_ORANGE_MAX,
    RATE_WARMUP,
    RESET_DROP_POINTS,
    SAMPLE_RETENTION,
    STORAGE_VERSION,
    STORE_SAVE_DELAY,
    TOKEN_EXPIRY_MARGIN,
)

type ClawdmeterConfigEntry = ConfigEntry[ClawdmeterDataUpdateCoordinator]

WEEK_SECONDS = 7 * 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class ClawdmeterData:
    """Parsed usage plus every locally derived projection."""

    account_name: str | None
    plan: str | None
    session_usage: float | None
    session_reset: datetime | None
    session_reset_in: float | None
    session_peak_today: float | None
    week_usage: float | None
    week_pace: float | None
    week_reset: datetime | None
    sonnet_usage: float | None
    sonnet_reset: datetime | None
    opus_usage: float | None
    opus_reset: datetime | None
    extra_enabled: bool | None
    extra_usage: float | None
    extra_credits: float | None
    extra_limit: float | None
    extra_currency: str | None
    extra_severity: str | None
    # Rate metrics are 0 (never None) when idle, so graphs stay continuous.
    burn_rate_fast: float
    burn_rate_slow: float
    usage_rate: float
    animation_group: str
    time_to_limit: float | None
    limit_eta: datetime | None
    runway_pace: float | None
    runway_margin: float | None
    runway_over: bool | None
    pace_frame: str | None


@dataclass(slots=True)
class _Sample:
    """One timestamped usage reading used for the rolling burn rate."""

    ts: float
    pct: float


class ClawdmeterDataUpdateCoordinator(DataUpdateCoordinator[ClawdmeterData]):
    """Poll the usage API and turn raw figures into Clawdmeter metrics."""

    config_entry: ClawdmeterConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ClawdmeterConfigEntry,
        client: ClawdmeterClient,
    ) -> None:
        """Initialise the coordinator."""
        interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
            config_entry=entry,
        )
        self._client = client
        self._samples: deque[_Sample] = deque()
        # The last raw usage response, kept for diagnostics (no extra API call).
        self.raw_usage: dict[str, Any] | None = None
        # Highest session usage seen so far on the current local calendar day.
        self._peak_value: float | None = None
        self._peak_date: date | None = None
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._logged_empty = False

    @override
    async def _async_setup(self) -> None:
        """Restore the burn-rate window and daily peak from the last run."""
        stored = await self._store.async_load()
        if stored is None:
            return
        self._samples = deque(
            _Sample(item["ts"], item["pct"]) for item in stored.get("samples", [])
        )
        self._peak_value = stored.get("peak_value")
        peak_date = stored.get("peak_date")
        self._peak_date = date.fromisoformat(peak_date) if peak_date else None

    @callback
    def _persisted_state(self) -> dict[str, Any]:
        """Return the in-memory state to keep across a restart."""
        return {
            "samples": [{"ts": s.ts, "pct": s.pct} for s in self._samples],
            "peak_value": self._peak_value,
            "peak_date": self._peak_date.isoformat() if self._peak_date else None,
        }

    @override
    async def _async_update_data(self) -> ClawdmeterData:
        """Refresh the token if needed, fetch usage and derive metrics."""
        await self._async_ensure_token()
        token = self.config_entry.data[CONF_ACCESS_TOKEN]
        try:
            raw = await self._client.async_get_usage(token)
        except ClawdmeterAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except ClawdmeterConnectionError as err:
            raise UpdateFailed(str(err)) from err
        LOGGER.debug("Fetched usage payload: %s", raw)
        self.raw_usage = raw
        data = self._derive(raw)
        self._store.async_delay_save(self._persisted_state, STORE_SAVE_DELAY)
        self._log_data_health(raw, data)
        return data

    def _log_data_health(self, raw: dict[str, Any], data: ClawdmeterData) -> None:
        """Explain in the log when the API returns no usable usage windows."""
        if data.session_usage is None and data.week_usage is None:
            if not self._logged_empty:
                LOGGER.warning(
                    "Usage API returned no session or weekly data (keys: %s); those "
                    "entities stay unavailable. This is normal if you have not used "
                    "Claude in these windows, but if it persists the poll interval may "
                    "be too low and the rate-limited API may be throttling you",
                    sorted(raw),
                )
                self._logged_empty = True
        elif self._logged_empty:
            LOGGER.info("Usage API is returning session/weekly data again")
            self._logged_empty = False

    async def _async_ensure_token(self) -> None:
        """Refresh the access token shortly before it expires."""
        expires_at = self.config_entry.data.get(CONF_EXPIRES_AT, 0)
        if time.time() < expires_at - TOKEN_EXPIRY_MARGIN:
            return
        LOGGER.debug("Access token expired, refreshing")
        try:
            bundle = await self._client.async_refresh(
                self.config_entry.data.get(CONF_REFRESH_TOKEN, "")
            )
        except ClawdmeterAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except ClawdmeterConnectionError as err:
            raise UpdateFailed(str(err)) from err
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={
                **self.config_entry.data,
                CONF_ACCESS_TOKEN: bundle.access_token,
                CONF_REFRESH_TOKEN: bundle.refresh_token,
                CONF_EXPIRES_AT: bundle.expires_at,
            },
        )

    def _derive(self, raw: dict[str, Any]) -> ClawdmeterData:
        """Combine the raw payload with the rolling burn-rate window."""
        now = dt_util.utcnow()

        session_usage = _utilization(raw.get("five_hour"))
        session_reset = _reset_at(raw.get("five_hour"))
        week_usage = _utilization(raw.get("seven_day"))
        week_reset = _reset_at(raw.get("seven_day"))
        sonnet_usage = _utilization(raw.get("seven_day_sonnet"))
        sonnet_reset = _reset_at(raw.get("seven_day_sonnet"))
        opus_usage = _utilization(raw.get("seven_day_opus"))
        opus_reset = _reset_at(raw.get("seven_day_opus"))
        overage = _overage(raw)

        self._record_sample(now.timestamp(), session_usage)
        burn_fast = self._rate_per_hour(BURN_WINDOW_FAST.total_seconds())
        burn_slow = self._rate_per_hour(BURN_WINDOW_SLOW.total_seconds())
        usage_rate = self._rate_per_minute()

        session_reset_in = _minutes_until(session_reset, now)
        time_to_limit = _time_to_limit(session_usage, burn_slow)
        runway_pace = _runway_pace(time_to_limit, session_reset_in, burn_slow)
        limit_eta = (
            now + timedelta(minutes=time_to_limit)
            if time_to_limit is not None
            else None
        )

        return ClawdmeterData(
            account_name=self.config_entry.data.get(CONF_ACCOUNT_NAME),
            plan=self.config_entry.data.get(CONF_PLAN),
            session_usage=session_usage,
            session_reset=session_reset,
            session_reset_in=session_reset_in,
            session_peak_today=self._track_peak(now, session_usage),
            week_usage=week_usage,
            week_pace=_week_pace(week_usage, week_reset, now),
            week_reset=week_reset,
            sonnet_usage=sonnet_usage,
            sonnet_reset=sonnet_reset,
            opus_usage=opus_usage,
            opus_reset=opus_reset,
            extra_enabled=overage.enabled,
            extra_usage=overage.usage,
            extra_credits=overage.credits,
            extra_limit=overage.limit,
            extra_currency=overage.currency,
            extra_severity=overage.severity,
            burn_rate_fast=burn_fast,
            burn_rate_slow=burn_slow,
            usage_rate=usage_rate,
            animation_group=_animation_group(usage_rate),
            time_to_limit=time_to_limit,
            limit_eta=limit_eta,
            runway_pace=runway_pace,
            runway_margin=_runway_margin(time_to_limit, session_reset_in),
            runway_over=_runway_over(time_to_limit, session_reset_in, burn_slow),
            pace_frame=_pace_frame(runway_pace),
        )

    def _record_sample(self, ts: float, pct: float | None) -> None:
        """Append a usage reading, flushing the window on a session reset."""
        if pct is None:
            return
        if self._samples and pct < self._samples[-1].pct - RESET_DROP_POINTS:
            self._samples.clear()
            self._samples.append(_Sample(ts, pct))
            return
        self._samples.append(_Sample(ts, pct))
        cutoff = ts - SAMPLE_RETENTION.total_seconds()
        while len(self._samples) > 2 and self._samples[0].ts < cutoff:
            self._samples.popleft()

    def _rate_per_hour(self, window_seconds: float) -> float:
        """Return the usage slope in %/h over a trailing window.

        Reports 0 (not "unknown") whenever no upward slope can be measured yet -
        a single sample, just after a reset, or genuinely flat usage. A burn rate
        of zero is the true state when nobody is using Claude, and it keeps history
        graphs continuous instead of leaving gaps over nights and weekends.
        """
        if len(self._samples) < 2:
            return 0.0
        newest = self._samples[-1]
        oldest = newest
        for sample in reversed(self._samples):
            if newest.ts - sample.ts > window_seconds:
                break
            oldest = sample
        span = newest.ts - oldest.ts
        if span <= 0:
            return 0.0
        return max(0.0, (newest.pct - oldest.pct) / (span / 3600.0))

    def _rate_per_minute(self) -> float:
        """Return the %/min slope over the whole window, 0 while warming up."""
        if len(self._samples) < 2:
            return 0.0
        oldest = self._samples[0]
        newest = self._samples[-1]
        span = newest.ts - oldest.ts
        if span < RATE_WARMUP.total_seconds():
            return 0.0
        return max(0.0, newest.pct - oldest.pct) * 60.0 / span

    def _track_peak(self, now: datetime, session_usage: float | None) -> float | None:
        """Track the highest session usage seen on the current local day."""
        today = dt_util.as_local(now).date()
        if today != self._peak_date:
            self._peak_date = today
            self._peak_value = session_usage
        elif session_usage is not None and (
            self._peak_value is None or session_usage > self._peak_value
        ):
            self._peak_value = session_usage
        return self._peak_value


def _as_float(value: Any) -> float | None:
    """Return an API value as a number (parsing numeric strings), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _utilization(section: dict[str, Any] | None) -> float | None:
    """Pull the utilization percentage out of a usage section."""
    if not section:
        return None
    return _as_float(section.get("utilization"))


def _reset_at(section: dict[str, Any] | None) -> datetime | None:
    """Parse the ISO reset instant out of a usage section as a UTC datetime."""
    if not section:
        return None
    raw = section.get("resets_at")
    if not raw:
        return None
    parsed = dt_util.parse_datetime(raw)
    if parsed is not None and parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


@dataclass(frozen=True, slots=True)
class _Overage:
    """Parsed overage / extra-usage figures."""

    enabled: bool | None
    usage: float | None
    credits: float | None
    limit: float | None
    currency: str | None
    severity: str | None


def _money(block: Any) -> tuple[float | None, str | None]:
    """Read an (amount, currency) pair from a spend money block or flat number."""
    if isinstance(block, dict):
        amount = _as_float(block.get("amount_minor"))
        scale = 10 ** block.get("exponent", 2)
        return (amount / scale if amount is not None else None, block.get("currency"))
    amount = _as_float(block)
    return (amount / 100 if amount is not None else None, None)


def _overage(raw: dict[str, Any]) -> _Overage:
    """Read the optional overage figures from either API shape.

    The legacy API exposes ``extra_usage`` with credits plus a currency; the newer
    API mirrors it in a top-level ``spend`` object (nested money blocks) and adds a
    ``severity``. Credit amounts are scaled by their declared decimal places.
    """
    extra = raw.get("extra_usage")
    spend = raw.get("spend")
    severity = spend.get("severity") if spend else None
    if extra and extra.get("is_enabled"):
        divisor = 10 ** extra.get("decimal_places", 2)
        used = _as_float(extra.get("used_credits"))
        limit = _as_float(extra.get("monthly_limit"))
        currency = extra.get("currency")
        if currency is None and spend:
            currency = _money(spend.get("used"))[1]
        return _Overage(
            True,
            _as_float(extra.get("utilization")),
            used / divisor if used is not None else None,
            limit / divisor if limit is not None else None,
            currency,
            severity,
        )
    if spend and spend.get("enabled"):
        used_amount, used_currency = _money(spend.get("used"))
        limit_amount, limit_currency = _money(spend.get("limit"))
        return _Overage(
            True,
            _as_float(spend.get("percent")),
            used_amount,
            limit_amount,
            used_currency or limit_currency,
            severity,
        )
    if extra is not None or spend is not None:
        return _Overage(False, None, None, None, None, severity)
    return _Overage(None, None, None, None, None, None)


def _week_pace(
    usage: float | None, reset: datetime | None, now: datetime
) -> float | None:
    """Return how far ahead (+) or behind (-) the linear weekly pace usage is."""
    if usage is None or reset is None:
        return None
    elapsed = WEEK_SECONDS - (reset - now).total_seconds()
    percent_elapsed = elapsed / WEEK_SECONDS * 100
    return round(usage - percent_elapsed, 1)


def _minutes_until(target: datetime | None, now: datetime) -> float | None:
    """Return whole-ish minutes until ``target``, clamped at zero."""
    if target is None:
        return None
    return max(0.0, (target - now).total_seconds() / 60.0)


def _time_to_limit(usage: float | None, burn_slow: float | None) -> float | None:
    """Project minutes until 100% at the slow burn rate."""
    if usage is None or burn_slow is None or burn_slow <= 0:
        return None
    return max(0.0, (100.0 - usage) / burn_slow * 60.0)


def _runway_pace(
    time_to_limit: float | None, reset_in: float | None, burn_slow: float | None
) -> float | None:
    """Return the pace ratio T_reset / T_limit (>1 means too fast)."""
    if burn_slow is None or reset_in is None:
        return None
    if burn_slow <= 0:
        return 0.0
    if time_to_limit is None or time_to_limit <= 0:
        return 99.0
    return reset_in / time_to_limit


def _runway_margin(time_to_limit: float | None, reset_in: float | None) -> float | None:
    """Signed minutes of slack: positive resets first, negative hits limit first."""
    if time_to_limit is None or reset_in is None:
        return None
    return time_to_limit - reset_in


def _runway_over(
    time_to_limit: float | None, reset_in: float | None, burn_slow: float | None
) -> bool | None:
    """Return whether usage hits the limit before the session resets."""
    if burn_slow is None or reset_in is None:
        return None
    if burn_slow <= 0 or time_to_limit is None:
        return False
    return time_to_limit < reset_in


def _animation_group(rate_per_min: float | None) -> str:
    """Bucket the warm/up-aware usage rate into a creature mood."""
    if rate_per_min is None or rate_per_min < GROUP_NORMAL_RATE:
        return GROUP_IDLE
    if rate_per_min < GROUP_ACTIVE_RATE:
        return GROUP_NORMAL
    if rate_per_min < GROUP_HEAVY_RATE:
        return GROUP_ACTIVE
    return GROUP_HEAVY


def _pace_frame(pace: float | None) -> str | None:
    """Map the pace ratio onto a traffic-light frame colour."""
    if pace is None:
        return None
    if pace < PACE_GREEN_MAX:
        return FRAME_GREEN
    if pace < PACE_ORANGE_MAX:
        return FRAME_ORANGE
    return FRAME_RED
