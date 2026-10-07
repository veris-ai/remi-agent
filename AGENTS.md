# AGENTS.md

Remi is the debt-collection voice agent, the **candidate under test**, for the
**VAmoS Debt Collection** benchmark on Veris Bench. It works for Tallmere Account
Services, LLC, places **outbound** calls to consumers in New York City, and must
apply the benchmark's policy (`collections_bench/RULES.md` in veris-ai/utility-bench)
against the benchmark's world (`collections_bench/WORLD.md`: a Postgres collections
database plus a Stripe twin).

This file is the canonical guide. `CLAUDE.md` points here and repeats only the hard rules.

---

## 1. Architecture

```
        bench actor (simulated consumer, speaks first)
                     │  voice_ws: PCM16 24 kHz mono, binary frames
                     ▼
┌────────────────────────────── remi_core/server.py (one app, every vendor) ──────────────────────────────┐
│  GET /health   503 until DB answers + provider warmed up                                                │
│  WS  /voice    one call per pod → CallSession + CallRuntime → provider.run_call(runtime) → finalize()   │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────┘
          │ CallRuntime: setup (instructions, tool specs, TurnTaking, CallStartPolicy)
          │              start (CallStart)  inbound (held until primed)  outbound (gated, playout-tracked)
          ▼
┌──────────── VoiceProvider (remi_core/providers/base.py) — the only seam ────────────┐
│ vendors/openai_realtime  → RealtimeProtocolProvider (remi_core/providers/           │
│                            realtime_protocol.py; a future Grok = small subclass)    │
│ vendors/gemini_live      → raw Gemini Live protocol                                  │
│ vendors/pipecat          → Pipecat STT → OpenAI LLM → TTS pipeline                  │
└──────────────────────────────────────────────────────────────────────────────────────┘
          │ every tool call: session.dispatch(name, args) → dict (errors are {"error": ...})
          ▼
┌─────── CallSession (remi_core/session.py): the gate + the call's state ───────┐
│ ToolRegistry (tool_registry.py; tools/*.py)  policy.py (RULES.md numbers)     │
│ CallContext + CallClock (context.py)         prompt/ (instructions template)  │
└───────────────────────────────────────────────────────────────────────────────┘
          │                                   │
          ▼                                   ▼
   Database (db.py, psycopg 3)        PaymentGateway (payments.py: StripeGateway)
   DATABASE_URL (Postgres twin)       STRIPE_API_BASE / STRIPE_API_KEY (Stripe twin)
```

`remi_core/` never imports a vendor SDK. `vendors/<name>/` never contains a rule.

## 2. The abstractions, and why each exists

| Abstraction | File | Why |
|---|---|---|
| `CallContext` | `remi_core/context.py` | The dialer's hand-off (`VERIS_AGENT_CONTEXT`: `account_id`, `phone_id`, `call_time`) is the only task-specific fact Remi gets. Parsed and validated once at startup; a missing or bad context stops the process instead of calling the wrong account. |
| `CallClock` | `remi_core/context.py` | The World is frozen at 2027-02-10 but Postgres `now()` is 2026 wall time. `call_time` + monotonic elapsed is the only clock; every timestamp and date Remi writes comes from it. |
| `ToolRegistry` + `@REGISTRY.tool` | `remi_core/tool_registry.py`, `remi_core/tools/` | One declaration per tool: a pydantic args model (the JSON schema every vendor gets), the handler, the `Gate` it needs, the fields to `redact`, and whether a collection stop blocks it. No per-vendor tool copies. |
| `CallSession` | `remi_core/session.py` | Enforces RULES.md's opening order as a gate (connect reads → assessment → right party → disclosures → debt). Refusals come back as `{"error": ...}` the model can act on; nothing out of order reaches the world. Owns the contact-attempt row and writes it on hang-up if the model did not. |
| `policy` | `remi_core/policy.py` | RULES.md's numbers and calendar (floors, plan limits, 14-day promises, 3-in-7, 8 a.m.–9 p.m. ET, time-barred, first conversation, business days, disposition precedence) as pure functions. The world enforces none of them. Remi's own reading; it must never import the grader. |
| `prompt` | `remi_core/prompt/` | RULES.md distilled into agent instructions, rendered per call from the context. Byte-identical across vendors. |
| `Database` | `remi_core/db.py` | psycopg 3, connect + statement timeouts, writes only to whitelisted columns, values always parameterized. Injected. |
| `PaymentGateway` / `StripeGateway` | `remi_core/payments.py` | An instance with its own `StripeClient` (base URL, key, timeout), never the `stripe` module globals. Card/bank numbers go only to Stripe. Injected; tests use a fake. |
| `CallStartPolicy` / `CallStart` | `remi_core/call_start.py` | Outbound etiquette: the callee speaks first. See section 5. |
| `VoiceProvider` | `remi_core/providers/base.py` | The one vendor seam. A provider gets a `CallRuntime` and owns only the wire protocol, its `ToolFormat`, how it forces the priming reads, and how barge-in maps. |
| `CallerLine`, `InboundAudio`, `OutboundAudio` | `remi_core/providers/base.py` | Shared audio plumbing: caller audio held until priming finishes; agent audio dropped until the callee spoke; playout tracked so barge-in truncation and hang-up timing are computed once, not per vendor. |
| `RealtimeProtocolProvider` | `remi_core/providers/realtime_protocol.py` | The OpenAI Realtime GA protocol, implemented once. OpenAI subclasses it with an endpoint and a session; Grok's Voice Agent API (a Realtime dialect) would be a similar small subclass. |
| `server.create_app` | `remi_core/server.py` | One FastAPI app (`/health`, `/voice`) for every vendor, with startup credential and context checks. |

