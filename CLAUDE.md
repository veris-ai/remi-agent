# CLAUDE.md

Read **AGENTS.md** first: it is the canonical guide (architecture, why each
abstraction exists, call start, clock, traces, config, testing, build/push).

Hard rules:

- `remi_core/` is vendor-free; `vendors/<name>/` holds no policy. Adding a vendor touches nothing in `remi_core/`.
- Never import or copy from the benchmark's grader (utility-bench `collections_bench`). Rules live in `remi_core/policy.py` and the tool gates.
- One clock: `CallClock` (call_time + elapsed). No `datetime.now()`, `date.today()`, SQL `now()` or column defaults.
- The callee speaks first (`CALLEE_FIRST`): prime the connect reads, hold caller audio until then, no agent audio before the callee's first utterance.
- Every tool call goes through `CallSession.dispatch`; refusals are `{"error": ...}` for the model, never exceptions.
- Card numbers, expiry, CVC and bank account numbers go only to Stripe: never logged (`redact`), stored or returned.
- Every dependency bounded (`>=x,<y`); `uv.lock` committed; base images pinned. No secrets in code or images.
- No inbound endpoints beyond `/health` and `/voice`.
- Vendor sockets via `remi_core.providers.wire.connect_websocket` (the client the bench's trace adapters hook).
- Before committing: `uv run --all-extras pytest -q` and `uv run ruff check . && uv run ruff format --check .`.
