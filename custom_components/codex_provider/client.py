"""One asynchronous Codex Responses HTTP/SSE transport."""

from __future__ import annotations

import json
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError, ClientTimeout

from .auth import AuthenticationError, ProviderError
from .const import INFERENCE_TIMEOUT, RESPONSES_URL

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from aiohttp import ClientSession

    from .auth import Credentials, Identity, TokenManager


async def _sse_events(content: Any) -> AsyncIterator[dict[str, Any]]:
    """Read SSE records, including multiple data lines and comments."""
    lines: list[str] = []
    async for raw in content:
        try:
            line = raw.decode("utf-8").rstrip("\r\n")
        except UnicodeError:
            raise ProviderError("invalid_response") from None
        if line.startswith("data:"):
            lines.append(line[5:].removeprefix(" "))
        elif not line and lines:
            payload = "\n".join(lines)
            lines.clear()
            if payload == "[DONE]":
                return
            try:
                event = json.loads(payload)
            except ValueError:
                raise ProviderError("invalid_response") from None
            if not isinstance(event, dict):
                raise ProviderError("invalid_response")
            yield event
    # A successful stream must contain a terminal event before EOF.


def _completed_text(response: dict[str, Any]) -> str:
    """Read only assistant output text, never tool arguments or reasoning."""
    parts: list[str] = []
    output = response.get("output", [])
    if not isinstance(output, list):
        raise ProviderError("invalid_response")
    for item in output:
        if (
            not isinstance(item, dict)
            or item.get("type") != "message"
            or item.get("role") != "assistant"
        ):
            continue
        content = item.get("content", [])
        if not isinstance(content, list):
            raise ProviderError("invalid_response")
        for part in content:
            if (
                isinstance(part, dict)
                and part.get("type") == "output_text"
                and isinstance(part.get("text"), str)
            ):
                parts.append(part["text"])  # noqa: PERF401 - filter untrusted event data
    return "".join(parts)


class CodexClient:
    """Send text-only inference requests with tools explicitly disabled."""

    def __init__(
        self, session: ClientSession, identity: Identity, tokens: TokenManager
    ) -> None:
        """Use the Home Assistant session and per-entry credential manager."""
        self.session, self.identity, self.tokens = session, identity, tokens

    async def generate(
        self, model: str, instructions: str, messages: list[dict[str, Any]]
    ) -> str:
        """Generate text with a finite total timeout and explicit terminal success."""
        credentials = await self.tokens.async_credentials()
        headers = {
            **self.identity.headers,
            "Authorization": f"Bearer {credentials.access_token}",
            "OpenAI-Beta": "responses=experimental",
            "Accept": "text/event-stream",
        }
        if credentials.account_id:
            headers["chatgpt-account-id"] = credentials.account_id
        body = {
            "model": model,
            "store": False,
            "stream": True,
            "instructions": instructions or "You are a helpful assistant.",
            "input": messages,
            "tools": [],
            "tool_choice": "none",
            "parallel_tool_calls": False,
        }
        try:
            async with self.session.post(
                RESPONSES_URL,
                headers=headers,
                json=body,
                timeout=ClientTimeout(total=INFERENCE_TIMEOUT),
                allow_redirects=False,
            ) as response:
                if response.status == HTTPStatus.UNAUTHORIZED:
                    raise AuthenticationError("invalid_auth")
                if response.status != HTTPStatus.OK:
                    # Do not include a raw response body, which can echo secrets.
                    raise ProviderError(
                        f"Codex backend returned HTTP {response.status}"
                    )
                return await self._read_response(response.content, credentials)
        except TimeoutError:
            raise ProviderError("Codex backend request timed out") from None
        except ClientError:
            raise ProviderError("Codex backend connection failed") from None

    @staticmethod
    async def _read_response(content: Any, credentials: Credentials) -> str:
        parts: list[str] = []
        async for event in _sse_events(content):
            kind = event.get("type")
            if kind in ("error", "response.failed", "response.incomplete"):
                raise ProviderError(
                    "Codex backend failed or returned an incomplete response"
                )
            if kind in ("response.output_text.delta", "response.text.delta"):
                delta = event.get("delta")
                if not isinstance(delta, str):
                    raise ProviderError("invalid_response")
                parts.append(delta)
            elif kind == "response.completed":
                result = event.get("response", {})
                if (
                    not isinstance(result, dict)
                    or result.get("status", "completed") != "completed"
                ):
                    raise ProviderError("Codex backend did not complete the response")
                text = "".join(parts) or _completed_text(result)
                if not text.strip():
                    raise ProviderError("Codex backend returned no assistant text")
                return credentials.redact(text)
        raise ProviderError("Codex backend closed the stream before completion")
