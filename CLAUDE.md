# CLAUDE.md

Read **AGENTS.md** first: it is the canonical guide (why one repo, framework vs
pack, the `AgentSpec` contract, call start, clock, traces, config, how to add an
agent / vendor / tool, build and push). Agent specifics: `vamos_agents/<name>/README.md`.

Hard rules:

- `vamos_voice/` never imports `agents.*` (agents are found by name at runtime); providers see only the `CallRuntime`. Domain rules live in packs. `tests/test_framework.py` enforces the import ban.
- One clock: `CallClock` (`call_time` + monotonic elapsed). No `datetime.now()`, `date.today()`, `time.time()`, SQL `now()` or column defaults.
- Card numbers, expiry, CVC and bank account numbers go only to the payment processor: declared `redact`, never logged, stored or returned.
- Every tool call goes through `CallSession.dispatch`; refusals are `ToolError` → `{"error": ...}`, never exceptions out of a call.
- Every dependency bounded (`>=x,<y`); `uv.lock` committed; base images pinned. No secrets in code or images.
- One `Dockerfile` (`--build-arg AGENT=… --build-arg VENDOR=…`); one image per agent x vendor; no inbound endpoints beyond `/health`, `/voice` and a provider's own per-call-authenticated `/vendor/<name>` routes.
- Vendor sockets via `vamos_voice.providers.wire.connect_websocket` (the client the bench's trace adapters hook).
- Never import or copy a benchmark's grader into a pack.
- Before committing: `uv run --all-extras --no-extra livekit pytest -q`, `uv run --all-extras --no-extra pipecat --no-extra huggingface --no-extra grok-cascade pytest -q` (LiveKit pins `openai<3`, those extras `openai>=3`) and `uv run ruff check . && uv run ruff format --check .`.
