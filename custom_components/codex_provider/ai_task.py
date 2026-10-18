"""Native Home Assistant AI Task entity for text and validated JSON."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import probatio as vol
from homeassistant.components import ai_task, conversation
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm
from homeassistant.util.json import json_loads

from .auth import AuthenticationError, ProviderError
from .const import CONF_MODEL, DEFAULT_MODEL

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import CodexConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: CodexConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create exactly one entity per config entry."""
    async_add_entities([CodexAITaskEntity(entry)])


class CodexAITaskEntity(ai_task.AITaskEntity):
    """Generate data without tools, attachments, or filesystem access."""

    _attr_has_entity_name = True
    _attr_name = "Codex Provider"
    _attr_supported_features = ai_task.AITaskEntityFeature.GENERATE_DATA

    def __init__(self, entry: CodexConfigEntry) -> None:
        """Tie entity identity to the stable config entry ID."""
        self.entry = entry
        self._attr_unique_id = entry.entry_id

    async def _async_generate_data(
        self, task: ai_task.GenDataTask, chat_log: conversation.ChatLog
    ) -> ai_task.GenDataTaskResult:
        """Use the Core-provided chat context and validate before appending a reply."""
        if task.attachments or task.llm_api is not None:
            raise HomeAssistantError(
                "Codex Provider does not support attachments or tools"
            )
        instructions: list[str] = []
        messages: list[dict] = []
        for content in chat_log.content:
            if isinstance(content, conversation.SystemContent):
                instructions.append(content.content)
            elif (
                isinstance(
                    content, (conversation.UserContent, conversation.AssistantContent)
                )
                and content.content
            ):
                messages.append({"role": content.role, "content": content.content})
        # Core 2026.10 has already appended task.instructions to the chat log.
        if task.structure is not None:
            try:
                schema = vol.to_openapi(
                    task.structure,
                    custom_serializer=llm.selector_serializer,
                    openapi_version="3.1.0",
                )
                json_validator = vol.from_json_schema(schema)
            except ValueError, TypeError, vol.Invalid, vol.SchemaError:
                raise HomeAssistantError(
                    "Cannot convert the requested output schema"
                ) from None
            instructions.append(
                "Return only JSON matching this schema, with no Markdown fences: "
                + json.dumps(schema)
            )
        try:
            text = await self.entry.runtime_data.generate(
                self.entry.data.get(CONF_MODEL, DEFAULT_MODEL),
                "\n\n".join(instructions),
                messages,
            )
        except AuthenticationError:
            self.entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                "Codex authorization failed; sign in again in Settings"
            ) from None
        except ProviderError as error:
            raise HomeAssistantError(str(error)) from None

        data = text
        if task.structure is not None:
            try:
                parsed = json_validator(json_loads(text))
                data = task.structure(parsed)
            except ValueError, TypeError, vol.Invalid:
                raise HomeAssistantError(
                    "Codex returned JSON that does not match the requested schema"
                ) from None
        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(agent_id=self.entity_id, content=text)
        )
        return ai_task.GenDataTaskResult(
            conversation_id=chat_log.conversation_id, data=data
        )
