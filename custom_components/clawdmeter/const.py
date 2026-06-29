"""Constants for the Clawdmeter integration."""

from datetime import timedelta
import logging
from typing import Final

DOMAIN: Final = "clawdmeter"
LOGGER: Final = logging.getLogger(__package__)

# How often the usage API is polled, in seconds. The endpoint is aggressively
# rate limited (a too-fast caller is locked out for roughly a day), so the
# default is conservative and the configurable minimum is clamped.
DEFAULT_SCAN_INTERVAL: Final = 300
MIN_SCAN_INTERVAL: Final = 60
MAX_SCAN_INTERVAL: Final = 3600

# Re-serve the last known values through this many consecutive failed polls
# before the entities go unavailable, so a brief API/network blip does not flip
# everything to "unavailable". At the 300 s default interval this is roughly a
# 30-minute grace window.
MAX_FETCH_FAILURES: Final = 6

# OAuth - the public PKCE client shared with the Claude CLI. No secret involved;
# the user pastes the authorization code from the Anthropic callback page.
OAUTH_CLIENT_ID: Final = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_AUTHORIZE_URL: Final = "https://claude.ai/oauth/authorize"
OAUTH_TOKEN_URL: Final = "https://console.anthropic.com/v1/oauth/token"
OAUTH_REDIRECT_URI: Final = "https://console.anthropic.com/oauth/code/callback"
OAUTH_SCOPES: Final = "org:create_api_key user:profile user:inference"

# Usage / profile endpoints and the beta opt-in header they require.
USAGE_ENDPOINT: Final = "https://api.anthropic.com/api/oauth/usage"
PROFILE_ENDPOINT: Final = "https://api.anthropic.com/api/oauth/profile"
BETA_HEADER: Final = "oauth-2025-04-20"

REQUEST_TIMEOUT: Final = 15
# Backoff (seconds) between immediate retries of a genuine connection failure.
# A failed connection never reaches the server, so retrying it does not count
# against the endpoint's aggressive rate limit; HTTP error statuses are never
# retried. The tuple length is the number of extra attempts; () disables retry.
CONNECT_RETRY_DELAYS: Final = (2.0, 5.0)
# Refresh the access token this many seconds before it actually expires.
TOKEN_EXPIRY_MARGIN: Final = 60

# Persist the burn-rate window + daily peak so they survive a restart.
STORAGE_VERSION: Final = 1
STORE_SAVE_DELAY: Final = 30

# Config-entry data keys. CONF_ACCESS_TOKEN lives in homeassistant.const.
CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_EXPIRES_AT: Final = "expires_at"
CONF_ACCOUNT_NAME: Final = "account_name"
CONF_ACCOUNT_EMAIL: Final = "account_email"
CONF_PLAN: Final = "plan"
CONF_AUTH_CODE: Final = "auth_code"

# --- Derived burn-rate / projection tuning ---------------------------------
# A downward step in session usage larger than this many points is treated as a
# session reset: the rolling sample window is flushed so the slope never spans
# the drop back to ~0%.
RESET_DROP_POINTS: Final = 5.0
# Trailing windows for the two exposed burn rates.
BURN_WINDOW_FAST: Final = timedelta(minutes=5)
BURN_WINDOW_SLOW: Final = timedelta(minutes=30)
# Samples older than this are discarded; also the span of the animation rate.
SAMPLE_RETENTION: Final = timedelta(minutes=30)
# The animation rate stays "warming up" until the window spans at least this.
RATE_WARMUP: Final = timedelta(minutes=2)

# Animation mood thresholds, in percent-per-minute (mirrors the device engine).
GROUP_NORMAL_RATE: Final = 0.10
GROUP_ACTIVE_RATE: Final = 0.20
GROUP_HEAVY_RATE: Final = 0.33

GROUP_IDLE: Final = "idle"
GROUP_NORMAL: Final = "normal"
GROUP_ACTIVE: Final = "active"
GROUP_HEAVY: Final = "heavy"
ANIMATION_GROUPS: Final = [GROUP_IDLE, GROUP_NORMAL, GROUP_ACTIVE, GROUP_HEAVY]

# Pace-frame bands, keyed off the runway pace ratio (T_reset / T_limit).
PACE_GREEN_MAX: Final = 0.90
PACE_ORANGE_MAX: Final = 1.00

FRAME_GREEN: Final = "green"
FRAME_ORANGE: Final = "orange"
FRAME_RED: Final = "red"
PACE_FRAMES: Final = [FRAME_GREEN, FRAME_ORANGE, FRAME_RED]

# Overage spend severity reported by the API.
OVERAGE_SEVERITIES: Final = ["normal", "warning", "critical"]
