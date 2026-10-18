"""Exercise device login, expiry, and persisted token rotation."""

import asyncio
import base64
import json
import time
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import ClientSession
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.codex_provider.auth import (
    AuthenticationError,
    Credentials,
    DeviceAuth,
    DeviceChallenge,
    Identity,
    ProviderError,
    TokenManager,
)
from custom_components.codex_provider.const import (
    DEVICE_CODE_URL,
    DEVICE_POLL_URL,
    TOKEN_URL,
)


@pytest.mark.parametrize("field", ["client_id", "originator", "user_agent"])
@pytest.mark.parametrize(
    "value", ["", " ", "a\r\nb", "\x00", "é", "a\x7f", None, " trailing "]
)
def test_identity_rejects_unsafe(identity_data, field, value):
    """No identity defaults, empty values, or header injection."""
    identity_data[field] = value
    with pytest.raises(ProviderError, match="invalid_identity"):
        Identity.from_mapping(identity_data)


async def test_device_login(responses, identity_data, token_data):
    """Pending and slow-down responses respect intervals, then exchange the code."""
    responses.post(
        DEVICE_CODE_URL,
        payload={"device_auth_id": "device", "usercode": "CODE", "interval": "7"},
    )
    responses.post(DEVICE_POLL_URL, status=403)
    responses.post(DEVICE_POLL_URL, status=400, payload={"error": "slow_down"})
    responses.post(
        DEVICE_POLL_URL,
        payload={"authorization_code": "code", "code_verifier": "verifier"},
    )
    responses.post(TOKEN_URL, payload=token_data)
    async with ClientSession() as session:
        auth = DeviceAuth(session, Identity.from_mapping(identity_data))
        challenge = await auth.request_device_code()
        with patch(
            "custom_components.codex_provider.auth.asyncio.sleep",
            new_callable=AsyncMock,
        ) as sleep:
            credentials = await auth.poll(challenge)
        assert [call.args[0] for call in sleep.call_args_list] == [7, 7, 12]
        assert credentials.refresh_token == "fake-refresh"
        requests = [
            call
            for (method, url), calls in responses.requests.items()
            if str(url) == TOKEN_URL
            for call in calls
        ]
        assert requests[0].kwargs["data"]["client_id"] == "test-client"
        assert requests[0].kwargs["headers"]["originator"] == "test-origin"


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (400, {"error": "access_denied"}, "access_denied"),
        (400, {"error": "expired_token"}, "login_expired"),
        (401, {}, "invalid_auth"),
        (500, {}, "cannot_connect"),
        (200, {"authorization_code": "code"}, "invalid_response"),
    ],
)
async def test_device_failures(responses, identity_data, status, body, error):
    """Denial, expiry, credentials, network, and malformed responses fail safely."""
    responses.post(DEVICE_POLL_URL, status=status, payload=body)
    async with ClientSession() as session:
        auth = DeviceAuth(session, Identity.from_mapping(identity_data))
        with (
            patch(
                "custom_components.codex_provider.auth.asyncio.sleep",
                new_callable=AsyncMock,
            ),
            pytest.raises(ProviderError, match=error),
        ):
            await auth.poll(DeviceChallenge("device", "CODE", 5, 60))


async def test_timeout_and_cancellation(identity_data):
    """Polling never blocks the loop and cancellation propagates."""
    async with ClientSession() as session:
        auth = DeviceAuth(session, Identity.from_mapping(identity_data))
        with pytest.raises(ProviderError, match="login_expired"):
            await auth.poll(DeviceChallenge("device", "CODE", 5, 0.01))
        task = asyncio.create_task(auth.poll(DeviceChallenge("device", "CODE", 5, 60)))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_refresh_persistence_and_restart(
    hass, identity_data, token_data, responses
):
    """Concurrent calls rotate once; a reconstructed manager uses persisted data."""
    entry = MockConfigEntry(
        domain="codex_provider",
        data={
            **identity_data,
            "credentials": Credentials("old-access", "old-refresh", 1).as_dict(),
        },
    )
    entry.add_to_hass(hass)
    responses.post(TOKEN_URL, payload=token_data)
    async with ClientSession() as session:
        auth = DeviceAuth(session, Identity.from_mapping(identity_data))
        manager = TokenManager(hass, entry, auth)
        first, second = await asyncio.gather(
            manager.async_credentials(), manager.async_credentials()
        )
        assert first == second
        assert entry.data["credentials"]["refresh_token"] == "fake-refresh"
        restored = MockConfigEntry(domain="codex_provider", data=dict(entry.data))
        restored.add_to_hass(hass)
        assert await TokenManager(hass, restored, auth).async_credentials() == first
    assert sum(len(calls) for calls in responses.requests.values()) == 1


@pytest.mark.parametrize(
    ("status", "body", "exception"),
    [
        (400, {"error": "invalid_grant"}, AuthenticationError),
        (400, {"error": {"code": "refresh_token_reused"}}, AuthenticationError),
        (401, {}, AuthenticationError),
        (503, {}, ProviderError),
        (429, {}, ProviderError),
    ],
)
async def test_refresh_failures(
    hass, identity_data, responses, status, body, exception
):
    """Permanent failures require reauth while transient failures retain tokens."""
    data = {
        **identity_data,
        "credentials": Credentials("old-access", "old-refresh", 1).as_dict(),
    }
    entry = MockConfigEntry(domain="codex_provider", data=data)
    entry.add_to_hass(hass)
    responses.post(TOKEN_URL, status=status, payload=body)
    async with ClientSession() as session:
        manager = TokenManager(
            hass, entry, DeviceAuth(session, Identity.from_mapping(identity_data))
        )
        with pytest.raises(exception):
            await manager.async_credentials()
    assert dict(entry.data) == data


def test_jwt_expiry_and_refresh_retention(token_data):
    """JWT is metadata only; omitted refresh tokens retain the current value."""
    claims = {
        "exp": time.time() + 1000,
        "https://api.openai.com/auth": {"chatgpt_account_id": "account"},
    }
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    token_data["access_token"] = f"header.{payload}.signature"
    del token_data["expires_in"]
    credentials = Credentials.from_token_response(token_data)
    assert credentials.account_id == "account"
    assert credentials.expires_at == claims["exp"]
    refreshed = Credentials.from_token_response(
        {"access_token": "new-access", "expires_in": 3600}, credentials
    )
    assert refreshed.refresh_token == credentials.refresh_token
    assert refreshed.account_id == "account"
    assert "new-access" not in repr(refreshed)
