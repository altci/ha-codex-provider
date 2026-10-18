"""Async device authorization with injected identity and config-entry storage."""

from __future__ import annotations

import asyncio
import base64
import json
import math
import time
from dataclasses import asdict, dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError, ClientTimeout

from .const import (
    AUTH_TIMEOUT,
    CONF_CLIENT_ID,
    CONF_CREDENTIALS,
    CONF_ORIGINATOR,
    CONF_USER_AGENT,
    DEVICE_CALLBACK_URL,
    DEVICE_CODE_URL,
    DEVICE_POLL_URL,
    LOGIN_TIMEOUT,
    REFRESH_MARGIN,
    TOKEN_URL,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from aiohttp import ClientSession
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


class ProviderError(Exception):
    """A safe, local error code; never include provider bodies or credentials."""


class AuthenticationError(ProviderError):
    """Permanent authorization failure requiring fresh login."""


@dataclass(frozen=True, repr=False)
class Identity:
    """User-supplied OAuth client identity. There are no defaults."""

    client_id: str
    originator: str
    user_agent: str

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Identity:
        """Reject missing, blank, non-ASCII, or unsafe header/identity values."""
        values = [
            data.get(k) for k in (CONF_CLIENT_ID, CONF_ORIGINATOR, CONF_USER_AGENT)
        ]
        for value in values:
            if (
                not isinstance(value, str)
                or not value.strip()
                or value != value.strip()
                or not value.isascii()
                or not value.isprintable()
            ):
                raise ProviderError("invalid_identity")
        return cls(*values)

    @property
    def headers(self) -> dict[str, str]:
        """Return only explicitly configured provider identity headers."""
        return {"originator": self.originator, "User-Agent": self.user_agent}


@dataclass(frozen=True, repr=False)
class Credentials:
    """Private config-entry credentials; timestamps are Unix seconds."""

    access_token: str
    refresh_token: str
    expires_at: float
    account_id: str | None = None

    @classmethod
    def from_token_response(
        cls, data: dict[str, Any], previous: Credentials | None = None
    ) -> Credentials:
        """Resolve expiry and account metadata without trusting JWT claims as auth."""
        access = data.get("access_token")
        refresh = data.get(
            "refresh_token", previous.refresh_token if previous else None
        )
        if not _nonempty(access) or not _nonempty(refresh):
            raise ProviderError("invalid_response")
        claims: dict[str, Any] = {}
        try:
            parts = access.split(".")
            if len(parts) == 3:  # noqa: PLR2004 - JWT has exactly three segments
                decoded = json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
                if isinstance(decoded, dict):
                    claims = decoded
        except ValueError, UnicodeError:
            pass
        duration = data.get("expires_in")
        if _positive_number(duration):
            expiry = time.time() + float(duration)
        elif _positive_number(claims.get("exp")):
            expiry = float(claims["exp"])
        else:
            raise ProviderError("invalid_response")
        metadata = claims.get("https://api.openai.com/auth", {})
        account = (
            metadata.get("chatgpt_account_id") if isinstance(metadata, dict) else None
        )
        if not _nonempty(account):
            account = previous.account_id if previous else None
        return cls(access, refresh, expiry, account)

    def as_dict(self) -> dict[str, Any]:
        """Serialize only into private config-entry data."""
        return asdict(self)

    def redact(self, text: str) -> str:
        """Prevent echoed credential values from reaching service results."""
        for secret in sorted(
            (self.access_token, self.refresh_token), key=len, reverse=True
        ):
            text = text.replace(secret, "[REDACTED]")
        return text


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _positive_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (float, int))
        and math.isfinite(value)
        and value > 0
    )


@dataclass(frozen=True, repr=False)
class DeviceChallenge:
    """Short-lived device login state, held only by the config flow."""

    device_auth_id: str
    user_code: str
    interval: float
    expires_in: float


def _error_code(data: dict[str, Any]) -> str | None:
    """Normalize the provider's OAuth string and nested error code shapes."""
    error = data.get("error")
    if isinstance(error, dict):
        error = error.get("code")
    return error if isinstance(error, str) else None


