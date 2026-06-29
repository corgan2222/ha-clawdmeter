"""Test the Clawdmeter API client retry and error handling."""

from typing import Any
from unittest.mock import AsyncMock

from aiohttp import ClientError
import pytest

from custom_components.clawdmeter.api import ClawdmeterClient, ClawdmeterConnectionError


def _json_response(status: int, payload: dict[str, Any]) -> AsyncMock:
    """Build a stub aiohttp response with a status and JSON body."""
    response = AsyncMock()
    response.status = status
    response.json = AsyncMock(return_value=payload)
    return response


def _broken_json_response(status: int = 200) -> AsyncMock:
    """Build a stub response whose body fails to parse as JSON."""
    response = AsyncMock()
    response.status = status
    response.json = AsyncMock(side_effect=ClientError("bad body"))
    return response


async def test_connection_retry_recovers() -> None:
    """Test a transient connection failure is retried and then succeeds."""
    payload = {"five_hour": {"utilization": 20}}
    session = AsyncMock()
    session.get = AsyncMock(
        side_effect=[ClientError("boom"), _json_response(200, payload)]
    )

    client = ClawdmeterClient(session)
    assert await client.async_get_usage("token") == payload
    # The first attempt failed; the retry returned the payload.
    assert session.get.call_count == 2


async def test_connection_retry_exhausts() -> None:
    """Test the retry budget is finite and a persistent failure surfaces."""
    session = AsyncMock()
    session.get = AsyncMock(side_effect=ClientError("down"))

    client = ClawdmeterClient(session)
    with pytest.raises(ClawdmeterConnectionError):
        await client.async_get_usage("token")
    # The initial attempt plus the two (test-patched) retries.
    assert session.get.call_count == 3


async def test_get_unparseable_body_is_connection_error() -> None:
    """Test a delivered-but-unparseable GET body is a connection error."""
    session = AsyncMock()
    session.get = AsyncMock(return_value=_broken_json_response())

    client = ClawdmeterClient(session)
    with pytest.raises(ClawdmeterConnectionError):
        await client.async_get_usage("token")
    # A bad body is not a connection failure, so it is not retried.
    assert session.get.call_count == 1


async def test_token_unparseable_body_is_connection_error() -> None:
    """Test a delivered-but-unparseable token body is a connection error."""
    session = AsyncMock()
    session.post = AsyncMock(return_value=_broken_json_response())

    client = ClawdmeterClient(session)
    with pytest.raises(ClawdmeterConnectionError):
        await client.async_refresh("refresh-token")
    assert session.post.call_count == 1
