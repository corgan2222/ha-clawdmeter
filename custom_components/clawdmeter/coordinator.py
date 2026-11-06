"""Data update coordinator for the Clawdmeter integration."""

from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
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
    CONF_SESSION_FALLBACK,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SESSION_FALLBACK,
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
    MAX_FETCH_FAILURES,
    PACE_GREEN_MAX,
    PACE_ORANGE_MAX,
    RATE_WARMUP,
    RESET_DROP_POINTS,
    SAMPLE_RETENTION,
    STORAGE_VERSION,
    STORE_SAVE_DELAY,
    TOKEN_EXPIRY_MARGIN,
    WINDOW_SESSION,
    WINDOW_WEEKLY,
)
from .limits import (
    KIND_SESSION,
    KIND_WEEKLY_ALL,
    LimitRow,
    allowances,
    as_datetime,
    as_float,
    of_kind,
    parse_limits,
    scoped,
    scoped_for,
)

type ClawdmeterConfigEntry = ConfigEntry[ClawdmeterDataUpdateCoordinator]

WEEK_SECONDS = 7 * 24 * 60 * 60


def account_title(name: str | None, plan: str | None) -> str:
    """Build a "Claude <name> (<plan>)" label for an account.

    Drives both the config entry title and the device name, so entity ids end up
    as ``claude_<name>_<plan>_<type>`` (e.g. ``claude_stefan_max_extra_usage``).
    An id keeps the plan it was created under when the plan changes later.
    """
    parts = ["Claude"]
    if name:
        parts.append(name)
    if plan:
        parts.append(f"({plan})")
    return " ".join(parts)


def follow_title(entry: ConfigEntry, name: str | None, plan: str | None) -> str:
    """Return the entry title to use for new account details.

    A title in the generated "Claude <name> (<plan>)" shape follows the
    account, including one an older version left on a superseded plan. A title
    the user typed stays as it is.
    """
    base = account_title(entry.data.get(CONF_ACCOUNT_NAME), None)
    generated = entry.title == base or (
        entry.title.startswith(f"{base} (") and entry.title.endswith(")")
    )
    return account_title(name, plan) if generated else entry.title


