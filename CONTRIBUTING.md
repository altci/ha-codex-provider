# Contributing

Use Python 3.14 and Home Assistant Core 2026.10.0. Install development dependencies
with `scripts/setup`. Run `python -m pytest -q`, `python -m ruff check .`, and
`python -m ruff format . --check` before submitting a change.

Keep Codex Provider a small, async Home Assistant adapter. Read the protocol
references linked in the README before changing auth or inference. Never introduce
functional identity defaults, log tokens, or add live credentials to tests. Provider
responses should be mocked in automated tests. Report the model/transport used and
manual acceptance checks separately from mocked results.
