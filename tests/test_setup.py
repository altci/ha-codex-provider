"""Exercise platform loading and the public ai_task.generate_data service."""

import time

import pytest
from homeassistant.components import ai_task
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.codex_provider.auth import Credentials
from custom_components.codex_provider.const import DOMAIN, RESPONSES_URL, TOKEN_URL

from .test_client import sse


async def test_public_service_and_reload(hass, identity_data, responses):
    """A real platform entity serves requests before and after entry unload/reload."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Codex Provider",
        data={
            **identity_data,
            "credentials": Credentials(
                "fake-access", "fake-refresh", time.time() + 3600
            ).as_dict(),
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    assert len(entities) == 1
    entity_id = entities[0].entity_id
    assert entity_id.startswith("ai_task.")
    assert (
        hass.states.get(entity_id).attributes["supported_features"]
        == ai_task.AITaskEntityFeature.GENERATE_DATA
    )
    event = {
        "type": "response.completed",
        "response": {
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "hello"}],
                }
            ]
        },
    }
    responses.post(RESPONSES_URL, body=sse(event), repeat=True)
    result = await hass.services.async_call(
        "ai_task",
        "generate_data",
        {"entity_id": entity_id, "task_name": "greeting", "instructions": "say hello"},
        blocking=True,
        return_response=True,
    )
    assert result["data"] == "hello"
    assert result["conversation_id"]
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await ai_task.async_generate_data(
        hass, entity_id=entity_id, task_name="again", instructions="say hello again"
    )
    assert result.data == "hello"
    assert (
        er.async_entries_for_config_entry(registry, entry.entry_id)[0].entity_id
        == entity_id
    )


@pytest.mark.parametrize("permanent", [True, False])
async def test_setup_refresh_failure(hass, identity_data, responses, permanent):
    """Setup delegates auth and transient failures to HA's standard recovery paths."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            **identity_data,
            "credentials": Credentials("old-access", "old-refresh", 1).as_dict(),
        },
    )
    entry.add_to_hass(hass)
    responses.post(
        TOKEN_URL,
        status=400 if permanent else 503,
        payload={"error": "invalid_grant"} if permanent else {},
    )
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is (
        ConfigEntryState.SETUP_ERROR if permanent else ConfigEntryState.SETUP_RETRY
    )
    flows = hass.config_entries.flow.async_progress()
    assert bool(flows) is permanent
    if flows:
        hass.config_entries.flow.async_abort(flows[0]["flow_id"])