@dataclass(frozen=True, slots=True)
class ClawdmeterData:
    """Parsed usage plus every locally derived projection."""

    account_name: str | None
    plan: str | None
    session_usage: float | None
    session_reset: datetime | None
    session_reset_in: float | None
    session_peak_today: float | None
    # Which window the four session fields above actually describe: "session"
    # normally, "weekly" when the plan has no five-hour window and the fallback
    # is on, None while nothing is known yet.
    session_window: str | None
    week_usage: float | None
    week_pace: float | None
    week_reset: datetime | None
    sonnet_usage: float | None
    sonnet_reset: datetime | None
    opus_usage: float | None
    opus_reset: datetime | None
    # The per-model weekly cap the server currently applies, whatever model it
    # is for. model_usages maps every capped model's label to its percentage.
    model_name: str | None
    model_usage: float | None
    model_reset: datetime | None
    model_allowance: float | None
    model_usages: dict[str, float]
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
        version: str,
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
        self.integration_version = version
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
        self._fetch_failures = 0
        # Sticky: an account that has ever reported a five-hour window has one.
        # The API omits that window between resets, so a single poll without it
        # must not be read as "this plan has no session limit".
        self._session_seen = False
        self._logged_fallback = False
        # Sticky for the same reason: a model whose weekly figure has arrived
        # once keeps its sensors through a poll that happens to omit it.
        self.models_seen: set[str] = set()
        # The plan this entry had before, when it changed since the last run.
        self.plan_changed_from: str | None = None
        self._stored_plan: str | None = None

    @override
    async def _async_setup(self) -> None:
        """Restore the state of the last run and refresh the account details."""
        await self._async_restore()
        await self._async_refresh_account()
        plan = self.config_entry.data.get(CONF_PLAN)
        # The stored plan is the one the remembered state below was learned
        # under, whether the entry changed through the profile lookup above, a
        # reauth or a reconfigure.
        if self._stored_plan in (None, plan):
            return
        self.plan_changed_from = self._stored_plan
        # What the old plan reported says nothing about the new one: Max has a
        # five-hour window and Ultra has none, and the per-model caps differ too.
        # The samples and the day's peak would mix two different windows.
        LOGGER.info(
            "Plan changed from %s to %s, learning its windows again",
            self.plan_changed_from,
            plan,
        )
        self._session_seen = False
        self.models_seen.clear()
        self._samples.clear()
        self._peak_value = None
        self._peak_date = None

    async def _async_restore(self) -> None:
        """Restore the burn-rate window and daily peak from the last run."""
        stored = await self._store.async_load() or {}
        self._samples = deque(
            _Sample(item["ts"], item["pct"]) for item in stored.get("samples", [])
        )
        self._peak_value = stored.get("peak_value")
        peak_date = stored.get("peak_date")
        self._peak_date = date.fromisoformat(peak_date) if peak_date else None
        self._session_seen = bool(stored.get("session_seen", False))
        self.models_seen = set(stored.get("models_seen", []))
        if "plan" in stored:
            self._stored_plan = stored["plan"]
            return
        # Pin the plan before the profile lookup can change the entry. The state
        # is otherwise only saved after a successful poll, so a setup that
        # failed after the lookup would not see the change when it is retried.
        self._stored_plan = self.config_entry.data.get(CONF_PLAN)
        await self._store.async_save(self._persisted_state())

    async def _async_refresh_account(self) -> None:
        """Bring the stored plan and account name up to date.

        The config flow writes them once, so a plan changed later on (an
        upgrade, a tier renamed by the server) would otherwise keep its old name
        until the next reauth. This is best effort: the usage poll that follows
        reports a real auth or connection problem on its own.
        """
        try:
            await self._async_ensure_token()
            account = await self._client.async_get_account(
                self.config_entry.data[CONF_ACCESS_TOKEN]
            )
        except (ClawdmeterAuthError, ClawdmeterConnectionError) as err:
            LOGGER.debug("Could not refresh the account details: %s", err)
            return
        data = self.config_entry.data
        changes = {
            key: value
            for key, value in (
                (CONF_PLAN, account.plan),
                (CONF_ACCOUNT_NAME, account.name),
            )
            if value is not None and value != data.get(key)
        }
        new_data = {**data, **changes}
        title = follow_title(
            self.config_entry,
            new_data.get(CONF_ACCOUNT_NAME),
            new_data.get(CONF_PLAN),
        )
        if not changes and title == self.config_entry.title:
            return
        LOGGER.info(
            "Account details changed, updating the entry: %s",
            changes or {"title": title},
        )
        self.hass.config_entries.async_update_entry(
            self.config_entry, data=new_data, title=title
        )

    @callback
    def _persisted_state(self) -> dict[str, Any]:
        """Return the in-memory state to keep across a restart."""
        return {
            "samples": [{"ts": s.ts, "pct": s.pct} for s in self._samples],
            "peak_value": self._peak_value,
            "peak_date": self._peak_date.isoformat() if self._peak_date else None,
            "session_seen": self._session_seen,
            "models_seen": sorted(self.models_seen),
            "plan": self.config_entry.data.get(CONF_PLAN),
        }

    @override
    async def _async_update_data(self) -> ClawdmeterData:
        """Refresh the token if needed, fetch usage and derive metrics."""
        try:
            await self._async_ensure_token()
            token = self.config_entry.data[CONF_ACCESS_TOKEN]
            raw = await self._client.async_get_usage(token)
        except ClawdmeterAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except ClawdmeterConnectionError as err:
            return self._handle_fetch_failure(err)
        self._fetch_failures = 0
        LOGGER.debug("Fetched usage payload: %s", raw)
        self.raw_usage = raw
        data = self._derive(raw)
        self._store.async_delay_save(self._persisted_state, STORE_SAVE_DELAY)
        self._log_data_health(raw, data)
        return data

    def _handle_fetch_failure(self, err: ClawdmeterConnectionError) -> ClawdmeterData:
        """Re-serve the last known values through a brief outage.

        A handful of consecutive connection failures keep the previous data so a
        short API/network blip does not flip every entity to "unavailable"; once
        they persist past MAX_FETCH_FAILURES the error surfaces and entities go
        unavailable as usual.
        """
        self._fetch_failures += 1
        if self.data is not None and self._fetch_failures <= MAX_FETCH_FAILURES:
            LOGGER.warning(
                "Error fetching clawdmeter data (%s/%s), keeping the last known "
                "values: %s",
                self._fetch_failures,
                MAX_FETCH_FAILURES,
                err,
            )
            return self.data
        raise UpdateFailed(str(err)) from err

    def _log_data_health(self, raw: dict[str, Any], data: ClawdmeterData) -> None:
        """Explain in the log when the API returns no usable usage windows."""
        if data.session_usage is None and data.week_usage is None:
            if not self._logged_empty:
                LOGGER.warning(
                    "Usage API returned no session or weekly data (keys: %s); those "
                    "entities stay unknown. This is normal if you have not used "
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
        bundle = await self._client.async_refresh(
            self.config_entry.data.get(CONF_REFRESH_TOKEN, "")
        )
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

        rows = parse_limits(raw.get("limits"))
        shares = allowances(raw.get("weekly_scoped_shares"), rows)

        session_usage, session_reset = _window(
            raw.get("five_hour"), of_kind(rows, KIND_SESSION)
        )
        week_usage, week_reset = _window(
            raw.get("seven_day"), of_kind(rows, KIND_WEEKLY_ALL)
        )
        sonnet_usage, sonnet_reset = _window(
            raw.get("seven_day_sonnet"), scoped_for(rows, "sonnet")
        )
        opus_usage, opus_reset = _window(
            raw.get("seven_day_opus"), scoped_for(rows, "opus")
        )

        if session_usage is not None or session_reset is not None:
            self._session_seen = True
        for model, usage, reset in (
            ("sonnet", sonnet_usage, sonnet_reset),
            ("opus", opus_usage, opus_reset),
        ):
            if usage is not None or reset is not None:
                self.models_seen.add(model)
        self.models_seen.update(row.slug for row in scoped(rows) if row.slug)
        session_window = WINDOW_SESSION if self._session_seen else None
        if not self._session_seen and self.config_entry.options.get(
            CONF_SESSION_FALLBACK, DEFAULT_SESSION_FALLBACK
        ):
            # Some plans have no five-hour window at all, so the week is the
            # only horizon the account has. Running it through the session
            # fields keeps the burn rate, the projections and anything reading
            # them alive, instead of leaving a third of the entities empty for
            # a reason that is not a fault. The `window` attribute says which
            # of the two a consumer is actually looking at.
            session_usage = week_usage
            session_reset = week_reset
            session_window = WINDOW_WEEKLY
            if not self._logged_fallback:
                LOGGER.info(
                    "This account reports no five-hour window, so the session "
                    "entities follow the weekly one instead; their `window` "
                    "attribute says which. Turn the session fallback off in the "
                    "integration options to leave them empty"
                )
                self._logged_fallback = True

        model_row = _busiest(scoped(rows))
        overage = _overage(raw)

        self._record_sample(now.timestamp(), session_usage)
        burn_fast = self._rate_per_hour(BURN_WINDOW_FAST.total_seconds())
        burn_slow = self._rate_per_hour(BURN_WINDOW_SLOW.total_seconds())
        usage_rate = self._rate_per_minute()

        # The 30-minute burn rate says how fast usage moves right now, the right
        # horizon for a five-hour window. Across a week it is zero most of the
        # time and would leave every projection empty, so the weekly window
        # projects from its average since the week opened instead.
        projection_rate: float | None = burn_slow
        if session_window == WINDOW_WEEKLY:
            projection_rate = _average_rate(session_usage, session_reset, now)

        session_reset_in = _minutes_until(session_reset, now)
        time_to_limit = _time_to_limit(session_usage, projection_rate)
        runway_pace = _runway_pace(time_to_limit, session_reset_in, projection_rate)
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
            session_window=session_window,
            week_usage=week_usage,
            week_pace=_week_pace(week_usage, week_reset, now),
            week_reset=week_reset,
            sonnet_usage=sonnet_usage,
            sonnet_reset=sonnet_reset,
            opus_usage=opus_usage,
            opus_reset=opus_reset,
            model_name=model_row.label if model_row else None,
            model_usage=model_row.percent if model_row else None,
            model_reset=model_row.resets_at if model_row else None,
            model_allowance=shares.get(model_row.index) if model_row else None,
            model_usages={
                row.label: row.percent
                for row in scoped(rows)
                if row.label is not None and row.percent is not None
            },
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
            runway_over=_runway_over(time_to_limit, session_reset_in, projection_rate),
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


def _utilization(section: Any) -> float | None:
    """Pull the utilization percentage out of a usage section."""
    if not isinstance(section, dict):
        return None
    return as_float(section.get("utilization"))


def _reset_at(section: Any) -> datetime | None:
    """Parse the ISO reset instant out of a usage section as a UTC datetime."""
    if not isinstance(section, dict):
        return None
    return as_datetime(section.get("resets_at"))


def _window(
    legacy: dict[str, Any] | None, row: LimitRow | None
) -> tuple[float | None, datetime | None]:
    """Resolve one usage window from the legacy key or its ``limits`` row.

    The legacy top-level objects win where they are still populated, so nothing
    changes for accounts that get them; the row fills in where the server has
    stopped sending them. Each half is resolved on its own because the two
    sources have been seen disagreeing on which fields they carry.
    """
    usage = _utilization(legacy)
    reset = _reset_at(legacy)
    if row is not None:
        if usage is None:
            usage = row.percent
        if reset is None:
            reset = row.resets_at
    return usage, reset


def _busiest(rows: list[LimitRow]) -> LimitRow | None:
    """Return the scoped row closest to its cap.

    ``is_active`` would look like the right field for this, but it marks the row
    the server wants shown as a single headline value, is not guaranteed to be
    set on exactly one row, and has been observed missing. Picking the highest
    percentage is something this side can always compute.
    """
    rated = [row for row in rows if row.percent is not None]
    if not rated:
        return None
    return max(rated, key=lambda row: row.percent or 0.0)


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
        amount = as_float(block.get("amount_minor"))
        scale = 10 ** block.get("exponent", 2)
        return (amount / scale if amount is not None else None, block.get("currency"))
    amount = as_float(block)
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
    # Whether the feature is switched on and what it currently costs are two
    # different questions. The API keeps reporting the spend and the cap while
    # it is off (a zero balance disables it server-side even with the toggle
    # on), and the web UI shows those figures, so they are reported here too
    # and the enabled flag answers the first question on its own.
    enabled: bool | None = None
    if extra is not None:
        enabled = bool(extra.get("is_enabled"))
    elif spend is not None:
        enabled = bool(spend.get("enabled"))
    if extra:
        divisor = 10 ** extra.get("decimal_places", 2)
        used = as_float(extra.get("used_credits"))
        limit = as_float(extra.get("monthly_limit"))
        currency = extra.get("currency")
        if currency is None and spend:
            currency = _money(spend.get("used"))[1]
        return _Overage(
            enabled,
            as_float(extra.get("utilization")),
            used / divisor if used is not None else None,
            limit / divisor if limit is not None else None,
            currency,
            severity,
        )
    if spend:
        used_amount, used_currency = _money(spend.get("used"))
        limit_amount, limit_currency = _money(spend.get("limit"))
        return _Overage(
            enabled,
            as_float(spend.get("percent")),
            used_amount,
            limit_amount,
            used_currency or limit_currency,
            severity,
        )
    return _Overage(enabled, None, None, None, None, severity)


def _week_elapsed(reset: datetime, now: datetime) -> float:
    """Return the seconds since the weekly window ending at ``reset`` opened."""
    return WEEK_SECONDS - (reset - now).total_seconds()


def _week_pace(
    usage: float | None, reset: datetime | None, now: datetime
) -> float | None:
    """Return how far ahead (+) or behind (-) the linear weekly pace usage is."""
    if usage is None or reset is None:
        return None
    percent_elapsed = _week_elapsed(reset, now) / WEEK_SECONDS * 100
    return round(usage - percent_elapsed, 1)


def _average_rate(
    usage: float | None, reset: datetime | None, now: datetime
) -> float | None:
    """Return the mean usage in %/h since the weekly window opened."""
    if usage is None or reset is None:
        return None
    elapsed_hours = _week_elapsed(reset, now) / 3600
    if elapsed_hours <= 0:
        return None
    return usage / elapsed_hours


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
