"""Config flow for the Clawdmeter integration."""

from collections.abc import Mapping
from typing import Any, override

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_SCAN_INTERVAL
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .api import (
    AccountInfo,
    ClawdmeterAuthError,
    ClawdmeterClient,
    ClawdmeterConnectionError,
    OAuthChallenge,
    TokenBundle,
)
from .const import (
    CONF_ACCOUNT_EMAIL,
    CONF_ACCOUNT_NAME,
    CONF_AUTH_CODE,
    CONF_EXPIRES_AT,
    CONF_PLAN,
    CONF_REFRESH_TOKEN,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LOGGER,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .entity import account_title

STEP_USER = "user"
STEP_REAUTH = "reauth_confirm"
STEP_RECONFIGURE = "reconfigure"

DATA_SCHEMA = vol.Schema({vol.Required(CONF_AUTH_CODE): str})


class ClawdmeterConfigFlow(ConfigFlow, domain=DOMAIN):
    """Authenticate a Claude account by pasting an OAuth code."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise the flow without a challenge yet."""
        self._challenge: OAuthChallenge | None = None

    @property
    def _oauth(self) -> OAuthChallenge:
        """Return a stable PKCE challenge for the lifetime of this flow."""
        if self._challenge is None:
            self._challenge = OAuthChallenge.create()
        return self._challenge

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start a brand new account authorization."""
        return await self._async_authorize(STEP_USER, user_input)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Trigger a re-authentication when the token is rejected."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect a fresh code to repair an existing entry."""
        return await self._async_authorize(STEP_REAUTH, user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-authorize, optionally switching the entry to another account."""
        return await self._async_authorize(STEP_RECONFIGURE, user_input)

    async def _async_authorize(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Render the paste-code form and process a submitted code."""
        errors: dict[str, str] = {}
        if user_input is not None:
            code = user_input[CONF_AUTH_CODE].strip()
            if not code:
                errors["base"] = "missing_code"
            else:
                try:
                    bundle, account = await self._async_redeem(code)
                except ClawdmeterAuthError:
                    errors["base"] = "exchange_failed"
                except ClawdmeterConnectionError:
                    errors["base"] = "cannot_connect"
                else:
                    return await self._async_finish(step_id, bundle, account)

        return self.async_show_form(
            step_id=step_id,
            data_schema=DATA_SCHEMA,
            description_placeholders={"url": self._oauth.authorize_url},
            errors=errors,
        )

    async def _async_redeem(self, code: str) -> tuple[TokenBundle, AccountInfo]:
        """Exchange the pasted code and fetch the account profile."""
        auth_code, _, returned_state = code.partition("#")
        if returned_state != self._oauth.state:
            LOGGER.warning("Discarding OAuth code with a missing or mismatched state")
            raise ClawdmeterAuthError("State mismatch")
        client = ClawdmeterClient(async_get_clientsession(self.hass))
        bundle = await client.async_exchange_code(
            auth_code, self._oauth, returned_state
        )
        account = await client.async_get_account(bundle.access_token)
        return bundle, account

    async def _async_finish(
        self, step_id: str, bundle: TokenBundle, account: AccountInfo
    ) -> ConfigFlowResult:
        """Create the entry, or update an existing one on reauth/reconfigure."""
        data = {
            CONF_ACCESS_TOKEN: bundle.access_token,
            CONF_REFRESH_TOKEN: bundle.refresh_token,
            CONF_EXPIRES_AT: bundle.expires_at,
            CONF_ACCOUNT_NAME: account.name,
            CONF_ACCOUNT_EMAIL: account.email,
            CONF_PLAN: account.plan,
        }

        if step_id == STEP_USER:
            await self.async_set_unique_id(account.email or account.name or DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=account_title(account.name, account.plan), data=data
            )

        entry = (
            self._get_reconfigure_entry()
            if step_id == STEP_RECONFIGURE
            else self._get_reauth_entry()
        )
        # Reauth and reconfigure can re-point an entry at a different account, so
        # refuse to let two entries track the same one and keep the entry's
        # unique id in sync with the account it now authenticates.
        new_unique_id = account.email or account.name
        if new_unique_id and any(
            other.entry_id != entry.entry_id and other.unique_id == new_unique_id
            for other in self._async_current_entries()
        ):
            return self.async_abort(reason="already_configured")
        return self.async_update_reload_and_abort(
            entry,
            unique_id=new_unique_id or entry.unique_id,
            data_updates=data,
        )

    @staticmethod
    @callback
    @override
    def async_get_options_flow(config_entry: ConfigEntry) -> ClawdmeterOptionsFlow:
        """Return the options flow handler."""
        return ClawdmeterOptionsFlow()


class ClawdmeterOptionsFlow(OptionsFlow):
    """Let the user adjust the polling interval."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the polling interval option."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    # A user-configurable poll interval is intentional here (an
                    # explicit feature request); the default stays conservative
                    # because the usage API is rate limited.
                    # pylint: disable-next=home-assistant-config-flow-polling-field
                    vol.Required(CONF_SCAN_INTERVAL, default=current): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
                    )
                }
            ),
        )
