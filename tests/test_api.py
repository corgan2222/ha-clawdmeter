"""Test the Clawdmeter API client retry and error handling."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aiohttp import ClientError, ConnectionTimeoutError, ServerDisconnectedError
import pytest

from custom_components.clawdmeter.api import ClawdmeterClient, ClawdmeterConnectionError


def _response(status: int, payload: Any = None, error: Exception | None = None) -> Any:
    """Build a stub response with a status and a JSON body or a parse error."""
    response = MagicMock()
    response.status = status
    response.json = AsyncMock(return_value=payload, side_effect=error)
    return response


def _session(method: str, *outcomes: Any) -> MagicMock:
    """Build a session whose ``method`` yields each outcome in turn.

    An outcome is either a response, entered through ``async with`` the way the
    client uses it, or an exception raised while connecting.
    """
    contexts = []
    for outcome in outcomes:
        context = MagicMock()
        if isinstance(outcome, BaseException):
            context.__aenter__ = AsyncMock(side_effect=outcome)
        else:
            context.__aenter__ = AsyncMock(return_value=outcome)
        context.__aexit__ = AsyncMock(return_value=False)
        contexts.append(context)
    session = MagicMock()
    getattr(session, method).side_effect = contexts
    session.contexts = contexts
    return session


async def test_connection_retry_recovers() -> None:
    """Test a failure to connect is retried and then succeeds."""
    payload = {"five_hour": {"utilization": 20}}
    session = _session("get", ConnectionTimeoutError(), _response(200, payload))

    client = ClawdmeterClient(session)
    assert await client.async_get_usage("token") == payload
    assert session.get.call_count == 2


async def test_connection_retry_exhausts() -> None:
    """Test the retry budget is finite and a persistent failure surfaces."""
    session = _session("get", *(ConnectionTimeoutError() for _ in range(3)))

    client = ClawdmeterClient(session)
    with pytest.raises(ClawdmeterConnectionError):
        await client.async_get_usage("token")
    # The initial attempt plus the two (test-patched) retries.
    assert session.get.call_count == 3


@pytest.mark.parametrize(
    "error",
    [ServerDisconnectedError(), ClientError("reset"), TimeoutError()],
    ids=["disconnected", "client_error", "timeout"],
)
async def test_failure_after_connecting_is_not_retried(error: Exception) -> None:
    """Test a request that may have reached the server is not sent again.

    Repeating it could count twice against the rate limit, which locks a caller
    out for about a day.
    """
    session = _session("get", error, _response(200, {}))

    client = ClawdmeterClient(session)
    with pytest.raises(ClawdmeterConnectionError):
        await client.async_get_usage("token")
    assert session.get.call_count == 1


@pytest.mark.parametrize(
    ("response", "method", "call"),
    [
        (_response(200, error=ClientError("bad body")), "get", "usage"),
        (_response(200, error=ValueError("not json")), "get", "usage"),
        (_response(200, ["not", "an", "object"]), "get", "usage"),
        (_response(200, error=ValueError("not json")), "post", "refresh"),
    ],
    ids=["content_type", "invalid_json", "not_an_object", "token_invalid_json"],
)
async def test_unusable_body_is_connection_error(
    response: Any, method: str, call: str
) -> None:
    """Test a delivered but unusable body is a connection error, sent once."""
    session = _session(method, response)

    client = ClawdmeterClient(session)
    with pytest.raises(ClawdmeterConnectionError):
        if call == "usage":
            await client.async_get_usage("token")
        else:
            await client.async_refresh("refresh-token")
    assert getattr(session, method).call_count == 1


async def test_error_status_releases_the_response() -> None:
    """Test an error status leaves its body unread and the response released."""
    response = _response(500)
    session = _session("get", response)

    client = ClawdmeterClient(session)
    with pytest.raises(ClawdmeterConnectionError):
        await client.async_get_usage("token")
    response.json.assert_not_awaited()
    session.contexts[0].__aexit__.assert_awaited_once()