## 3. What this design fixes from rory-agent

| rory-agent problem | How Remi avoids it |
|---|---|
| Inbound-only prompt; the agent spoke first in every adapter | Outbound instructions; `CALLEE_FIRST` start policy shared by all providers; parity tests assert no greeting is triggered and no audio reaches the caller before the callee speaks. |
| ~1,500 duplicated lines: 15 `web.py`, tool-surface copies, 13 Dockerfiles, Grok ≈ copy of OpenAI | One `server.py`, one registry, one `Dockerfile` with `ARG VENDOR`, one Realtime-protocol base for OpenAI and Grok. |
| Vendor behaviour drift (barge-in truncation, VAD thresholds, date injection) | `TurnTaking` (800 ms silence, 300 ms prefix, 0.5 threshold) mapped into each vendor's VAD; `OutboundAudio.played_ms` drives truncation; the date is rendered into the shared instructions from `CallContext`. Parity tests assert the values each vendor actually sends. |
| Parity tests that only checked module attributes | `tests/test_parity.py` runs each provider through a scripted call and asserts on the frames it sends (session/setup, priming request, caller audio). |
| 73 unbounded `>=` deps, `uv:latest` | Every dependency bounded; `uv` image pinned (`0.11.26`), Python base pinned (`3.12.13-slim-bookworm`), `uv.lock` committed, `uv sync --locked` in the image. |
| Unauthenticated inbound webhooks | No webhooks at all. The only routes are `/health` and the bench's `/voice` channel. No `public_endpoint`. |
| Full tool args logged (card data) | Tools declare `redact` fields; `CallSession.dispatch` logs `[redacted]`. Card numbers never reach Postgres, logs or tool results (tests assert all three). |
| Process-global clock from env that never added elapsed time | `CallClock` per call: `call_time` + monotonic elapsed. An AST test bans wall-clock calls and SQL `now()` in tool code. |
| Module-global Stripe config | `StripeGateway` owns a `StripeClient`; a test asserts `stripe.api_base`/`api_key` are untouched. |

Kept from rory-agent on purpose: one shared tool schema and dispatch with a
verification gate; tools return `{"error": ...}` to the model instead of
crashing the call; thin vendor adapters that use each vendor's native tool
calling and turn detection.

## 4. Tools

