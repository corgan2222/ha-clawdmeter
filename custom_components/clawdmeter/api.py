"""Thin async client for the Claude OAuth usage API."""

import asyncio
import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import hashlib
import secrets
import time
from typing import Any
from urllib.parse import urlencode

from aiohttp import ClientError, ClientResponse, ClientSession, ClientTimeout

from .const import (
    BETA_HEADER,
    CONNECT_RETRY_DELAYS,
    LOGGER,
    OAUTH_AUTHORIZE_URL,
    OAUTH_CLIENT_ID,
    OAUTH_REDIRECT_URI,
    OAUTH_SCOPES,
    OAUTH_TOKEN_URL,
    PROFILE_ENDPOINT,
    REQUEST_TIMEOUT,
    USAGE_ENDPOINT,
)


class ClawdmeterError(Exception):
    """Base error for the Claude usage client."""


class ClawdmeterConnectionError(ClawdmeterError):
    """Raised when the API cannot be reached."""


class ClawdmeterAuthError(ClawdmeterError):
    """Raised when credentials are rejected or missing."""


@dataclass(slots=True)
class OAuthChallenge:
    """A PKCE verifier/challenge pair plus the anti-CSRF state."""

    verifier: str
    challenge: str
    state: str

    @classmethod
    def create(cls) -> OAuthChallenge:
        """Build a fresh challenge with random verifier and state."""
        verifier = secrets.token_urlsafe(32)
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        return cls(verifier, challenge, secrets.token_urlsafe(32))

    @property
    def authorize_url(self) -> str:
        """Return the Anthropic authorization URL for this challenge."""
        query = urlencode(
            {
                "code": "true",
                "client_id": OAUTH_CLIENT_ID,
                "response_type": "code",
                "redirect_uri": OAUTH_REDIRECT_URI,
                "scope": OAUTH_SCOPES,
                "code_challenge": self.challenge,
                "code_challenge_method": "S256",
                "state": self.state,
            }
        )
        return f"{OAUTH_AUTHORIZE_URL}?{query}"


@dataclass(slots=True)
class TokenBundle:
    """An access/refresh token pair with its absolute expiry."""

    access_token: str
    refresh_token: str
    expires_at: float

    @classmethod
    def from_response(
        cls, payload: dict[str, Any], *, fallback_refresh: str = ""
    ) -> TokenBundle:
        """Build a bundle from an OAuth token response."""
        if "access_token" not in payload:
            raise ClawdmeterAuthError("Token response did not contain an access token")
        return cls(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token") or fallback_refresh,
            expires_at=time.time() + payload.get("expires_in", 3600),
        )


@dataclass(slots=True)
class AccountInfo:
    """Display details about the authenticated Claude account."""

    name: str | None
    email: str | None
    plan: str | None


class ClawdmeterClient:
    """Talk to the Claude OAuth usage and profile endpoints."""

    def __init__(self, session: ClientSession) -> None:
        """Store the shared aiohttp session."""
        self._session = session

    async def _request_with_retry(
        self, send: Callable[[], Awaitable[ClientResponse]]
    ) -> ClientResponse:
        """Send a request, retrying only genuine connection failures.

        A failed connection never reaches the server, so retrying it cannot trip
        the endpoint's aggressive rate limit. HTTP error statuses are inspected
        by the caller and are never retried here.
        """
        for attempt, delay in enumerate(CONNECT_RETRY_DELAYS, start=1):
            try:
                return await send()
            except ClientError as err:
                LOGGER.debug(
                    "Connection failed (attempt %s), retrying in %ss: %s",
                    attempt,
                    delay,
                    err,
                )
                await asyncio.sleep(delay)
        try:
            return await send()
        except ClientError as err:
            raise ClawdmeterConnectionError(str(err)) from err

    async def async_exchange_code(
        self, code: str, challenge: OAuthChallenge, returned_state: str
    ) -> TokenBundle:
        """Exchange an authorization code for tokens."""
        return await self._async_token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "state": returned_state,
                "client_id": OAUTH_CLIENT_ID,
                "redirect_uri": OAUTH_REDIRECT_URI,
                "code_verifier": challenge.verifier,
            }
        )

    async def async_refresh(self, refresh_token: str) -> TokenBundle:
        """Trade a refresh token for a fresh access token."""
        if not refresh_token:
            raise ClawdmeterAuthError("No refresh token available")
        return await self._async_token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": OAUTH_CLIENT_ID,
            },
            fallback_refresh=refresh_token,
        )

    async def _async_token_request(
        self, payload: dict[str, str], *, fallback_refresh: str = ""
    ) -> TokenBundle:
        """Post to the token endpoint and parse the bundle.

        The Anthropic OAuth token endpoint expects a JSON body; a form-encoded
        body is rejected with HTTP 400 (notably on token refresh).
        """
        response = await self._request_with_retry(
            lambda: self._session.post(
                OAUTH_TOKEN_URL,
                json=payload,
                timeout=ClientTimeout(total=REQUEST_TIMEOUT),
            )
        )
        if response.status >= 400:
            raise ClawdmeterAuthError(f"Token endpoint returned {response.status}")
        try:
            body = await response.json()
        except ClientError as err:
            raise ClawdmeterConnectionError(str(err)) from err
        return TokenBundle.from_response(body, fallback_refresh=fallback_refresh)

    async def async_get_account(self, access_token: str) -> AccountInfo:
        """Fetch the display name, email and plan of the account."""
        body = await self._async_get(PROFILE_ENDPOINT, access_token)
        account = body.get("account", {})
        email = account.get("email")
        plan = None
        if account.get("has_claude_max"):
            plan = "Max"
        elif account.get("has_claude_pro"):
            plan = "Pro"
        return AccountInfo(
            name=account.get("display_name") or account.get("full_name") or email,
            email=email,
            plan=plan,
        )

    async def async_get_usage(self, access_token: str) -> dict[str, Any]:
        """Fetch the raw usage payload."""
        return await self._async_get(USAGE_ENDPOINT, access_token)

    async def async_get_profile_raw(self, access_token: str) -> dict[str, Any]:
        """Fetch the unparsed profile payload (used by diagnostics)."""
        return await self._async_get(PROFILE_ENDPOINT, access_token)

    async def _async_get(self, url: str, access_token: str) -> dict[str, Any]:
        """Perform an authenticated GET and return parsed JSON."""
        response = await self._request_with_retry(
            lambda: self._session.get(
                url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "anthropic-beta": BETA_HEADER,
                },
                timeout=ClientTimeout(total=REQUEST_TIMEOUT),
            )
        )
        if response.status == 401:
            raise ClawdmeterAuthError("Access token rejected")
        if response.status >= 400:
            raise ClawdmeterConnectionError(f"{url} returned {response.status}")
        try:
            return await response.json()
        except ClientError as err:
            raise ClawdmeterConnectionError(str(err)) from err
