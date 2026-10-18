"""Codex Provider: a thin Home Assistant AI Task adapter."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .auth import AuthenticationError, DeviceAuth, Identity, ProviderError, TokenManager
from .client import CodexClient

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

PLATFORMS = [Platform.AI_TASK]
type CodexConfigEntry = ConfigEntry[CodexClient]


async def async_setup_entry(hass: HomeAssistant, entry: CodexConfigEntry) -> bool:
    """Restore credentials, refresh if needed, and expose one AI Task entity."""
    session = async_get_clientsession(hass)
    identity = Identity.from_mapping(entry.data)
    tokens = TokenManager(hass, entry, DeviceAuth(session, identity))
    try:
        await tokens.async_credentials()
    except AuthenticationError:
        raise ConfigEntryAuthFailed(
            "Codex authorization expired; sign in again"
        ) from None
    except ProviderError:
        raise ConfigEntryNotReady("Cannot refresh Codex credentials") from None
    entry.runtime_data = CodexClient(session, identity, tokens)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CodexConfigEntry) -> bool:
    """Unload the AI Task platform."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
