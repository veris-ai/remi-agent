# AGENTS.md

**vamos-agents** is the one repository for every VAmoS voice agent: the
**candidates under test** on Veris Bench's VAmoS benchmarks. Each benchmark
(debt collection today; insurance claims, mortgage servicing and others to
come) gets its own agent, and every agent runs on several voice vendors.

| Agent | Benchmark | Direction | Pack |
|---|---|---|---|
| **Remi** | VAmoS Debt Collection (`collections_bench` in veris-ai/utility-bench) | outbound, the callee speaks first | `vamos_agents/remi/` ([README](vamos_agents/remi/README.md)) |
| Rory (energy), Riley (banking) | VAmoS Energy / Banking | inbound, the agent speaks first | live in their own repos today; see section 14 |

This file is the canonical guide. `CLAUDE.md` points here and repeats only the hard rules.

> **Principle: vamos-agents are benchmark reference candidates.** Use each
> vendor's native turn-taking, interruption, VAD and noise handling; do not add
> custom audio processing, VAD gating or behavioral workarounds that mask vendor
> differences. Benchmark conditions (noise, accents, interruptions) exist to
> measure the vendor. Configure each vendor sensibly per its own docs; fix only
> our own plumbing (e.g. requests that race each other), never the vendor's
> behavior.

---

## 1. Why one repository

A repo per agent does not scale: every agent needs the same server, the same
`voice_ws` audio plumbing, the same call-start etiquette, the same tool
dispatch and the same three-plus vendor integrations, and rory-agent showed
what copying them does (section 13). Of Remi's first ~4,100 lines, ~1,900 were
generic. They are now the **`vamos_voice` framework**; an agent is a **pack**
that plugs in through one small contract, `AgentSpec`.

## 2. Layout

```
vamos_voice/                      # the framework: never imports an agent
  __main__.py  server.py          # python -m vamos_voice: /health + /voice, startup config checks
  agent.py                        # AgentSpec (the contract) + ConfigError
  registry.py                     # VAMOS_AGENT=<name> -> vamos_agents.<name>.AGENT
  context.py                      # AgentContext (VERIS_AGENT_CONTEXT as a str map) + CallClock
  call_start.py                   # CallStartPolicy: callee_first (outbound) / agent_first (inbound)
  audio.py                        # PCM16 helpers, streaming resampler
  tools/registry.py               # ToolRegistry: pydantic args -> one JSON schema, requires/redact/tags
  session/engine.py               # CallSession: the one dispatch path (gates, redaction, never raises)
  session/gates.py                # Gates protocol + GateLadder (ordered state machine)
  providers/                      # the vendor seam and the vendors
    base.py realtime_protocol.py wire.py
    openai_realtime/  grok_voice/     # Realtime protocol: OpenAI natively, Grok as a dialect
    gemini_live/  gpt_live/  pipecat/
vamos_agents/
  remi/                           # Remi's pack: AgentSpec impl, prompt, tools, policy, gates,
    agent.py ... tests/           # world clients (db; payments via the World's gateway), and its own tests
tests/                            # framework tests, the agents x vendors parity matrix,
  fake_inbound.py                 # a test-only inbound agent (not a pack, never shipped)
Dockerfile                        # ONE recipe: ARG AGENT x ARG VENDOR
pyproject.toml  uv.lock           # one package; one extra per agent and per vendor
```

## 3. Architecture

```
          bench actor (simulated human)          voice_ws: PCM16 24 kHz mono, binary or json framing
                         │
                         ▼
┌──────────────── vamos_voice/server.py (one app, every agent x vendor) ────────────────┐
│ GET /health  503 until agent.connect() and provider.warm_up() finish                  │
│ WS  /voice   one call per pod: clock, CallSession, CallRuntime → provider.run_call()  │
│              → agent state's finalize()                                               │
└───────────────────────────────────────────────────────────────────────────────────────┘
      │ CallRuntime: setup (instructions, tool specs, TurnTaking, CallStartPolicy)
      │              start (CallStart)  inbound (held while priming)  outbound (gated)
      ▼
┌──────── VoiceProvider (providers/base.py): the vendor seam ────────┐
│ openai_realtime → RealtimeProtocolProvider (GA protocol, once)     │
│ grok_voice      → RealtimeProtocolProvider (xAI dialect)           │
│ gemini_live     → raw Gemini Live protocol                         │
│ gpt_live        → GPT-Live live sessions + Responses delegation    │
│ pipecat         → Pipecat STT → OpenAI LLM → TTS                   │
└─────────────────────────────────────────────────────────────────────┘
      │ every tool call: session.dispatch(name, args) → dict ({"error": ...} on refusal)
      ▼
┌──────── CallSession (session/engine.py) ────────┐      ┌──────── AgentSpec (vamos_agents/<name>/) ────────┐
│ registry lookup → gates.refusal(state, tool)    │◀─────│ tools (ToolRegistry)   gates (Gates)        │
│ → args validation → handler(state, args)        │      │ call_start   instructions(clock)            │
│ redact in logs; track connect reads             │      │ open_call(clock) → per-call state           │
└─────────────────────────────────────────────────┘      │ from_env / connect / close: world clients   │
                                                         └─────────────────────────────────────────────┘
```

