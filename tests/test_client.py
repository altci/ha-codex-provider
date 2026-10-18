"""Verify SSE terminal events, transport failures, and tool-free requests."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientSession

from custom_components.codex_provider.auth import (
    AuthenticationError,
    Credentials,
    Identity,
    ProviderError,
)
from custom_components.codex_provider.client import CodexClient
from custom_components.codex_provider.const import RESPONSES_URL


def sse(*events):
    """Build SSE records without live provider traffic."""
    return "".join("data: " + json.dumps(event) + "\n\n" for event in events)


def client(session, identity_data):
    """Use fixed fake credentials."""
    return CodexClient(
        session,
        Identity.from_mapping(identity_data),
        SimpleNamespace(
            async_credentials=AsyncMock(
                return_value=Credentials(
                    "fake-access", "fake-refresh", time.time() + 3600, "account"
                )
            )
        ),
    )


async def test_success(responses, identity_data):
    """Collect deltas once and send user identity with all tool execution disabled."""
    responses.post(
        RESPONSES_URL,
        body=sse(
            {"type": "response.output_text.delta", "delta": "hello "},
            {"type": "response.output_text.delta", "delta": "world"},
            {"type": "response.completed", "response": {"status": "completed"}},
        ),
    )
    async with ClientSession() as session:
        assert (
            await client(session, identity_data).generate(
                "gpt-5.5", "system", [{"role": "user", "content": "hi"}]
            )
            == "hello world"
        )
    request = next(iter(responses.requests.values()))[0]
    assert request.kwargs["headers"]["User-Agent"] == "test-agent"
    assert request.kwargs["headers"]["chatgpt-account-id"] == "account"
    assert request.kwargs["json"]["tools"] == []
    assert request.kwargs["json"]["tool_choice"] == "none"
    assert request.kwargs["json"]["store"] is False


@pytest.mark.parametrize("event", ["error", "response.failed", "response.incomplete"])
async def test_terminal_error(responses, identity_data, event):
    """Even partial assistant text cannot hide a terminal error."""
    responses.post(
        RESPONSES_URL,
        body=sse(
            {"type": "response.output_text.delta", "delta": "partial"},
            {"type": event, "error": {"message": "fake-access"}},
        ),
    )
    async with ClientSession() as session:
        with pytest.raises(
            ProviderError, match="failed or returned an incomplete"
        ) as exc:
            await client(session, identity_data).generate("gpt-5.5", "", [])
        assert "fake-access" not in str(exc.value)


@pytest.mark.parametrize(
    "body",
    [
        "data: [DONE]\n\n",
        sse({"type": "response.output_text.delta", "delta": "partial"}),
        sse({"type": "response.completed", "response": {}}),
        "data: malformed\n\n",
        "data: []\n\n",
        sse({"type": "response.completed", "response": {"output": None}}),
    ],
)
async def test_invalid_or_truncated_stream(responses, identity_data, body):
    """EOF, malformed events, and empty completions never succeed."""
    responses.post(RESPONSES_URL, body=body)
    async with ClientSession() as session:
        with pytest.raises(ProviderError):
            await client(session, identity_data).generate("gpt-5.5", "", [])


async def test_completed_output_and_redaction(responses, identity_data):
    """Completion snapshots supply text if deltas are absent; secrets are redacted."""
    responses.post(
        RESPONSES_URL,
        body=sse(
            {
                "type": "response.completed",
                "response": {
                    "output": [
                        {"type": "function_call", "arguments": "never execute"},
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": "token fake-access"}
                            ],
                        },
                    ]
                },
            }
        ),
    )
    async with ClientSession() as session:
        assert (
            await client(session, identity_data).generate("gpt-5.5", "", [])
            == "token [REDACTED]"
        )


@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_http_failure(responses, identity_data, status):
    """Auth versus backend errors are safe and do not expose response bodies."""
    responses.post(RESPONSES_URL, status=status, body="fake-access")
    async with ClientSession() as session:
        with pytest.raises(
            AuthenticationError if status == 401 else ProviderError
        ) as exc:
            await client(session, identity_data).generate("gpt-5.5", "", [])
        assert "fake-access" not in str(exc.value)


async def test_timeout(responses, identity_data):
    """Backend timeout is a meaningful local error."""
    responses.post(RESPONSES_URL, exception=TimeoutError())
    async with ClientSession() as session:
        with pytest.raises(ProviderError, match="timed out"):
            await client(session, identity_data).generate("gpt-5.5", "", [])


async def test_cancel(responses, identity_data):
    """Cancellation is propagated rather than converted into a provider error."""
    responses.post(RESPONSES_URL, exception=asyncio.CancelledError())
    async with ClientSession() as session:
        with pytest.raises(asyncio.CancelledError):
            await client(session, identity_data).generate("gpt-5.5", "", [])
