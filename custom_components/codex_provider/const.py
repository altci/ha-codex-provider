"""Constants for Codex Provider."""

DOMAIN = "codex_provider"
CONF_CLIENT_ID = "client_id"
CONF_ORIGINATOR = "originator"
CONF_USER_AGENT = "user_agent"
CONF_MODEL = "model"
CONF_CREDENTIALS = "credentials"
DEFAULT_MODEL = "gpt-5.5"
AUTH_BASE = "https://auth.openai.com"
DEVICE_CODE_URL = f"{AUTH_BASE}/api/accounts/deviceauth/usercode"
DEVICE_POLL_URL = f"{AUTH_BASE}/api/accounts/deviceauth/token"
TOKEN_URL = f"{AUTH_BASE}/oauth/token"
DEVICE_CALLBACK_URL = f"{AUTH_BASE}/deviceauth/callback"
VERIFICATION_URL = f"{AUTH_BASE}/codex/device"
RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
AUTH_TIMEOUT = 15
LOGIN_TIMEOUT = 600
INFERENCE_TIMEOUT = 120
REFRESH_MARGIN = 300