| Tool | Gate | What it does |
|---|---|---|
| `get_account`, `get_dialed_phone`, `get_contact_history` | none | Connect-time reads (each its own tool so the trace shows each read). The last one returns `call_assessment`: proceed or should-not-call (with reasons), first conversation, time-barred, language question due. `get_account` returns no DOB, ZIP, SSN, street, balance or creditor. |
| `verify_right_party` | proceeding | Full name + two of DOB/ZIP/SSN-last4 compared server-side; 3 failures lock. `role=estate_rep` checks the estate record (name only). |
| `record_flag` | proceeding (consumer only, except `deceased`) | cease, channel_optout, attorney, bankruptcy, deceased, scra, hardship, original_creditor_request: the flag, its letter, status and consent changes in one transaction; stops collection where RULES.md says so. |
| `log_dispute` | verified | dispute or medical financial assistance: flag, substantiation letter, status `disputed`, collection stops. |
| `set_language` | verified | Records the language preference. |
| `get_debt_details` | verified | Requires attestations that the owed disclosures were given (recording, mini-Miranda, time-barred, alternative format, language recorded). Returns creditor, last four, balance, itemization, and the limits Remi may offer. |
| `add_payment_method` | debt unlocked, not stopped | Card or ACH to Stripe (customer created and written to the account on first use). Card fields redacted. |
| `take_payment` | debt unlocked, not stopped | Pay in full (exactly the balance) or a one-payment settlement at/above the floor; records the payment, balance, status; settlement adds its arrangement and letter. Declines are real. |
| `create_arrangement` | debt unlocked, not stopped | plan / hardship_plan (needs the hardship flag) / settlement_instalments (≤3, floor+10); equal monthly instalments; confirmation letter due in 5 business days. |
| `record_promise` | debt unlocked, not stopped | Exact amount and date ≤14 days out. |
| `schedule_callback` | verified | 8 a.m.–<9 p.m. ET; agency-initiated ones never inside the 7-day window. |
| `queue_letter` | verified | Rarely needed; idempotent per call. |
| `transfer_to_human` | proceeding | Marks the transfer (disposition `transferred`). |
| `log_contact_attempt` | assessed | The call's one row. Remi supplies who answered and a summary; time, duration and disposition (WORLD.md precedence) come from the session and the clock. |
| `end_call` | assessed | Requires the log; the line closes after the last words play. |

### Adding a tool

1. Add an args model and an `async def name(session, args) -> dict` handler in the
   fitting `remi_core/tools/*.py`, decorated with `@REGISTRY.tool(Args, requires=Gate.X,
   redact=(...), blocked_by_stop=...)`. The docstring is the model's description.
2. Raise `ToolError("what to do instead")` for refusals; never return partial writes:
   do all writes inside one `async with session.db.transaction()`.
3. Take time only from `session.clock`. Put any RULES.md number in `policy.py`.
4. Add a flow to `tests/test_tools.py` asserting the exact rows written. Parity tests
   pick the new tool up automatically.

## 5. Call start: `CALLEE_FIRST`

