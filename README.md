# Codex Provider for Home Assistant

A HACS custom integration for Home Assistant Core **2026.10.0+**. It exposes one
native `ai_task` entity per config entry and calls ChatGPT's Codex Responses backend
directly. No CLI, sidecar, app-server, or add-on is needed.

**Implementation preview:** automated tests use mocked provider responses. A real
account login, model request, HACS installation, and post-restart refresh still
need the manual acceptance checks below before a release can be called complete.

## Install

1. In HACS, open **Custom repositories**, add
   `https://github.com/altci/ha-codex-provider`, and select **Integration**.
2. Download **Codex Provider**, then restart Home Assistant.
3. Open **Settings → Devices & services → Add integration → Codex Provider**.
4. Supply your own **OAuth client ID**, **Originator**, and **User-Agent**.
   All three are mandatory, with no built-in identity or fallback. Use the identity
   you are authorized to use; these strings alone do not grant backend access.
5. Open the verification link shown in the flow, enter its code, and approve the
   ChatGPT login. Keep the Home Assistant dialog open until setup completes.
   Cancelling the dialog stops polling; authorization times out within ten minutes.

For manual installation, copy `custom_components/codex_provider` into your Home
Assistant configuration's `custom_components` directory and restart.

## Model and transport

The initial model setting is **`gpt-5.5`**, matching the HTTP/SSE reference.
Change it through the integration entry's **Reconfigure** menu. Model-only changes
reuse your existing credentials and reload the integration automatically; no new
device authorization or Home Assistant restart is needed. Changing any client
identity field requires a new device authorization.
There is no model discovery or transport negotiation.

This integration implements **HTTP/SSE only**, at the fixed
`https://chatgpt.com/backend-api/codex/responses` endpoint. The supported target is
`gpt-5.5` with Codex HTTP/SSE access; availability depends on your account and
client identity and has not been verified with live credentials here. Models
requiring WebSocket or Responses-Lite are unsupported. An HTTP error is reported
explicitly; the integration does not silently choose another transport or model.

This backend is not a guaranteed stable public API. Provider changes can break
authorization or inference. Report failures without including tokens, login
codes, or raw provider response bodies.

## Run an AI Task

In **Developer tools → Actions**, use the entity ID created by your installation:

```yaml
action: ai_task.generate_data
data:
  entity_id: ai_task.codex_provider
  task_name: greeting
  instructions: "Write one short greeting."
```

The action returns `conversation_id` and generated `data`. It sends the relevant
Home Assistant chat-log text to the provider and appends the assistant reply.

Structured generation uses Home Assistant's schema conversion to describe the
requested JSON shape in the prompt, then validates JSON types with probatio’s JSON Schema validator and applies the
original task `probatio.Schema`. Native backend schema enforcement is not assumed.
Malformed JSON or schema violations produce a clear error; invalid text is never
returned as a successful structured result. For example:

```yaml
action: ai_task.generate_data
data:
  entity_id: ai_task.codex_provider
  task_name: decision
  instructions: "Set answer to true. Return JSON."
  structure:
    answer:
      required: true
      selector:
        boolean: {}
```

## Credentials and limits

Access tokens, refresh tokens, Unix expiry, and any account identifier are stored
in private config-entry data, restored across restarts, and refreshed within five
minutes of expiry before inference. Rotated credentials are persisted. Protect
Home Assistant's configuration storage and backups; private config-entry storage
is not a separate encrypted vault. No tokens are included in entity attributes or
provider error messages. Permanent refresh failures start Home Assistant's standard
reauthentication flow; transient network failures retain credentials for a later
request. Inference authorization failures prompt reauthentication.

Only `GENERATE_DATA` is advertised. There are no attachments, image generation,
Assist conversation agent, tools, shell/filesystem access, arbitrary endpoints,
usage dashboard, automatic retries, or model catalog. Requests disable tools and
use a 120-second total inference timeout. Failed, incomplete, empty, malformed, or
truncated streams fail explicitly. Task instructions and chat context leave Home
Assistant and are sent to ChatGPT.

## Development and acceptance

Use Python 3.14 and `scripts/setup`, then:

```sh
python -m pytest -q
python -m ruff check .
python -m ruff format . --check
```

Tests cover device pending/success/denial/expiry/cancellation, mandatory identity,
refresh rotation and permanent/transient failures, restored credential state,
config-flow lifecycle, AI Task conversion, schema validation, and backend stream
errors and timeouts. Tests never require live credentials.

Before release, perform these checks on a real Home Assistant 2026.10+ instance:

- Install from this repository through HACS and locate the config flow.
- Supply a user-authorized identity and complete device login in Home Assistant.
- Run both example actions; confirm text succeeds and structured output validates.
- Restart Home Assistant and run another task without repeating device login.
- After access-token expiry, confirm refresh and another task succeed; confirm
  rotated tokens survive a subsequent restart.
- Cancel a pending login and check that polling stops. Test denial or expiry and
  permanent refresh failure, confirming a useful error or reauthentication prompt.

Do not share or commit real credentials to complete these checks.

## Implementation references and licensing

Protocol references inspected before implementation:

- [codex-device-auth](https://github.com/jorgemosquera/codex-device-auth):
  `config.py`, `device_code.py`, `credentials.py`, and `runtime.py`. Its package
  metadata declares MIT; the integration independently implements the relevant
  HTTP protocol using `aiohttp`, without its hardcoded identity, synchronous
  client, credential files, or LangChain/LangGraph dependency tree.
- [the-ai-counsel WebSocket implementation at f9f784f](https://github.com/jacob-bd/the-ai-counsel/blob/f9f784f12bfbda8ea313a9725cdb9a92eb7c18d2/backend/oauth/responses_websocket.py):
  inspected for event semantics and model/transport limitations; no WebSocket
  implementation is included or copied.
- [Home Assistant AI Task documentation](https://developers.home-assistant.io/docs/core/entity/ai-task/)
  and [Core 2026.10.0](https://github.com/home-assistant/core/tree/2026.10.0/homeassistant/components/ai_task)
  define the entity, result, and chat-log contracts used here.

The repository retains the blueprint's MIT [LICENSE](LICENSE). No reference
library source is vendored.
