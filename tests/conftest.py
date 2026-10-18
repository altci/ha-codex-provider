"""Fixtures use only fake identity and token values."""

from unittest.mock import Mock, patch

import pytest
from aiohttp import ClientResponse
from aioresponses import aioresponses
from homeassistant.setup import async_setup_component


@pytest.fixture(autouse=True)
async def custom_integrations(hass, enable_custom_integrations):
    """Allow loading this integration in the Home Assistant test harness."""
    assert await async_setup_component(hass, "homeassistant", {})


@pytest.fixture
def identity_data():
    """Return a deliberately nonfunctional test identity."""
    return {
        "client_id": "test-client",
        "originator": "test-origin",
        "user_agent": "test-agent",
        "model": "gpt-5.5",
    }


@pytest.fixture
def token_data():
    """Never use real credentials in fixtures."""
    return {
        "access_token": "fake-access",
        "refresh_token": "fake-refresh",
        "expires_in": 3600,
    }


@pytest.fixture
def responses():
    """Mock all provider HTTP I/O."""

    # aioresponses 0.7.9 omits the new aiohttp 3.14 stream_writer argument.
    class MockResponse(ClientResponse):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("stream_writer", Mock(output_size=0))
            super().__init__(*args, **kwargs)

    with (
        patch("aioresponses.core.ClientResponse", MockResponse),
        aioresponses() as mock,
    ):
        yield mock