### Framing: how audio and the hang-up cross `/voice`

`vamos_voice/framing.py`; `VAMOS_VOICE_FRAMING` picks one per image, and it
must match the bench candidate's channel `protocol` (the actor's side is
veris-sandbox `actor-service/app/actor/channels/voice_ws.py`).

| | `binary` (default) | `json` (bench candidates) |
|---|---|---|
| Audio, both ways | one raw PCM16 binary message per chunk | text `{"type": "audio", "audio": "<base64 PCM16 24 kHz>"}` |
| Agent hangs up | close (1000) | `{"type": "end"}`, then close (1000) |
| Caller hangs up | closes the socket | `{"type": "end"}` (then closes); the call ends and finalizes; no `end` is echoed |
| Anything else | text frames ignored (warning) | non-JSON or unknown `type` ignored |

**Why json on the bench.** With `binary`, the actor has no way to tell a
deliberate hang-up from a dropped socket, so *any* close by the callee is
`voice_ws_disconnect`, a failure end reason: an attempt where Remi correctly
ended the call (`end_call` on a "should not have been called" account, after a
cease) was marked failed and never graded. With `json`, the `end` the agent
sends first makes the actor record `client_end`, a normal end.

**One hang-up path.** `CallerLine.close()` is the hang-up for every socket
vendor: the agent's hang-up tool (`CallRuntime.hang_up`, after the last words
play), a provider failure (priming included) and the server's end of call all
go through it, so a deliberate end in `json` always sends `end` before the
close. Pipecat owns its transport, so its `LineSerializer` applies the same
`Framing`: the pipeline's `EndFrame`/`CancelFrame` is written as `end` before
Pipecat closes with 1000, and a caller's `end` cancels the pipeline.

## 4. The boundary: framework vs pack, and why

