"""Test the real Home Assistant flow manager and its cancellable progress task."""

import asyncio
import time
from unittest.mock import patch

import probatio as vol
import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.codex_provider.auth import (
    Credentials,
    DeviceChallenge,
    ProviderError,
)
from custom_components.codex_provider.const import DOMAIN


@pytest.fixture
async def flow_setup(hass):
    """Keep flow tests focused on the flow rather than platform loading."""
    with patch("custom_components.codex_provider.async_setup_entry", return_value=True):
        yield


async def start(hass, identity_data):
    """Start a user flow and submit identity."""
    initial = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    return await hass.config_entries.flow.async_configure(
        initial["flow_id"], identity_data
    )


async def test_required_fields_without_defaults(hass):
    """Require all three identity fields without defaults."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    fields = {key.schema: key for key in result["data_schema"].schema}
    for name in ("client_id", "originator", "user_agent"):
        assert isinstance(fields[name], vol.Required)
        assert fields[name].default is vol.UNDEFINED
    hass.config_entries.flow.async_abort(result["flow_id"])


async def test_flow_success(hass, identity_data, flow_setup):
    """Display URL/code, finish device login, then create a private credential entry."""
    gate = asyncio.Event()
    credentials = Credentials("fake-access", "fake-refresh", time.time() + 3600)

    async def poll(_challenge):
        await gate.wait()
        return credentials

    with (
        patch(
            "custom_components.codex_provider.config_flow.DeviceAuth.request_device_code",
            return_value=DeviceChallenge("device", "CODE", 5, 600),
        ),
        patch(
            "custom_components.codex_provider.config_flow.DeviceAuth.poll",
            side_effect=poll,
        ),
    ):
        result = await start(hass, identity_data)
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        assert result["description_placeholders"]["code"] == "CODE"
        gate.set()
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"]["credentials"] == credentials.as_dict()
        assert result["data"]["client_id"] == "test-client"


async def test_flow_cancel(hass, identity_data):
    """Aborting the UI cancels the actual poll task."""
    started, stopped = asyncio.Event(), asyncio.Event()

    async def poll(_challenge):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    with (
        patch(
            "custom_components.codex_provider.config_flow.DeviceAuth.request_device_code",
            return_value=DeviceChallenge("device", "CODE", 5, 600),
        ),
        patch(
            "custom_components.codex_provider.config_flow.DeviceAuth.poll",
            side_effect=poll,
        ),
    ):
        result = await start(hass, identity_data)
        await started.wait()
        hass.config_entries.flow.async_abort(result["flow_id"])
        await asyncio.wait_for(stopped.wait(), timeout=1)
        await hass.async_block_till_done()
        assert not hass.config_entries.flow.async_progress()


@pytest.mark.parametrize("error", ["login_expired", "access_denied", "cannot_connect"])
async def test_poll_error(hass, identity_data, error):
    """Poll failures lead to meaningful translated abort reasons."""
    gate = asyncio.Event()

    async def poll(_challenge):
        await gate.wait()
        raise ProviderError(error)

    with (
        patch(
            "custom_components.codex_provider.config_flow.DeviceAuth.request_device_code",
            return_value=DeviceChallenge("device", "CODE", 5, 600),
        ),
        patch(
            "custom_components.codex_provider.config_flow.DeviceAuth.poll",
            side_effect=poll,
        ),
    ):
        result = await start(hass, identity_data)
        gate.set()
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == error


async def test_invalid_identity_and_network(hass, identity_data):
    """Reject invalid identity locally; retain the form on network failure."""
    with patch(
        "custom_components.codex_provider.config_flow.DeviceAuth.request_device_code",
        side_effect=ProviderError("cannot_connect"),
    ) as request:
        result = await start(hass, {**identity_data, "originator": "\r\n"})
        assert result["errors"] == {"base": "invalid_identity"}
        request.assert_not_called()
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], identity_data
        )
        assert result["errors"] == {"base": "cannot_connect"}
        hass.config_entries.flow.async_abort(result["flow_id"])


@pytest.mark.parametrize(
    "source", [config_entries.SOURCE_REAUTH, config_entries.SOURCE_RECONFIGURE]
)
async def test_reauthorize_existing_entry(hass, identity_data, flow_setup, source):
    """Reauthorize existing entries without creating duplicates."""
    original = Credentials("old-access", "old-refresh", 1)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Codex Provider",
        data={**identity_data, "credentials": original.as_dict()},
    )
    entry.add_to_hass(hass)
    gate = asyncio.Event()
    new_credentials = Credentials("new-access", "new-refresh", time.time() + 3600)

    async def poll(_challenge):
        await gate.wait()
        return new_credentials

    with (
        patch(
            "custom_components.codex_provider.config_flow.DeviceAuth.request_device_code",
            return_value=DeviceChallenge("device", "CODE", 5, 600),
        ),
        patch(
            "custom_components.codex_provider.config_flow.DeviceAuth.poll",
            side_effect=poll,
        ),
        patch.object(hass.config_entries, "async_schedule_reload") as reload_entry,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": source, "entry_id": entry.entry_id},
            data=dict(entry.data) if source == config_entries.SOURCE_REAUTH else None,
        )
        if source == config_entries.SOURCE_REAUTH:
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {}
            )
        assert result["type"] is FlowResultType.FORM
        submitted = (
            identity_data
            if source == config_entries.SOURCE_REAUTH
            else {**identity_data, "client_id": "replacement-client"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], submitted
        )
        assert entry.data["credentials"] == original.as_dict()
        gate.set()
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
        assert result["type"] is FlowResultType.ABORT
        assert entry.data["credentials"] == new_credentials.as_dict()
        assert entry.data["client_id"] == submitted["client_id"]
        assert len(hass.config_entries.async_entries(DOMAIN)) == 1
        reload_entry.assert_called_once_with(entry.entry_id)


async def test_model_change_reuses_credentials(hass, identity_data):
    """Save a model change without contacting auth or replacing private tokens."""
    credentials = Credentials("fake-access", "fake-refresh", time.time() + 3600)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Codex Provider",
        data={**identity_data, "credentials": credentials.as_dict()},
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.codex_provider.config_flow.DeviceAuth.request_device_code"
        ) as request,
        patch.object(hass.config_entries, "async_schedule_reload") as reload_entry,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
            },
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**identity_data, "model": "another-http-model"}
        )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reconfigure_successful"
        assert entry.data["model"] == "another-http-model"
        assert entry.data["credentials"] == credentials.as_dict()
        assert len(hass.config_entries.async_entries(DOMAIN)) == 1
        request.assert_not_called()
        reload_entry.assert_called_once_with(entry.entry_id)