class DeviceAuth:
    """Implement the provider's device-code and token HTTP protocol asynchronously."""

    def __init__(self, session: ClientSession, identity: Identity) -> None:
        """Use Home Assistant's shared session."""
        self.session = session
        self.identity = identity

    async def _request(
        self, url: str, payload: dict[str, str], *, form: bool = False
    ) -> tuple[int, dict[str, Any]]:
        try:
            async with self.session.post(
                url,
                headers=self.identity.headers,
                timeout=ClientTimeout(total=AUTH_TIMEOUT),
                allow_redirects=False,
                **({"data": payload} if form else {"json": payload}),
            ) as response:
                try:
                    data = await response.json(content_type=None)
                except ValueError, UnicodeError:
                    data = {}
                return response.status, data if isinstance(data, dict) else {}
        except ClientError, TimeoutError:
            raise ProviderError("cannot_connect") from None

    @staticmethod
    def _check(status: int, data: dict[str, Any]) -> None:
        if status in (400, 401, 403) and _error_code(data) in (
            "invalid_grant",
            "invalid_client",
            "access_denied",
            "unauthorized_client",
            "refresh_token_reused",
            "refresh_token_expired",
            "refresh_token_invalid",
        ):
            raise AuthenticationError("invalid_auth")
        if status in (401, 403):
            raise AuthenticationError("invalid_auth")
        if status != HTTPStatus.OK:
            raise ProviderError(
                "cannot_connect"
                if status >= HTTPStatus.INTERNAL_SERVER_ERROR
                or status == HTTPStatus.TOO_MANY_REQUESTS
                else "invalid_response"
            )

    async def request_device_code(self) -> DeviceChallenge:
        """Request a challenge using the user's OAuth client ID."""
        status, data = await self._request(
            DEVICE_CODE_URL, {"client_id": self.identity.client_id}
        )
        self._check(status, data)
        device_id = data.get("device_auth_id")
        user_code = data.get("user_code") or data.get("usercode")
        try:
            interval = float(data.get("interval", 5))
            expires = float(data.get("expires_in", LOGIN_TIMEOUT))
        except ValueError, TypeError:
            raise ProviderError("invalid_response") from None
        if (
            not _nonempty(device_id)
            or not _nonempty(user_code)
            or not _positive_number(interval)
            or not _positive_number(expires)
        ):
            raise ProviderError("invalid_response")
        return DeviceChallenge(
            device_id, user_code, max(5, interval), min(LOGIN_TIMEOUT, expires)
        )

    async def poll(self, challenge: DeviceChallenge) -> Credentials:
        """Poll with provider intervals, cancellation, and a total timeout."""
        try:
            async with asyncio.timeout(challenge.expires_in):
                interval = challenge.interval
                while True:
                    await asyncio.sleep(interval)
                    status, data = await self._request(
                        DEVICE_POLL_URL,
                        {
                            "device_auth_id": challenge.device_auth_id,
                            "user_code": challenge.user_code,
                        },
                    )
                    error = _error_code(data)
                    if error in ("access_denied", "authorization_declined"):
                        raise ProviderError("access_denied")
                    if error in ("expired_token", "expired_device_code"):
                        raise ProviderError("login_expired")
                    if error == "slow_down":
                        interval += 5
                        continue
                    # The provider uses 403/404 for pending approval.
                    if error == "authorization_pending" or (
                        status in (403, 404) and not error
                    ):
                        continue
                    self._check(status, data)
                    code, verifier = (
                        data.get("authorization_code"),
                        data.get("code_verifier"),
                    )
                    if not _nonempty(code) or not _nonempty(verifier):
                        raise ProviderError("invalid_response")
                    status, tokens = await self._request(
                        TOKEN_URL,
                        {
                            "grant_type": "authorization_code",
                            "code": code,
                            "code_verifier": verifier,
                            "redirect_uri": DEVICE_CALLBACK_URL,
                            "client_id": self.identity.client_id,
                        },
                        form=True,
                    )
                    self._check(status, tokens)
                    return Credentials.from_token_response(tokens)
        except TimeoutError:
            raise ProviderError("login_expired") from None

    async def refresh(self, credentials: Credentials) -> Credentials:
        """Refresh credentials, retaining the refresh token if not rotated."""
        status, data = await self._request(
            TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "refresh_token": credentials.refresh_token,
                "client_id": self.identity.client_id,
            },
            form=True,
        )
        self._check(status, data)
        return Credentials.from_token_response(data, credentials)


class TokenManager:
    """Serialize refreshes and persist rotated credentials in the config entry."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, auth: DeviceAuth
    ) -> None:
        """Retain only the config entry as the credential source of truth."""
        self.hass, self.entry, self.auth = hass, entry, auth
        self._lock = asyncio.Lock()

    async def async_credentials(self) -> Credentials:
        """Refresh before inference; HA handles permanent auth failures."""
        async with self._lock:
            credentials = Credentials(**self.entry.data[CONF_CREDENTIALS])
            if credentials.expires_at > time.time() + REFRESH_MARGIN:
                return credentials
            credentials = await self.auth.refresh(credentials)
            self.hass.config_entries.async_update_entry(
                self.entry,
                data={**self.entry.data, CONF_CREDENTIALS: credentials.as_dict()},
            )
            return credentials