| Lives in `vamos_voice/` (framework) | Lives in `vamos_agents/<name>/` (pack) |
|---|---|
| server, `/health`, `/voice`, one call per pod | the agent's name, persona, prompt text |
| `CallerLine`, held inbound audio, gated outbound audio, barge-in playout | the per-call state and its `finalize()` (e.g. Remi's contact-attempt row) |
| `CallStartPolicy` mechanics in every provider | which policy, which connect reads, what the opening says |
| `AgentContext` (string map), `CallClock` | the typed view of the context (Remi: account, phone, call time in Eastern) |
| `ToolRegistry`, JSON-schema reduction, `CallSession.dispatch`, redaction | the tools, their gates (levels and messages), domain rules (`policy.py`) |
| `GateLadder` engine | the ladder's levels and explanations |
| vendor protocols, tool-format conversion, VAD mapping | world clients (Postgres, a payment gateway, Fineract...) built from the agent's env |

Why the line is there:

- **Vendor work is the expensive, drift-prone part.** Barge-in truncation,
  priming, held audio, VAD mapping and trace-friendly sockets are written
  once and tested once (`tests/test_parity.py`), for every agent.
- **Rules belong to an agent.** A benchmark grades its own policy; nothing
  domain-specific may leak into the framework, or the next agent inherits the
  wrong rules. The framework never imports `agents.*`
  (`tests/test_framework.py` enforces it with an AST check), and providers see
  only a `CallRuntime`.
- **An image carries one agent.** Packs install as extras and only the chosen
  pack's folder is copied, so a Remi image has no Rory code or dependencies.

## 5. The `AgentSpec` contract

`vamos_voice/agent.py`. A pack subclasses it and exports the class as `AGENT`
from `vamos_agents/<name>/__init__.py`.

```python
class AgentSpec(abc.ABC):
    name: ClassVar[str]  # folder under vamos_agents/, AGENT build arg, extra, image prefix
    call_start: ClassVar[CallStartPolicy]  # callee_first(...) or agent_first(...)
    tools: ClassVar[ToolRegistry]
    gates: ClassVar[Gates]  # usually a GateLadder[State]
    required_env: ClassVar[tuple[str, ...]]  # checked before from_env; missing -> exit 2 naming them

    # parse the typed context + config, build world-client factories; no I/O; raise ValueError
    @classmethod
    def from_env(cls, env: Mapping[str, str], context: AgentContext) -> Self: ...

    def call_time(self) -> datetime: ...  # the World's instant at connect (tz-aware): CallClock origin
    async def connect(self) -> None: ...  # open world clients (retry while twins start); /health waits
    async def close(self) -> None: ...
    def instructions(self, clock: CallClock) -> str: ...  # from context + clock only: identical per vendor
    def open_call(self, clock: CallClock) -> CallState: ...  # per-call state; handlers receive it
    def describe(self) -> str: ...  # one log line, no secrets
```

The per-call state satisfies `CallState`: an `ended: bool` (set by the agent's
hang-up tool; providers then close the line) and `async def finalize()` (runs
whenever the socket closes, for whatever the call must always leave behind).

Supporting pieces an agent uses:

- `ToolRegistry.tool(Args, requires=Level.X | None, redact=(...), tags=(...))`
  on `async def handler(state, args) -> dict`. The docstring is the model's
  description; the pydantic model is the schema every vendor gets.
- `ToolError("what to do instead")`: a refusal the model can act on.
- `GateLadder[State](level=..., explain=...)`: ordered levels; a tool runs only
  once `level(state) >= tool.requires`. Compose extra checks in your own
  `Gates` class (Remi adds the collection stop on `tags`).
- `AgentContext.require(*keys)` and `parse_instant(raw, what)` to build the typed view.

Discovery: `VAMOS_AGENT=<name>` → `vamos_voice.registry.load_agent` imports
`vamos_agents.<name>` and validates `AGENT`. No framework edit adds an agent.

## 6. Call start: inbound vs outbound

`vamos_voice/call_start.py`. Every provider honours the policy the same way.

| | `callee_first(connect_reads, prime_instruction)` (outbound: Remi) | `agent_first(opening_instruction, connect_reads=(), prime_instruction="")` (inbound: Rory, Riley) |
|---|---|---|
| Before anyone speaks | the connect reads run as **real tool calls** (so the judge's trace shows them first); caller audio is **held, not dropped** | optional connect reads, same mechanism |
| First words | the callee's: no agent audio leaves until their first utterance is detected; anything earlier is dropped | the agent's: the provider requests an opening turn carrying `opening_instruction`; audio flows at once |
| OpenAI Realtime | a text-only `response.create` (`output_modalities: ["text"]`, per response; the session stays audio) + `tool_choice: "required"` + `instructions = session instructions + prime_instruction`, then each skipped read by name; then release held audio | then `response.create` with `instructions = session instructions + opening_instruction` |
| Gemini Live (no forced tool choice) | a user-role text turn labelled `[Call system note, not the person on the line]` | then the opening as the same kind of labelled note |
| Grok Voice (no forced tool choice) | the Gemini fallback in Realtime frames: a labelled user-role `conversation.item.create` + `response.create`; a skipped read is asked for again by name | `response.create` with per-response `instructions`, as OpenAI |
| GPT-Live (tools live on a delegated Responses backend) | `session.start` with the backend's `tool_choice: "required"`; `session.instructions.append` (`prime_instruction`) makes the live model delegate; each missing read forced by name via `session.update`; then `tool_choice: "auto"` | `session.instructions.append` with `opening_instruction`, after `session.started` |
| Pipecat | a forced chat completion through the instrumented `openai` SDK; reads land in the pipeline's context | then, when the caller's socket connects, `LLMMessagesAppendFrame(opening, run_llm=True)` |

**Priming deadline.** Priming never holds a caller forever: a model can skip
the reads, or a vendor can stall. If the connect reads are not all done within
`VAMOS_PRIMING_TIMEOUT_S` (default 6 s, `call_start.PRIMING_TIMEOUT_S`), every
priming provider stops priming and releases the held audio, logging a warning
that names the missing reads. `CallStart.priming_deadline` runs beside the
call loops for the socket vendors; Pipecat bounds its priming completions the
same way. GPT-Live also sets the backend's `tool_choice` back to `"auto"`.

The framework does **not** run the reads itself on the model's behalf. The
judge's trace must show only what the model did, and a read the model never
asked for would put words in its mouth. The agent's gates take over instead:
Remi refuses every tool beyond the reads with "Run the connect-time reads
first: <the missing ones>", so the model runs them on its next turn, before it
can discuss or take money. A late priming reply after the deadline is handled
as an ordinary turn: on the Realtime protocol a priming response still in
flight at the deadline is left to the vendor (its reads count if they arrive).

### After the call start: turns (Realtime protocol)

Turn taking is the vendor's own: server VAD with `create_response` and
`interrupt_response` on (OpenAI), Grok's VAD as documented. The provider never
answers a caller turn itself and never cancels a response; on caller speech it
only truncates the context to what was played (where the vendor supports it).
Its own requests (tool results, the opening turn) are serialized: one asked for
while a response is in flight waits for its `response.done` (requests
coalesce), and one refused with `conversation_already_has_active_response` is
sent again when the running response ends.

## 7. The clock rule

A bench World is frozen at a reference instant that is not wall time (the
debt-collection World is 2027-02-10), and the twins' `now()` return real wall
time. `CallClock.now()` = `agent.call_time()` + monotonic time since `/voice`
connected. Every timestamp an agent writes and every date it computes comes
from it. Never `datetime.now()`, `date.today()`, `time.time()`, SQL `now()`,
`CURRENT_DATE` or a column default: `tests/guards.assert_no_wall_clock` runs
over every framework module and every Remi module.

## 8. Adding a tool (to any agent)

1. An args model and `async def name(state, args) -> dict` in the pack's
   `tools/`, decorated with the pack's registry: `requires=` its gate level
   (or nothing for ungated), `redact=` every sensitive argument, `tags=` what
   its gates act on. The docstring is the model's description.
2. Refuse with `ToolError("what to do instead")`. Do all writes of one tool in
   one transaction; never leave partial writes.
3. Take time only from `state.clock`. Put any benchmark number in the pack's `policy.py`.
4. Test it against the pack's world (Remi: `vamos_agents/remi/tests/test_tools.py`,
   real Postgres). Parity tests pick the schema up automatically.

## 9. Adding a NEW agent (checklist)

1. `vamos_agents/<name>/` with `__init__.py` exporting `AGENT = <Name>Agent`.
2. `<Name>Agent(AgentSpec)`: `name = "<name>"`, `call_start`, `tools`, `gates`,
   `required_env`, `from_env`, `call_time`, `connect`/`close`, `instructions`, `open_call`.
3. A per-call state with `ended` and `finalize()`; a hang-up tool that sets `ended`.
4. World clients built in `from_env` from the agent's own env vars (no module
   globals, every network call with a timeout), opened in `connect()`.
