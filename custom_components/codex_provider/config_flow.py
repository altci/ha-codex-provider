"""Home Assistant device authorization and reconfiguration UI."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import probatio as vol
from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .auth import Credentials, DeviceAuth, DeviceChallenge, Identity, ProviderError
from .const import (
    CONF_CLIENT_ID,
    CONF_CREDENTIALS,
    CONF_MODEL,
    CONF_ORIGINATOR,
    CONF_USER_AGENT,
    DEFAULT_MODEL,
    DOMAIN,
    VERIFICATION_URL,
)

if TYPE_CHECKING:
    import asyncio


class CodexConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Use standard HA progress-task lifecycle, including cancellation."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize transient authorization state; never store challenges on disk."""
        self._data: dict[str, Any] = {}
        self._auth: DeviceAuth | None = None
        self._challenge: DeviceChallenge | None = None
        self._poll_task: asyncio.Task[Credentials] | None = None
        self._credentials: Credentials | None = None
        self._error: str | None = None
        self._entry: config_entries.ConfigEntry | None = None

    async def async_step_user(
        self, user_input: dict | None = None
    ) -> config_entries.ConfigFlowResult:
        """Require all three identity values, with no functional defaults."""
        errors = {}
        if user_input is not None:
            try:
                identity = Identity.from_mapping(user_input)
                model = user_input.get(CONF_MODEL, DEFAULT_MODEL)
                model = _validate_model(model)
                self._data = {**user_input, CONF_MODEL: model}
                if (
                    self.source == config_entries.SOURCE_RECONFIGURE
                    and self._entry is not None
                    and identity == Identity.from_mapping(self._entry.data)
                ):
                    return self.async_update_reload_and_abort(
                        self._entry, data_updates={CONF_MODEL: model}
                    )
                self._auth = DeviceAuth(async_get_clientsession(self.hass), identity)
                self._challenge = await self._auth.request_device_code()
            except ProviderError as error:
                errors["base"] = str(error)
            else:
                return await self.async_step_authorize()
        defaults = user_input or self._data
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_CLIENT_ID,
                        default=defaults.get(CONF_CLIENT_ID, vol.UNDEFINED),
                    ): str,
                    vol.Required(
                        CONF_ORIGINATOR,
                        default=defaults.get(CONF_ORIGINATOR, vol.UNDEFINED),
                    ): str,
                    vol.Required(
                        CONF_USER_AGENT,
                        default=defaults.get(CONF_USER_AGENT, vol.UNDEFINED),
                    ): str,
                    vol.Optional(
                        CONF_MODEL, default=defaults.get(CONF_MODEL, DEFAULT_MODEL)
                    ): str,
                }
            ),
            errors=errors,
        )

    async def async_step_authorize(
        self, _user_input: dict | None = None
    ) -> config_entries.ConfigFlowResult:
        """Display the verification code while polling in a cancellable task."""
        if self._auth is None or self._challenge is None:
            return self.async_abort(reason="invalid_response")
        if self._poll_task is None:
            self._poll_task = self.hass.async_create_task(
                self._auth.poll(self._challenge), "codex_provider_device_login"
            )
        if not self._poll_task.done():
            return self.async_show_progress(
                step_id="authorize",
                progress_action="authorize",
                description_placeholders={
                    "url": VERIFICATION_URL,
                    "code": self._challenge.user_code,
                },
                progress_task=self._poll_task,
            )
        try:
            self._credentials = self._poll_task.result()
        except ProviderError as error:
            self._error = str(error)
        return self.async_show_progress_done(next_step_id="finish")

    async def async_step_finish(
        self, _user_input: dict | None = None
    ) -> config_entries.ConfigFlowResult:
        """Persist credentials only after authorization completes."""
        if self._error or self._credentials is None:
            return self.async_abort(reason=self._error or "invalid_response")
        data = {**self._data, CONF_CREDENTIALS: self._credentials.as_dict()}
        if self._entry is not None:
            return self.async_update_reload_and_abort(self._entry, data=data)
        return self.async_create_entry(title="Codex Provider", data=data)

    async def async_step_reauth(
        self, entry_data: dict
    ) -> config_entries.ConfigFlowResult:
        """Require confirmation before starting a new device login."""
        self._entry = self._get_reauth_entry()
        self._data = {k: v for k, v in entry_data.items() if k != CONF_CREDENTIALS}
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict | None = None
    ) -> config_entries.ConfigFlowResult:
        """Allow users to review identity before authorizing again."""
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm", data_schema=vol.Schema({})
            )
        return await self.async_step_user()

    async def async_step_reconfigure(
        self, user_input: dict | None = None
    ) -> config_entries.ConfigFlowResult:
        """Edit the model or identity; changed identity always needs fresh login."""
        self._entry = self._get_reconfigure_entry()
        self._data = {
            k: v for k, v in self._entry.data.items() if k != CONF_CREDENTIALS
        }
        return await self.async_step_user(user_input)


def _validate_model(model: Any) -> str:
    """Reject empty model identifiers."""
    if not isinstance(model, str) or not model.strip():
        raise ProviderError("invalid_model")
    return model.strip()