On a real outbound call the person who answers speaks first, and the bench's actor
does too (`wait_for_callee_first: false` in the candidate's channel). For every vendor:

1. **Prime.** When the vendor session opens, the provider makes the model run the
   three connect reads as real tool calls, before the callee is heard: OpenAI
   Realtime with `response.create` + `tool_choice: "required"` (then by name for any
   read it skipped); Pipecat with a forced chat completion through the same SDK;
   Gemini Live (no forced tool choice) with a text turn labelled `[Dialer note, not
   the person on the line]`. The caller's audio is **held, not dropped** meanwhile.
2. **Release.** When the reads are done, the held "Hello?" goes to the vendor and its
   own turn detection answers it, already knowing whether the call may proceed.
3. **Hold the line.** No agent audio reaches the caller before the callee's first
   utterance has been detected; anything earlier is dropped (`OutboundAudio`).

## 6. The clock rule

`CallClock.now()` = `call_time` + time elapsed since `/voice` connected. Every
`at`, `created_at`, `queued_at`, `consent_revoked_at` and every computed date
(instalments, promises, callbacks, the confirmation-letter deadline, 7-day windows,
time-barred) comes from it. Never `datetime.now()`, `date.today()`, SQL `now()`,
`CURRENT_DATE` or a column default. `contact_attempts.at` is `call_time`.

## 7. Traces per vendor (what the judge sees)

The judge reads an agent trace built by the bench's OTel bundle. Its first check is
that the trace shows the account, dialed-phone and contact-history reads before Remi
asked for anyone by name.

| Vendor | `trace_adapter` | How the trace is built | Status |
|---|---|---|---|
| `openai_realtime` | `openai-realtime` | The adapter hooks the `websockets` asyncio client's `send`/`recv` and reads GA Realtime events. Remi sets `audio.input.transcription` so the caller's words are in the trace. | Verified offline against the bench's adapter code over real websockets: system, the 3 reads, `Hello?`, greeting, log, end. Not yet run on the bench. |
| `gemini_live` | `gemini-live` | Same hook; camelCase setup/toolCall/toolResponse frames. **Gemini Developer API only** (function calls carry `id`s; Vertex omits them). | Verified offline the same way. The priming note appears as a `user` turn, labelled as a dialer note. |
| `pipecat` | none | The LLM runs in-process through the `openai` SDK, which the bundle instruments (OpenInference `OpenAIInstrumentor`): each chat completion is an LLM span with tool calls, and tool results arrive in the next request's messages. | Priming span verified offline with the OpenInference instrumentor. The streaming pipeline completions use the same SDK but were not run end to end. |

Keep vendor traffic on `websockets.asyncio.client` (see `remi_core/providers/wire.py`)
with JSON text frames; a different client library is invisible to the adapters.

## 8. Config contract (environment)

| Variable | Who sets it | Used by | Meaning |
|---|---|---|---|
| `VERIS_AGENT_CONTEXT` | bench (per attempt) | all | `{"account_id","call_time","phone_id"}`. Required. |
| `DATABASE_URL` | bench (world) | all | Postgres DSN of the collections twin. Required. |
| `STRIPE_API_KEY` | bench (world) | all | Stripe twin key. Required. |
| `STRIPE_API_BASE` | bench (world) | all | Stripe twin base URL; unset means api.stripe.com. |
| `SSL_CERT_FILE` | optional | all | CA bundle Stripe traffic trusts (the SDK otherwise pins its own). |
| `REMI_VENDOR` | image (`ARG VENDOR`) | all | Which `vendors/<name>` runs. |
| `PORT` | image | all | Default `8008`. |
| `REMI_LOG_LEVEL` | optional | all | Default `INFO`. |
| `OPENAI_API_KEY` | candidate-env | openai_realtime, pipecat | Required for those. |
| `REMI_OPENAI_REALTIME_MODEL` / `REMI_OPENAI_VOICE` / `REMI_OPENAI_TRANSCRIBE_MODEL` | optional | openai_realtime | Defaults `gpt-realtime` / `marin` / `gpt-4o-mini-transcribe`. |
| `GEMINI_API_KEY` | candidate-env | gemini_live | Gemini Developer API key. Required. |
| `REMI_GEMINI_MODEL` / `REMI_GEMINI_VOICE` | optional | gemini_live | Defaults `gemini-2.5-flash-native-audio-preview-12-2025` / `Puck`. |
| `REMI_PIPECAT_STT` | optional | pipecat | `deepgram` (default) or `openai`. |
| `REMI_PIPECAT_TTS` | optional | pipecat | `openai` (default) or `cartesia`. |
| `REMI_PIPECAT_LLM_MODEL` | optional | pipecat | Default `gpt-4.1`. |
| `DEEPGRAM_API_KEY` / `REMI_DEEPGRAM_MODEL` | candidate-env | pipecat + deepgram | Key required; model default `nova-3-general`. |
| `REMI_OPENAI_STT_MODEL` / `REMI_OPENAI_TTS_MODEL` | optional | pipecat | Defaults `gpt-4o-transcribe` / `gpt-4o-mini-tts` (voice from `REMI_OPENAI_VOICE`, default `marin`). |
| `CARTESIA_API_KEY` / `REMI_CARTESIA_VOICE_ID` | candidate-env | pipecat + cartesia | Both required when `REMI_PIPECAT_TTS=cartesia`. |

Missing required variables stop the process at startup with a message naming them.

## 9. Adding a vendor (checklist)

1. `vendors/<name>/__init__.py`: subclass `VoiceProvider` (or `RealtimeProtocolProvider`
   for a Realtime dialect), set `name = "<name>"`, `trace_adapter`, `required_env`,
   implement `from_env` and `run_call`, export `PROVIDER`.
2. A `ToolFormat` converting `setup.tools` to the vendor's declarations.
3. Map `setup.turn_taking` onto the vendor's VAD; send `setup.instructions` verbatim.
4. Prime per `CALLEE_FIRST`: force the connect reads, then `call.start.mark_primed()`
   and `await call.inbound.release()`; call `call.start.mark_callee_spoke()` on the
   vendor's first user-turn signal; send agent audio only via `call.outbound.send`.
5. Route every tool call to `call.session.dispatch` and send its dict back; when
   `session.ended`, `await call.hang_up()`.
6. Talk to the vendor with `remi_core.providers.wire.connect_websocket` so the bench's
   trace adapter can see it (or confirm an OpenInference instrumentor covers its SDK).
7. Add a `<name>` extra in `pyproject.toml` (bounded versions), `uv lock`.
8. Add the vendor to `tests/test_parity.py` (a scripted vendor socket or mock transport).
9. Add its row to sections 7, 8 and 11.

Nothing in `remi_core/` changes.

## 10. Testing

```bash
uv sync --all-extras          # every vendor's deps, for parity tests
uv run --all-extras pytest -q # unit, parity, smoke (--all-extras keeps every vendor installed)
uv run ruff check . && uv run ruff format --check .
```

- `tests/test_tools.py`: tools and the gate against a **real Postgres** with WORLD.md's
  schema (`tests/fixtures/world_schema.sql`), started with `docker run postgres:16-alpine`
  or taken from `REMI_TEST_DATABASE_URL`; skipped with the reason if neither exists.
  Stripe is `tests.support.FakePayments`.
- `tests/test_context_and_policy.py`: context, clock, RULES.md numbers.
- `tests/test_payments.py`: `StripeGateway` against an httpx mock of the Stripe API.
- `tests/test_parity.py`: what each installed vendor sends.
- `tests/test_smoke.py`: the real server over `/voice` with a fake provider and Postgres.

No test calls a paid API.

## 11. Build, push, register on the bench

```bash
docker build --platform linux/amd64 --build-arg VENDOR=openai_realtime -f Dockerfile \
  -t us-central1-docker.pkg.dev/veris-ai-dev/bench-private-images/bench-e2e/remi-openai_realtime:<version> .
docker push us-central1-docker.pkg.dev/veris-ai-dev/bench-private-images/bench-e2e/remi-openai_realtime:<version>
docker inspect --format='{{index .RepoDigests 0}}' <that tag>    # register THIS (@sha256) ref
```

Repeat with `VENDOR=gemini_live` and `VENDOR=pipecat` (`remi-<vendor>`). The image
carries no secrets; keys come from the bench's candidate-env
(`PUT /v1/benches/{id}/candidate-env/{KEY}`) referenced as `${env:KEY}`.

Bench candidate (one per vendor):

```json
{
  "name": "Remi (OpenAI Realtime)",
  "channel_config": {
    "init": null,
    "channel": {"type": "voice_ws", "url": null, "protocol": "binary", "language": "en-US",
                "wait_for_callee_first": false}
  },
  "image": {
    "ref": "us-central1-docker.pkg.dev/veris-ai-dev/bench-private-images/bench-e2e/remi-openai_realtime@sha256:<digest>",
    "port": 8008, "path": "/voice", "health_path": "/health",
    "trace_adapter": "openai-realtime",
    "env": {"OPENAI_API_KEY": "${env:OPENAI_API_KEY}"}
  }
}
```

| Vendor | `trace_adapter` | `image.env` |
|---|---|---|
| `openai_realtime` | `openai-realtime` | `OPENAI_API_KEY` |
| `gemini_live` | `gemini-live` | `GEMINI_API_KEY` |
| `pipecat` | omit | `OPENAI_API_KEY`, `DEEPGRAM_API_KEY` (plus `CARTESIA_API_KEY`, `REMI_PIPECAT_TTS`, `REMI_CARTESIA_VOICE_ID` for Cartesia) |

`DATABASE_URL`, `STRIPE_API_BASE`, `STRIPE_API_KEY` and `VERIS_AGENT_CONTEXT` are
injected by the bench; do not set them on the candidate.

## 12. Hard rules

- `remi_core` stays vendor-free; vendors hold no policy. Rules live in `policy.py` and the tool gates.
- Never import or copy from the benchmark's grader (utility-bench `collections_bench`).
- One clock: `CallClock`. No wall time, no SQL `now()`.
- Card number, expiry, CVC and bank account numbers: only to Stripe; never logged, stored, or returned.
- Every tool call goes through `CallSession.dispatch`; tools return `{"error": ...}`, never crash the call.
- Every dependency bounded; `uv.lock` committed; base images pinned. No secrets in code or images.
- No inbound endpoints beyond `/health` and `/voice`.
- Vendor traffic via `websockets.asyncio.client` (or an instrumented SDK) so the judge's trace exists.