5. A `<name>` extra in `pyproject.toml` with the pack's dependencies, every one
   bounded (`>=x,<y`); `uv lock`.
6. Tests under `vamos_agents/<name>/tests/` (rules, tools against the world, a
   `/voice` smoke test with a fake provider, the wall-clock guard).
7. Add an `AgentCase` to `tests/fakes.AGENT_CASES` (the parity matrix fails until you do).
8. `vamos_agents/<name>/README.md`; a row in this file's agent table and config table (section 11).
9. Build `--build-arg AGENT=<name> --build-arg VENDOR=<v>` for each vendor.

Nothing in `vamos_voice/` changes. If it seems to need to, the change is a
framework feature: make it generic, test it with the fake inbound agent and
Remi, and document it here.

## 10. Adding a vendor (checklist)

1. `vamos_voice/providers/<name>/__init__.py`: subclass `VoiceProvider` (or
   `RealtimeProtocolProvider` for a Realtime dialect such as Grok), set
   `name = "<name>"`, `trace_adapter`, `required_env`; implement `from_env`
   and `run_call`; export `PROVIDER`.
2. A `ToolFormat` converting `setup.tools` to the vendor's declarations.
3. Map `setup.turn_taking` onto the vendor's VAD; send `setup.instructions` verbatim.
4. Call start: if `call.session.missing_reads`, force them (section 6); then
   `call.start.mark_primed()`, `await call.inbound.release()`, and if
   `setup.start_policy.agent_opens` request the opening turn. Call
   `call.start.mark_callee_spoke()` on the vendor's first user-turn signal;
   send agent audio only via `call.outbound.send`.
5. Route every tool call to `call.session.dispatch` and send its dict back;
   when `call.session.ended`, `await call.hang_up()`.
6. Use `vamos_voice.providers.wire.connect_websocket` (the client the bench's
   trace adapters hook) or an SDK an OpenInference instrumentor covers.
7. A `<name>` extra in `pyproject.toml` (bounded), `uv lock`.
8. Add it to `tests/test_parity.py` (a scripted socket or a mock transport):
   it then runs against every agent, callee-first and agent-first.
9. Rows in sections 11 and 12.

### Vendor capability differences, and how the framework handles them

Vendors do not all support what OpenAI Realtime does. The rule: implement what
the vendor supports, and where it does not, declare the difference in the
provider (a class attribute or an overridden hook, never an `if vendor ==`
branch in shared code), document it in the provider's docstring and here, and
pin it in a test. Never send a frame the vendor documents as unsupported: an
`error` event ends the call.

