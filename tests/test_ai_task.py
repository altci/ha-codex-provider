"""Validate actual Core 2026.10 task and chat-log classes."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import probatio as vol
import pytest
from homeassistant.components import ai_task, conversation
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector

from custom_components.codex_provider.ai_task import CodexAITaskEntity
from custom_components.codex_provider.auth import AuthenticationError


def entity(response="hello"):
    """Build an entity with an asynchronous inference boundary."""
    entry = SimpleNamespace(
        entry_id="entry",
        data={"model": "gpt-5.5"},
        runtime_data=SimpleNamespace(generate=AsyncMock(return_value=response)),
        async_start_reauth=Mock(),
    )
    result = CodexAITaskEntity(entry)
    result.entity_id = "ai_task.codex_provider"
    return result


def chat(hass):
    """Core appends task.instructions before invoking the provider."""
    return conversation.ChatLog(
        hass=hass,
        conversation_id="conversation",
        content=[
            conversation.SystemContent(content="system"),
            conversation.UserContent(content="say hello"),
        ],
    )


async def test_task_success(hass):
    """Return the native result and append one assistant message."""
    provider, log = entity(), chat(hass)
    result = await provider._async_generate_data(
        ai_task.GenDataTask(name="test", instructions="say hello"), log
    )
    assert isinstance(result, ai_task.GenDataTaskResult)
    assert result.as_dict() == {"conversation_id": "conversation", "data": "hello"}
    assert log.content[-1].content == "hello"
    assert log.content[-1].role == "assistant"
    assert provider.supported_features == ai_task.AITaskEntityFeature.GENERATE_DATA
    assert provider.unique_id == "entry"
    assert provider.entry.runtime_data.generate.call_args.args[2] == [
        {"role": "user", "content": "say hello"}
    ]


@pytest.mark.parametrize(
    ("text", "valid"),
    [
        ('{"answer":true}', True),
        ("not JSON", False),
        ('{"answer":"bad"}', False),
        ("{}", False),
    ],
)
async def test_structured_data(hass, text, valid):
    """HA selector conversion and probatio validation enforce the requested shape."""
    schema = vol.Schema({vol.Required("answer"): selector.BooleanSelector()})
    provider, log = entity(text), chat(hass)
    task = ai_task.GenDataTask(name="test", instructions="say hello", structure=schema)
    if valid:
        result = await provider._async_generate_data(task, log)
        assert result.data == {"answer": True}
    else:
        with pytest.raises(HomeAssistantError, match="does not match"):
            await provider._async_generate_data(task, log)
        assert len(log.content) == 2
    assert '"type": "boolean"' in provider.entry.runtime_data.generate.call_args.args[1]


async def test_task_reauthentication(hass):
    """Permanent authorization failure starts the standard HA reauth flow."""
    provider = entity()
    provider.hass = hass
    provider.entry.runtime_data.generate.side_effect = AuthenticationError(
        "invalid_auth"
    )
    with pytest.raises(HomeAssistantError, match="sign in again"):
        await provider._async_generate_data(
            ai_task.GenDataTask(name="test", instructions="say hello"), chat(hass)
        )
    provider.entry.async_start_reauth.assert_called_once_with(hass)