| Capability | openai_realtime | grok_voice | gemini_live | gpt_live | pipecat |
|---|---|---|---|---|---|
| Forced tool choice for priming | `response.create` `tool_choice` | **no** (undocumented): labelled note fallback | **no**: labelled note | on the backend only: `session.update` `delegation.responses.tool_choice` | `tool_choice` on the SDK call |
| Barge-in truncation (context cut to what was heard) | `conversation.item.truncate` at `played_ms` | **no**: xAI lists truncate/cancel as unsupported. Grok stops generating itself on caller speech; its context may keep a few seconds the caller never heard. `truncates_on_barge_in = False` | vendor-side (`START_OF_ACTIVITY_INTERRUPTS`) | **no** such event: full duplex, the live model stops itself | Pipecat's interruption |
| Caller transcription (in the judge's trace) | `audio.input.transcription` (`gpt-4o-mini-transcribe`) | only with `transcription.model = "grok-transcribe"` (always sent) | `inputAudioTranscription` | always on (`session.input_transcript.delta`) | STT |
| `TurnTaking` mapping | silence, prefix padding, threshold | silence only (`threshold`/`prefix_padding_ms` not documented; xAI defaults) | silence, prefix padding | **none**: no VAD settings exist | silence via VAD + turn timeout |
| Turn management (who gets a reply, what may interrupt) | vendor: server VAD, `create_response`/`interrupt_response` on | vendor: server VAD | vendor | vendor | Pipecat |
| Callee-spoke signal | `input_audio_buffer.committed` | same | first input transcription | first non-empty `session.input_transcript.delta` | first user transcript |

`RealtimeProtocolProvider` exposes the hooks dialects differ on:
`priming_frames(setup, missing, first)` (default: text-only forced `tool_choice`),
and `truncates_on_barge_in` (default `True`). `CallStartPolicy.reprime_instruction`
/ `reprime_note` are the shared "still missing" wording.

## 11. Config contract (environment)

Framework (every image):

| Variable | Set by | Meaning |
|---|---|---|
| `VAMOS_AGENT` | image (`ARG AGENT`) | Which `vamos_agents/<name>` runs. |
| `VAMOS_VENDOR` | image (`ARG VENDOR`) | Which `vamos_voice/providers/<name>` runs. |
| `VERIS_AGENT_CONTEXT` | bench, per attempt | JSON object of strings; parsed into `AgentContext`. Absent = empty (inbound agents may not get one). |
| `PORT` | image | Default `8008`. |
| `VAMOS_LOG_LEVEL` | optional | Default `INFO`. |
| `VAMOS_VOICE_FRAMING` | candidate `image.env` | `/voice` framing, `binary` (default) or `json` (section 3, "Framing"); must match the channel's `protocol`. Bench candidates use `json`. Anything else stops startup. |
| `VAMOS_PRIMING_TIMEOUT_S` | optional | Seconds the connect reads may hold the caller before priming is abandoned (section 6). Default `6`; must be a positive number. |

Vendors:

| Variable | Vendor | Meaning |
|---|---|---|
| `OPENAI_API_KEY` | openai_realtime, pipecat | Required. |
| `VAMOS_OPENAI_REALTIME_MODEL` / `VAMOS_OPENAI_VOICE` / `VAMOS_OPENAI_TRANSCRIBE_MODEL` | openai_realtime | Defaults `gpt-realtime` / `marin` / `gpt-4o-mini-transcribe`. |
| `XAI_API_KEY` | grok_voice | xAI API key. Required. |
| `VAMOS_GROK_MODEL` / `VAMOS_GROK_VOICE` | grok_voice | Defaults `grok-voice-think-fast-2.0` / `eve`. Transcription is fixed to `grok-transcribe` (the only model xAI documents). |
| `OPENAI_API_KEY` | gpt_live | Required; the OpenAI project must have GPT-Live access (otherwise `session.start` fails with "Voice session access denied"). |
| `VAMOS_GPT_LIVE_MODEL` / `VAMOS_GPT_LIVE_BACKEND_MODEL` / `VAMOS_GPT_LIVE_VOICE` | gpt_live | Defaults `gpt-live-1` / `gpt-5.6-terra` (the Responses model tool use is delegated to, as in the VAmoS Energy GPT-Live candidate) / `marin`. |
| `GEMINI_API_KEY` | gemini_live | Gemini **Developer API** key. Required. |
| `VAMOS_GEMINI_MODEL` / `VAMOS_GEMINI_VOICE` | gemini_live | Defaults `gemini-2.5-flash-native-audio-preview-12-2025` / `Puck`. |
| `VAMOS_PIPECAT_STT` / `VAMOS_PIPECAT_TTS` | pipecat | `deepgram` (default) or `openai` / `openai` (default) or `cartesia`. |
| `VAMOS_PIPECAT_LLM_MODEL` | pipecat | Default `gpt-4.1`. |
| `DEEPGRAM_API_KEY` / `VAMOS_DEEPGRAM_MODEL` | pipecat + deepgram | Key required; model default `nova-3-general`. |
| `VAMOS_OPENAI_STT_MODEL` / `VAMOS_OPENAI_TTS_MODEL` | pipecat | Defaults `gpt-4o-transcribe` / `gpt-4o-mini-tts` (voice `VAMOS_OPENAI_VOICE`). |
| `CARTESIA_API_KEY` / `VAMOS_CARTESIA_VOICE_ID` | pipecat + cartesia | Both required with `VAMOS_PIPECAT_TTS=cartesia`. |

Agents:

| Variable | Agent | Meaning |
|---|---|---|
| `VERIS_AGENT_CONTEXT` keys `account_id`, `phone_id`, `call_time` | remi | Required; `TAS-NNNNNN`, a `phones` row id, ISO-8601 with offset. |
| `DATABASE_URL` | remi | Postgres DSN of the collections twin (bench-injected). Required. Payments go through the payment gateway inside that database (`gateway_tokenize_card`, `gateway_tokenize_ach`, `gateway_charge`), so Remi has no other world variable. |

Any missing required variable stops the process at startup (exit 2) with a message naming it.

**Key names.** Vendor keys are bench candidate-env secrets under their
standard names, `OPENAI_API_KEY`, `GEMINI_API_KEY` and `XAI_API_KEY`, as on
VAmoS Energy, so one bench-wide secret serves every candidate on that vendor.
Veris Bench allows these exact names (veris-sandbox
`benchmark/api/app/services/candidate_env.py`), while every other `GEMINI_*`,
`XAI_*`, `GROK_*` and `VERIS_*` name stays reserved: those namespaces belong to
the platform and to the coding-agent harnesses it runs (their CLIs keep their
own workspace key), and a bench-wide value there would re-route them. That is
why every other vendor setting here is `VAMOS_*`; a new variable must not use a
reserved prefix (`tests/test_parity.py` pins the key names).

## 12. Traces per vendor (what the judge sees)

The judge reads an agent trace built by the bench's OTel bundle; for Remi its
first check is that the account, dialed-phone and contact-history reads come
before anyone is asked for by name. Priming exists to make that true.

| Vendor | `trace_adapter` | How the trace is built | Requirement |
|---|---|---|---|
| `openai_realtime` | `openai-realtime` | The adapter hooks the `websockets` asyncio client's `send`/`recv` and reads GA Realtime events. | Keep traffic on `wire.connect_websocket`, JSON text frames; keep `audio.input.transcription` on so the caller's words are traced. |
| `grok_voice` | `openai-realtime` | The Realtime adapter, which already reads Grok's dialect (top-level `turn_detection`, no `output_item.added`, no `response.done` for a response cancelled by speech). | `grok-transcribe` on, or the caller is missing from the trace. Priming notes are user-role *items*, not transcriptions, so the adapter does not record them as the caller. |
| `gpt_live` | `gpt-live` | Same hook: system and model from `session.start`, tools from `session.delegation.responses.tools`, tool calls from nested `response.output_item.done`, results from `response.item.create`, both speakers from timed transcript deltas. | Tools only on the delegation backend; results only via `response.item.create`. Priming instructions are `session.instructions.append` and never appear as the caller. |
| `gemini_live` | `gemini-live` | Same hook; camelCase setup/toolCall/toolResponse frames. | **Developer API only** (function calls carry `id`s; Vertex omits them). Priming/opening notes appear as labelled `user` turns. |
| `pipecat` | none (omit) | The LLM runs in-process through the `openai` SDK, which the bundle instruments (OpenInference `OpenAIInstrumentor`): each completion is an LLM span with tool calls; results arrive in the next request. | Keep LLM calls on the `openai` SDK, including priming. |

## 13. Build, push, register on the bench

Images: `us-central1-docker.pkg.dev/veris-ai-dev/bench-private-images/bench-e2e/<agent>-<vendor>`,
with the vendor's underscores as dashes (the registry's convention, e.g.
`riley-gemini-live`): `remi-openai-realtime`, `remi-gemini-live`, `remi-grok-voice`,
`remi-gpt-live`, `remi-pipecat`.

```bash
REPO=us-central1-docker.pkg.dev/veris-ai-dev/bench-private-images/bench-e2e
docker build --platform linux/amd64 --build-arg AGENT=remi --build-arg VENDOR=openai_realtime \
  -t $REPO/remi-openai-realtime:<version> .
docker push $REPO/remi-openai-realtime:<version>
docker inspect --format='{{index .RepoDigests 0}}' $REPO/remi-openai-realtime:<version>  # register THIS ref
```

The image carries no secrets and no other agent. Vendor keys live in the
bench's candidate-env (`PUT /v1/benches/{id}/candidate-env/{KEY}`, `is_secret:
true`), and the engine merges every candidate-env key into every image
candidate's environment. **Never reference them from `image.env`:** for image
candidates the engine resolves `${env:KEY}` only inside the channel and init
config, not in `image.env`, and `image.env` wins over candidate-env, so
`"OPENAI_API_KEY": "${env:OPENAI_API_KEY}"` would hand the agent the literal
string instead of the key (veris-sandbox
`benchmark/api/app/services/engine_runner.py`, `_image_candidate_spec`).
`image.env` holds only per-candidate, non-secret config (the framing, model and voice names).

The bench injects `DATABASE_URL` (the collections twin's DSN) and
`VERIS_AGENT_CONTEXT`. That is Remi's whole world: the debt-collection World has
no Stripe twin (Stripe does not serve debt collection agencies), and payments go
through the payment gateway inside the collections database (utility-bench
`collections_bench/WORLD.md`, "The payment gateway"). No `STRIPE_*` value is
needed on the bench; one left over in candidate-env is ignored.

Bench candidate (one per agent x vendor; Remi on OpenAI Realtime shown):

```json
{
  "name": "Remi (OpenAI Realtime)",
  "channel_config": {
    "init": null,
    "channel": {"type": "voice_ws", "url": null, "protocol": "json", "language": "en-US",
                "wait_for_callee_first": false}
  },
  "image": {
    "ref": "us-central1-docker.pkg.dev/veris-ai-dev/bench-private-images/bench-e2e/remi-openai-realtime@sha256:<digest>",
    "port": 8008, "path": "/voice", "health_path": "/health",
    "trace_adapter": "openai-realtime",
    "env": {"VAMOS_VOICE_FRAMING": "json", "VAMOS_OPENAI_REALTIME_MODEL": "gpt-realtime-2.1"}
  }
}
```

| Vendor | `trace_adapter` | Bench candidate-env (secret) | `image.env` (non-secret) |
|---|---|---|---|
| `openai_realtime` | `openai-realtime` | `OPENAI_API_KEY` | `VAMOS_OPENAI_REALTIME_MODEL` (e.g. `gpt-realtime-2.1`, as Riley/Rory) |
| `gemini_live` | `gemini-live` | `GEMINI_API_KEY` | `VAMOS_GEMINI_MODEL` (e.g. `gemini-3.8-live`, as Riley/Rory) |
| `grok_voice` | `openai-realtime` | `XAI_API_KEY` | `VAMOS_GROK_MODEL` (e.g. `grok-voice-think-fast-2.0`, as Riley "Grok Voice"), optional `VAMOS_GROK_VOICE` |
| `gpt_live` | `gpt-live` | `OPENAI_API_KEY` (a project with GPT-Live access) | `VAMOS_GPT_LIVE_MODEL` (`gpt-live-1`), `VAMOS_GPT_LIVE_BACKEND_MODEL` (e.g. `gpt-5.6-terra`, as Rory "OpenAI GPT-Live 1"), optional `VAMOS_GPT_LIVE_VOICE` |
| `pipecat` | omit | `OPENAI_API_KEY`, `DEEPGRAM_API_KEY` (plus `CARTESIA_API_KEY` for Cartesia) | `VAMOS_PIPECAT_TTS`, `VAMOS_CARTESIA_VOICE_ID` when using Cartesia |

Every candidate (every row above) sets `"protocol": "json"` in its channel and
`VAMOS_VOICE_FRAMING=json` in its `image.env`, together (both, or neither): with `binary` an agent that ends the call is
recorded as `voice_ws_disconnect` and the attempt fails ungraded (section 3,
"Framing"). A mismatch breaks the call outright: each side ignores the other's
frames.

`wait_for_callee_first` is about the bench actor: `false` for an outbound
agent (the actor, as callee, speaks first). An inbound agent's candidate is
configured so the actor waits for the agent's greeting.

## 14. What this design fixes from rory-agent

| rory-agent problem | How vamos-agents avoids it |
|---|---|
| One repo per agent; a new benchmark means copying the whole stack | One framework, one pack per agent; adding an agent touches no framework file (section 9). |
| ~1,500 duplicated lines: 15 `web.py`, tool-surface copies, 13 Dockerfiles, Grok ≈ copy of OpenAI | One `server.py`, one dispatch path, one `Dockerfile` (`ARG AGENT` x `ARG VENDOR`), one Realtime-protocol base. |
| Call start implemented 15 ways (greeting fields, TTS frames, instructed turns) | One `CallStartPolicy`, honoured by every provider; parity tests assert what each sends, for both policies. |
| Vendor behaviour drift (barge-in truncation, VAD thresholds, date injection) | `TurnTaking` mapped into each VAD; `OutboundAudio.played_ms` drives truncation; the date is rendered into one prompt. Parity tests assert the frames each vendor sends. |
| Parity tests that only checked module attributes | `tests/test_parity.py` runs each provider through scripted calls, agents x vendors. |
| 73 unbounded `>=` deps, `uv:latest` | Every dependency bounded; `uv` and Python images pinned; `uv.lock` committed; `uv sync --locked`. |
| Unauthenticated inbound webhooks / tunnels for hosted vendors | No webhooks: only `/health` and `/voice`. |
| Full tool args logged (card data) | `redact` on the declaration; `CallSession.dispatch` logs `[redacted]`; tests assert card data is in no log, row or result. |
| Process-global clock that never adds elapsed time, falling back to wall time | `CallClock` per call: `call_time` + monotonic elapsed; AST guards ban wall-clock calls and SQL `now()`. |
| Module-global Stripe/Fineract config, created lazily | World clients built per agent in `from_env`, opened in `connect()`, injected into the call state. |
| Sync tool handlers wrapped in `to_thread` | Async handlers; blocking SDKs are wrapped inside the client. |

Kept on purpose: one shared tool schema and dispatch with a verification
gate in code; tools return `{"error": ...}` instead of crashing the call;
thin vendor adapters using each vendor's native tool calling and turn detection.

## 15. Rory and Riley: how an inbound agent maps (and migrating later)

Rory (veris-ai/rory-agent, read at `e6e9c24`) was checked against `AgentSpec`
on paper; it fits without framework changes, and the test-only
`tests/fake_inbound.py` agent has its shape and runs through every provider.

| Rory today | As an `AgentSpec` |
|---|---|
| Speaks first with a fixed `GREETING` ("Thanks for calling Acme Energy, this is Rory…"), triggered 15 ways | `call_start = agent_first(f'Greet the caller with exactly: "{GREETING}"')`; no connect reads (the caller is unknown until verified) |
| No per-call context; `RORY_REFERENCE_TIME` (ISO or epoch), frozen, falls back to wall time | `from_env` parses `RORY_REFERENCE_TIME` (required, no wall-time fallback); `call_time()` returns it; `CallClock` adds elapsed time; `AgentContext` stays empty |
| `verify_caller(account_number, name_on_account, second_factor)`; everything except `verify_caller`/`transfer_to_human` refused until `session.account` is set; 3 attempts | `GateLadder` with levels `CALLER < VERIFIED`; the two tools declare no `requires`, the other 14 declare `VERIFIED`; attempts counted on the call state |
| 16 tools: Stripe-only (bills, payment, extension, paperless, supplier), Fineract+Stripe (account, history, arrangements), none (transfer) | One `ToolRegistry` in `vamos_agents/rory/tools/`; `make_payment` declares its card fields in `redact` |
| Stripe via module globals; Fineract via `httpx` Basic auth + `Fineract-Platform-TenantId` (`FINERACT_API_BASE`, `_USER`, `_PASSWORD`, `_TENANT`) | `required_env = ("STRIPE_API_KEY", "FINERACT_API_BASE")`; `from_env` builds a `StripeClient` instance and a Fineract client; `connect()` checks both |
| Prompt `agent_desc.txt` + `today_context()` | `instructions(clock)`: the prompt plus the date from `clock` |
| Per-call `CallSession` with `account`, `failed_attempts`; no `end_call` | `open_call(clock)` returns that state; add an `end_call` tool so providers can hang up (or rely on the caller hanging up; `ended` stays False) |
| Vapi/PolyAI/Cartesia hosted vendors calling back over HTTP | Out of scope for the framework (no inbound endpoints); such a vendor would be a provider with its own authenticated callback, a framework decision to make then |

Migration path (optional, later): move `rory_tools/` into `vamos_agents/rory/` as the
pack above; port vendors one at a time into `vamos_voice/providers/` only where
the framework lacks them (Deepgram Agent, ElevenLabs...; Grok Voice and
GPT-Live already exist here); add `rory` to `AGENT_CASES`; build
`bench-e2e/rory-<vendor>` and switch candidates over by digest. Riley follows
the same path with its own world clients. Until then their images keep coming
from their own repos.

## 16. Testing

```bash
uv sync --all-extras                 # every agent's and vendor's deps
uv run --all-extras pytest -q        # framework, parity matrix, every pack (needs docker for Remi's Postgres)
uv run ruff check . && uv run ruff format --check .
```

- `tests/test_framework.py`: the boundary (no `agents` import, no wall clock),
  discovery, `AgentContext`, the gate engine and dispatch (refusals, redaction, never raises).
- `tests/test_parity.py`: agents x vendors, on the frames each provider sends.
- `tests/test_grok_voice.py`, `tests/test_gpt_live.py`: each vendor's own
  frame translation (session shape, priming fallback / backend forcing,
  barge-in, end of call), pinned where it differs from OpenAI Realtime.
- `tests/test_realtime_turns.py`: the Realtime call flow after priming (text-only priming,
  vendor-native turn taking, the deadline mid-priming, a request refused for an active response).
- `tests/test_smoke_inbound.py`: the real server with the fake inbound agent over `/voice`.
- `tests/test_framing.py`: both framings, alone and through the real server (agent hang-up
  sends `end` before the 1000 close, a caller's `end` finalizes the call, binary unchanged),
  and Pipecat's serializer.
- `vamos_agents/remi/tests/`: Remi's rules, tools against a real Postgres
  (`docker run postgres:16-alpine`, or `REMI_TEST_DATABASE_URL`) loaded with
  the World's own SQL (schema, payment gateway, rows; vendored in
  `tests/fixtures/collections_world.sql`, resynced with `resync_world.sh`),
  the payment gateway client against the real gateway functions, `/voice` smoke test.

No test calls a paid API.

## 17. Hard rules

- `vamos_voice` never imports `agents.*`; providers see only the `CallRuntime`. Rules live in packs.
- One clock: `CallClock`. No wall time, no SQL `now()`.
- Card number, expiry, CVC and bank account numbers: only to the payment processor; declared `redact`; never logged, stored or returned.
- Every tool call goes through `CallSession.dispatch`; tools return `{"error": ...}`, never crash the call.
- Every dependency bounded; `uv.lock` committed; base images pinned. No secrets in code or images.
- One `Dockerfile`; one image per agent x vendor.
- No inbound endpoints beyond `/health` and `/voice`.
- Vendor traffic via `wire.connect_websocket` or an instrumented SDK, so the judge's trace exists.
- Never import or copy a benchmark's grader into a pack.
