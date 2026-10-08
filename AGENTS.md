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
  screen_pop.py                   # ScreenPop: the agent's connect-time reads, appended to the instructions
  audio.py                        # PCM16 helpers, streaming resampler
  tools/registry.py               # ToolRegistry: pydantic args -> one JSON schema, requires/redact/tags
  session/engine.py               # CallSession: the one dispatch path (gates, redaction, never raises)
  session/gates.py                # Gates protocol + GateLadder (ordered state machine)
  providers/                      # the vendor seam and the vendors
    base.py realtime_protocol.py wire.py hosted.py
    openai_realtime/  grok_voice/     # Realtime protocol: OpenAI natively, Grok as a dialect
    gemini_live/  gpt_live/  pipecat/
    deepgram/  elevenlabs/  fluxions/ # managed voice agents: the vendor runs STT+LLM+TTS, tools client-side
    cascade.py silero.py openai_chat.py data/   # the in-pod cascade (STT -> LLM -> TTS) and its Silero VAD
    mistral/  huggingface/  grok_cascade/       # cascades: the vendor's legs on cascade.py
    gradium/                                    # gradbot: Gradium's engine runs the cascade itself
    vapi/  cartesia/                  # hosted orchestrators (the vendor runs STT, LLM, TTS)
    livekit/                          # LiveKit Agents in a room on the image's own livekit-server
vamos_agents/
  remi/                           # Remi's pack: AgentSpec impl, prompt, tools, policy, gates,
    agent.py ... tests/           # world clients (db; payments via the World's gateway; the Fineract ledger), and its own tests
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
│ WS  /voice   one call per pod: clock, CallSession, agent.screen_pop() (connect reads, │
│              before any vendor session), CallRuntime → provider.run_call()            │
│              → agent state's finalize()                                               │
│ POST /vendor/<name>/...  only a provider's own router (Vapi's tool webhook), each      │
│              route authenticated by a per-call secret, dispatching via CallSession    │
└───────────────────────────────────────────────────────────────────────────────────────┘
      │ CallRuntime: setup (instructions + screen pop, tool specs, TurnTaking, CallStartPolicy)
      │              start (CallStart)  inbound (caller audio)  outbound (gated)
      ▼
┌──────── VoiceProvider (providers/base.py): the vendor seam ────────┐
│ openai_realtime → RealtimeProtocolProvider (GA protocol, once)     │
│ grok_voice      → RealtimeProtocolProvider (xAI dialect)           │
│ gemini_live     → raw Gemini Live protocol                         │
│ gpt_live        → GPT-Live live sessions + Responses delegation    │
│ pipecat         → Pipecat STT → OpenAI LLM → TTS                   │
│ deepgram        → Deepgram Voice Agent (nova-3, managed LLM, aura) │
│ elevenlabs      → ElevenLabs ConvAI (a stored agent per call)      │
│ fluxions        → Fluxions realtime (bring-your-own tools)         │
│ mistral         → CascadeProvider: Voxtral STT → Mistral → Voxtral │
│ huggingface     → CascadeProvider: Whisper → HF router LLM → Kokoro│
│ grok_cascade    → CascadeProvider: xAI STT → gpt-4.1-mini/Grok →   │
│                   xAI TTS                                          │
│ gradium         → gradbot engine: Gradium STT → LLM → Gradium TTS  │
│ vapi            → hosted: inline assistant, server tools (webhook) │
│ cartesia        → hosted: managed agent, client tools on its WS    │
│ livekit         → LiveKit Agents (Deepgram, OpenAI, ElevenLabs) in │
│                   a room on the image's own livekit-server         │
└─────────────────────────────────────────────────────────────────────┘
      │ every tool call: session.dispatch(name, args) → dict ({"error": ...} on refusal)
      ▼
┌──────── CallSession (session/engine.py) ────────┐      ┌──────── AgentSpec (vamos_agents/<name>/) ────────┐
│ registry lookup → gates.refusal(state, tool)    │◀─────│ tools (ToolRegistry)   gates (Gates)        │
│ → args validation → handler(state, args)        │      │ call_start   instructions(clock)            │
│ redact in logs                                  │      │ open_call(clock) → per-call state           │
└─────────────────────────────────────────────────┘      │ screen_pop(state) → ScreenPop (reads done)  │
                                                         │ from_env / connect / close: world clients   │
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
play), a provider failure and the server's end of call all
go through it, so a deliberate end in `json` always sends `end` before the
close. Pipecat owns its transport, so its `LineSerializer` applies the same
`Framing`: the pipeline's `EndFrame`/`CancelFrame` is written as `end` before
Pipecat closes with 1000, and a caller's `end` cancels the pipeline.

## 4. The boundary: framework vs pack, and why

| Lives in `vamos_voice/` (framework) | Lives in `vamos_agents/<name>/` (pack) |
|---|---|
| server, `/health`, `/voice`, one call per pod | the agent's name, persona, prompt text |
| `CallerLine`, inbound audio, gated outbound audio, barge-in playout | the per-call state and its `finalize()` (e.g. Remi's contact-attempt row) |
| `CallStartPolicy` mechanics in every provider; running the screen pop before any vendor session | which policy, what the opening says; the screen pop (which reads, how they render, what they record on the state) |
| `AgentContext` (string map), `CallClock` | the typed view of the context (Remi: account, phone, call time in Eastern) |
| `ToolRegistry`, JSON-schema reduction, `CallSession.dispatch`, redaction | the tools, their gates (levels and messages), domain rules (`policy.py`) |
| `GateLadder` engine | the ladder's levels and explanations |
| vendor protocols, tool-format conversion, VAD mapping | world clients (Postgres, a payment gateway, Fineract...) built from the agent's env |

Why the line is there:

- **Vendor work is the expensive, drift-prone part.** Barge-in truncation,
  the call-start gate, VAD mapping and trace-friendly sockets are written
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
    async def screen_pop(self, call: CallState) -> ScreenPop | None: ...  # optional (section 6)
    def describe(self) -> str: ...  # one log line, no secrets
```

The per-call state satisfies `CallState`: an `ended: bool` (set by the agent's
hang-up tool; providers then close the line) and `async def finalize()` (for
whatever the call must always leave behind). The framework runs `finalize`
exactly once (`CallSession.finalize`), as early as it can:

- the moment the caller hangs up or the socket drops (`CallerLine.caller_gone`,
  which the server points at `CallSession.caller_gone`; Pipecat's serializer and
  disconnect handler call it too), not after the provider has torn down its
  vendor session. A vendor socket's close handshake can take seconds (2 s on
  OpenAI Realtime, Grok and GPT-Live, 1.1 s on Mistral), and the bench drains
  the World's log one to two seconds after the hang-up: a record written after
  the teardown was lost on 11 of 212 Remi attempts (`trial_vj4i37mlw6q9lqc58lprk`);
- otherwise when the provider returns or fails;
- at the latest when the server shuts down (a pod stopped mid-call), before the
  agent's world clients close.

From `caller_gone` on, `dispatch` refuses new tool calls ("the caller has hung
up"), and `finalize` waits (up to 10 s) for any tool call already running, so
the agent's record comes after the call's last effect.

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

## 6. Call start: the screen pop, then who speaks first

### The screen pop (`vamos_voice/screen_pop.py`)

A real collector's dialer shows the account on screen when the call
connects; nobody looks it up while the person says "Hello?". Agents do the
same. `AgentSpec.screen_pop(call)` runs the agent's connect-time reads
**itself**, on the call's fresh state, through its own world clients and the
same handlers its tools use (no second copy of the SQL), records them on the
state so its gates treat them as done, and returns a `ScreenPop(heading,
body)`. The server (`call_runtime`) runs it once per call, after the state
exists and **before the provider opens its vendor session**, and appends
`# <heading>` and the body to the instructions every vendor receives.

So, for every vendor identically:

- the reads finish before any caller audio reaches a vendor and before any
  agent audio reaches the caller (the vendor session does not exist yet);
- they are the agent's own SQL on its own database role, so the World's SQL
  log shows them first (Remi's grader accepts the model's tool calls *or* the
  SQL log's SELECTs on accounts/phones/contact_attempts before the first
  spoken turn);
- the model knows the account from its first token: the section is part of
  the session instructions (OpenAI/Grok `session.update`, Gemini
  `systemInstruction`, GPT-Live `session.start` for both the live model and its
  backend, Pipecat's system instruction);
- nothing asks the model to act before the callee speaks: no forced
  `tool_choice`, no priming response, no labelled note, no instruction append.

What the section shows is the agent's to decide, under its own redaction rules
(Remi renders exactly the redacted dicts its read tools return: no date of
birth, ZIP, SSN or street; verification stays server-side). The agent still
decides everything from that data (should-not-call, cap reached, cease on
file) and still discloses nothing it should not: the policy is unchanged.

A screen pop that raises is logged (`screen pop failed for <agent>`) and the
call starts without the section; the read tools still exist, the instructions
say to run them when the section is missing, and the gates name any read not
done. An agent with nothing to show returns None (the default).

**Why not priming.** The framework used to "prime" the model: hold the
caller's audio and make the model call the reads (forced `tool_choice` on
OpenAI and Pipecat, labelled notes on Gemini and Grok, the backend's
`tool_choice` on GPT-Live) under a 6 s deadline. It was unreliable and
vendor-dependent (mini-run `trial_5sy9iim6ancmrxjd9pp0b`): on GPT-Live the
reads never ran, the deadline fired and the model, with no account, asked for
"Dana Whitlock" (the agency's callback contact) instead of the consumer (0/10);
on gpt-realtime sometimes only `get_account` ran before it spoke. The reads
are plumbing, not behaviour under test, so the agent does them, the same way
on every vendor; turn taking stays each vendor's own.

### Who speaks first (`vamos_voice/call_start.py`)

| | `callee_first()` (outbound: Remi) | `agent_first(opening_instruction)` (inbound: Rory, Riley) |
|---|---|---|
| First words | the callee's: caller audio flows to the vendor at once; no agent audio leaves until their first utterance is detected; anything earlier is dropped | the agent's: the provider requests an opening turn carrying `opening_instruction`; audio flows at once |
| OpenAI Realtime / Grok Voice | nothing is requested; the vendor's VAD answers the callee | `response.create` with `instructions = session instructions + opening_instruction` |
| Gemini Live | nothing is requested | a user-role text turn labelled `[Call system note, not the person on the line]` carrying the opening (the Live API has no other way to ask for a turn) |
| GPT-Live | nothing is requested | `session.instructions.append` with `opening_instruction`, after `session.started` |
| Pipecat | the LLM runs only on a user turn | `LLMMessagesAppendFrame(opening, run_llm=True)` when the caller's socket connects |
| Deepgram Voice Agent | no `greeting` in `Settings`; nothing is requested | `InjectUserMessage` carrying the labelled `opening_note` (no other way to ask for an LLM turn) |
| ElevenLabs ConvAI | the per-call agent has no `first_message`; nothing is requested | `user_message` carrying the labelled `opening_note` |
| Fluxions | `greet: false`; nothing is requested | `greet: true`: Fluxions' own opening, generated from the soul. No message asks for a turn (`user.text` is ignored on `/v1/realtime`), so `opening_instruction` itself is not sent |
| Cascades (Mistral, Hugging Face, Grok cascade) | the LLM runs only on a finished caller turn | an opening round with `opening_instruction` appended as a system message, at once |
| Gradium (gradbot) | `assistant_speaks_first=False`: gradbot waits for the caller's turn | `assistant_speaks_first=True` (gradbot sends the model a literal `[start]`); `opening_instruction` is appended to the instructions, the only place gradbot reads it |

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
   `required_env`, `from_env`, `call_time`, `connect`/`close`, `instructions`, `open_call`,
   and `screen_pop` if the agent knows who it is talking to when the call connects (section 6).
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
4. Call start: send `setup.instructions` (the screen pop is already in them)
   in the session setup; once the vendor accepts audio, `call.inbound.bind(...)`
   and run `call.inbound.run()`; if `setup.start_policy.agent_opens` request
   the opening turn. Never ask the model to act before the callee speaks. Call
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
| Forced tool choice (never used: the screen pop needs none) | `response.create` `tool_choice` | **no** (undocumented) | **no** | on the backend only (left `"auto"`) | `tool_choice` on the SDK call |
| Barge-in truncation (context cut to what was heard) | `conversation.item.truncate` at `played_ms` | **no**: xAI lists truncate/cancel as unsupported. Grok stops generating itself on caller speech; its context may keep a few seconds the caller never heard. `truncates_on_barge_in = False` | vendor-side (`START_OF_ACTIVITY_INTERRUPTS`) | **no** such event: full duplex, the live model stops itself | Pipecat's interruption |
| Caller transcription (in the judge's trace) | `audio.input.transcription` (`gpt-4o-mini-transcribe`) | only with `transcription.model = "grok-transcribe"` (always sent) | `inputAudioTranscription` | always on (`session.input_transcript.delta`) | STT |
| `TurnTaking` mapping | silence, prefix padding, threshold | silence only (`threshold`/`prefix_padding_ms` not documented; xAI defaults) | silence, prefix padding | **none**: no VAD settings exist | silence via VAD + turn timeout |
| Turn management (who gets a reply, what may interrupt) | vendor: server VAD, `create_response`/`interrupt_response` on | vendor: server VAD | vendor | vendor | Pipecat |
| Callee-spoke signal | `input_audio_buffer.committed` | same | first input transcription | first non-empty `session.input_transcript.delta` | first user transcript |
| Language (every VAmoS call is English) | transcription `language: "en"` | **none** documented (the prompt holds it) | **none**: native-audio models choose the language themselves and take no `languageCode` (the prompt holds it) | **none** documented (the prompt holds it) | `Language.EN` on Deepgram STT, OpenAI STT and Cartesia TTS (OpenAI's speech endpoint takes none) |

Managed voice agents (the vendor runs STT, the LLM and TTS; tools are
client-side over the same socket):

| Capability | deepgram | elevenlabs | fluxions |
|---|---|---|---|
| Where the instructions and tools go | `Settings` (`agent.think.prompt` / `.functions`), per call | a stored agent **created per call** (REST) and deleted after it; the prompt also rides `conversation_initiation_client_data` (for the trace) | `session.update` `soul` / `tools`, per call. **The host caps the soul** (`GET /config` `max_soul_chars`, 4,000 on `api.fluxions.ai`) and silently truncates: a call over the cap fails before connecting |
| Tool schema changes | none | every parameter needs a `description`; an undescribed one gets its own name | none |
| Barge-in truncation | **no**: Deepgram stops itself on `UserStartedSpeaking` | **no**: ElevenLabs stops itself (`interruption`) | the client is the playback buffer: audio arrives ~6x real time, is released at most 1 s ahead, `audio.flush` drops the rest, `playback.pos` reports what was heard |
| Caller transcription | `ConversationText` (role `user`) | `user_transcript` | `user.transcript` |
| `TurnTaking` mapping | **none** (`nova-3` has no endpointing setting) | **none** (a learned turn model; `turn_eagerness: normal`) | **none** (an adaptive end-of-utterance model with no knob) |
| Callee-spoke signal | first user `ConversationText` | first non-empty `user_transcript` | first non-empty `user.transcript` |
| Audio | linear16 24 kHz both ways (binary frames) | `pcm_24000` both ways (base64 JSON) | binary; 16 kHz up (resampled), 24 kHz down |
| Hang-up after `end_call` | at `AgentAudioDone` if the agent is speaking, else at once | once the agent's audio has been quiet 0.6 s (max 10 s): no end-of-audio event | at `agent.done` if a turn is in progress, else at once |

The other vendors' language settings: Deepgram Voice Agent `language: "en"`,
ElevenLabs `language: "en"`, Vapi `"en"`, Cartesia `{"primary": "en"}`,
Gradium `Lang.En`, Hugging Face Whisper `language=en`, the Grok cascade's xAI
STT/TTS `language=en`, LiveKit Deepgram STT `en-US` (its TTS model,
`eleven_flash_v2`, is English-only); Fluxions and Mistral's Voxtral realtime
document none, so the prompt holds it there. Pinning a language is
configuration, not tuning: the prompt says the same to every vendor.

`RealtimeProtocolProvider` exposes the hook dialects differ on:
`truncates_on_barge_in` (default `True`).

### In-pod cascades (`providers/cascade.py`)

Mistral, Hugging Face and xAI's STT/TTS sell speech in and speech out as
separate services; no vendor holds the session. So the pipeline runs in the
pod, written once in `CascadeProvider` and shared, and a vendor supplies only
its three legs: `SpeechToText` (caller audio in, finished turns out),
`ChatModel` (one LLM round: text deltas, then tool calls) and `TextToSpeech`
(text in, PCM16 24 kHz out). Each matches the VAmoS Energy candidate of the
same vendor (rory-agent `mistral/`, `huggingface/`, `grok-cascade/`), so the
vendors are compared under the same plumbing on both benches.

- **Turns.** The model runs only on a finished caller turn. Where the vendor
  has endpointing it decides (xAI STT's Smart Turn, `speech_final`). Where it
  has none (Voxtral Realtime documents none; Whisper on HF is
  request/response), the turn ends after `TurnTaking.silence_ms` of
  non-speech by Silero (`SilenceEndpoint`, counted in audio time), the
  Energy candidates' rule. Turns that finish while the agent answers are
  answered together, in one user message.
- **Barge-in.** None of these vendors detects the caller talking over the
  agent, so Silero (`silero.py`, the model Pipecat ships, vendored with its
  MIT licence) runs on the caller's audio. Under `BargeInRule`, a framework
  default identical for every cascade, the caller interrupts a turn being
  answered only after **0.5 s of Silero speech** in one utterance (pauses under
  300 ms don't end it; counted from when the reply began) **and at least one
  transcribed word** where the STT leg streams interim text (Voxtral
  `transcription.text.delta`, xAI `transcript.partial`; Whisper has none, so
  the duration rule alone). That is the shape of LiveKit Agents' and Pipecat's
  defaults (`min_interruption_duration` 0.5 s, a minimum word count). Before it,
  the first Silero speech onset interrupted, so under the bench's background
  noise a 0.4 s blip muted every reply (smoke trial
  `trial_5bah0up0gmvijc2xo4zfb`, grok_cascade: Remi never spoke). Noise stays
  a benchmark condition; sustained speech still interrupts. The interruption
  stops the audio (`OutboundAudio.interrupted`), clears the vendor's
  TTS utterance (xAI `text.clear`), and silences the rest of the turn. The LLM
  round it lands in still completes and its tool calls still run; the reply is
  kept in the history as the words the caller can have heard
  (`OutboundAudio.played_ms`), marked `… [interrupted by the caller]`.
- **Playout.** TTS audio leaves in 0.5 s slices, at most 1 s ahead of real
  time (vendors synthesize faster than speech plays; audio already at the
  caller cannot be cut).
- **Rounds.** Up to 5 per turn: a round's text is spoken as it streams, then
  its tool calls run in order through `CallSession.dispatch`; a call whose
  arguments are not JSON is answered with an error, not run. When a tool ends
  the call, no round follows: the last words play and the line closes.

**Gradium** is different: gradbot (Gradium's engine, a Rust core behind
`gradbot.run`) runs STT, the LLM and TTS itself and owns turn taking,
barge-in and the history, so `providers/gradium` only bridges the line and
answers tool calls. gradbot re-prompts the model while a tool call is
pending and the model often re-issues the same call; `ToolCalls` answers an
identical re-issue with the earlier result instead of running it twice (the
Energy candidate's rule). Its silence nudges (`silence_timeout_s`) are off, as
on Energy.

| Capability | mistral | huggingface | grok_cascade | gradium |
|---|---|---|---|---|
| End of turn | local: Silero + `silence_ms`, then `input_audio.flush` | local: Silero + `silence_ms`, utterance to Whisper | xAI Smart Turn (`smart_turn=0.7`, 3 s timeout) | gradbot |
| Barge-in | local Silero, `BargeInRule` (0.5 s + 1 interim word) | local Silero, `BargeInRule` (0.5 s; no interim text) | local Silero, `BargeInRule` (0.5 s + 1 interim word) + `text.clear` | gradbot (`interrupted` event) |
| History after barge-in | trimmed to what played | trimmed | trimmed | gradbot keeps only transmitted words |
| Forced tool choice | never (`tool_choice="auto"`) | same | same | not exposed |
| Callee-spoke signal | first finished turn | same | same | first `stt_text` |
| `TurnTaking` mapping | `silence_ms`, `vad_threshold` (Silero start) | same | `vad_threshold` (barge-in only) | none: gradbot's defaults |
The hosted orchestrators and LiveKit (the vendor, or LiveKit Agents, runs the
whole STT → LLM → TTS loop and its turn taking):

| Capability | vapi | cartesia | livekit |
|---|---|---|---|
| What runs where | Vapi: Deepgram `nova-3`, OpenAI `gpt-4.1-mini`, ElevenLabs `eleven_flash_v2` | Cartesia: Ink STT, `gpt-4.1` (its catalog has no `gpt-4.1-mini`), Sonic TTS | this process: LiveKit Agents `AgentSession` (Deepgram `nova-3-general`, `gpt-4.1-mini`, ElevenLabs `eleven_flash_v2`, Silero VAD) in a room on the image's own `livekit-server` |
| Per-call vendor resources | an inline assistant on the call (nothing stored but the call record) | a managed agent, **deleted at the end of the call**; client tools are shared account resources, reused when identical (an account holds 100) | a room, deleted at the end of the call |
| Tools reach `CallSession.dispatch` via | Vapi POSTs `tool-calls` to `POST /vendor/vapi/tool` (needs `PUBLIC_BASE_URL`; per-call secret in `X-Vamos-Tool-Secret`, and the message must name the live Vapi call) | `client_tool_call` / `client_tool_result` on the agent's own WebSocket (no webhook, no public endpoint) | raw-schema function tools in-process |
| Tool schema | the shared JSON schema | cut to `type`/`description`/`enum`/`items` (Cartesia rejects every other keyword); names Cartesia reserves (`end_call`, `send_dtmf`, `voicemail_detection`, `transfer_to_number`) get a trailing `_` on the wire and are mapped back | the shared JSON schema; one tool call per step, up to 10 steps a turn |
| Callee first | `firstMessageMode: assistant-waits-for-user` | `initial_message: null` | nothing is generated before a user turn |
| Agent first | `assistant-speaks-first-with-model-generated-message` + the opening instruction as a second system message | **no**: no way to request a turn; `opens_calls = False`, startup refuses an agent that speaks first | `generate_reply(instructions=opening_instruction)` |
| `TurnTaking` mapping | `startSpeakingPlan` (`waitSeconds` = silence, transcription endpointing as the Energy candidate) | none exposed (Cartesia defaults) | Silero silence/prefix/threshold + endpointing `min_delay` |
| Callee-spoke signal | first user `transcript` | first user `turn_ended` with text | first `user_input_transcribed` |
| Agent hang-up | after the tool: wait for Vapi's audio to stop (`OutboundAudio.wait_until_quiet`, digital silence counts as quiet: Vapi streams it), drain, close the line, send Vapi `end-call` | same wait, then close (the agent is deleted) | `wait_for_playout()` of the words before the tool, `StopResponse` (no reply), close |

### Providers that are called back (webhooks)

A hosted vendor that executes tools by calling a URL (Vapi) contributes HTTP
routes with `VoiceProvider.router()`; the server mounts them at
`/vendor/<name>` (nothing else is ever mounted). The rules:

- **Authenticated, always.** The platform adds no auth (below), so the provider
  mints a secret per call, hands it to the vendor only inside that call's
  configuration, and refuses (401) any request without it, and (404) any that
  names a call other than the live one. After the call the route refuses everything.
- **One dispatch path.** A webhook runs tools only through the live call's
  `CallSession.dispatch` (gates, validation, redaction), one at a time; never
  a parallel path.
- **The public address.** Veris Bench gives an image candidate whose `image`
  block has `"public_endpoint": true` the env var
  `PUBLIC_BASE_URL=https://<voice host>/hooks/<attempt id>` (set before the
  process starts; it overrides any user value). Its gateway strips
  `/hooks/<attempt id>` and forwards every path, method and header (it adds
  `X-Attempt-Id`) to the pod's `port` (or `public_endpoint_port`), for the
  attempt's lifetime. Before the actor dials, the engine probes
  `PUBLIC_BASE_URL` + `health_path` and fails the attempt
  (`public_endpoint_unreachable`) if it does not answer. The attempt id is the
  only secret in the URL, which is why the routes authenticate themselves
  (veris-sandbox `attempt-engine/app/candidate.py`, `webhook-gateway/`).
- Prefer a vendor's socket-borne tools when it has them (Cartesia's client
  tools): no public endpoint at all.

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
| `DEEPGRAM_API_KEY` | deepgram | Required; the only credential (the LLM runs on Deepgram's managed OpenAI access). |
| `VAMOS_DEEPGRAM_LISTEN_MODEL` / `VAMOS_DEEPGRAM_LLM_MODEL` / `VAMOS_DEEPGRAM_VOICE` | deepgram | Defaults `nova-3` / `gpt-4.1-mini` / `aura-2-thalia-en` (the VAmoS Energy Deepgram candidate). |
| `ELEVENLABS_API_KEY` | elevenlabs | Required; needs ConvAI agent create/delete permission. |
| `VAMOS_ELEVENLABS_LLM` / `VAMOS_ELEVENLABS_TTS_MODEL` / `VAMOS_ELEVENLABS_VOICE_ID` | elevenlabs | Defaults `gpt-4.1-mini` (Energy's `ELEVENLABS_LLM`) / `eleven_flash_v2` / `EXAVITQu4vr4xnSDxMaL`. |
| `FLUXIONS_API_KEY` | fluxions | Required; sent bare in the handshake's `Authorization` header. |
| `VAMOS_FLUXIONS_BASE_URL` / `VAMOS_FLUXIONS_VOICE` | fluxions | Defaults `https://api.fluxions.ai/vui` (the socket is `<base>/v1/realtime`, `wss`) / `maeve` (resolved to its full `voice_id` at startup). |
| `MISTRAL_API_KEY` | mistral | Required. |
| `VAMOS_MISTRAL_LLM_MODEL` / `VAMOS_MISTRAL_STT_MODEL` / `VAMOS_MISTRAL_TTS_MODEL` / `VAMOS_MISTRAL_VOICE` | mistral | Defaults (the Energy candidate's) `mistral-medium-2604` / `voxtral-mini-transcribe-realtime-2602` / `voxtral-mini-tts-2603` / `en_paul_neutral` (a Voxtral preset slug, resolved at startup; unknown fails the boot). |
| `HF_TOKEN` | huggingface | Required; bills all three legs (serverless Inference Providers). |
| `VAMOS_HF_LLM_MODEL` / `VAMOS_HF_STT_MODEL` / `VAMOS_HF_TTS_MODEL` / `VAMOS_HF_TTS_VOICE` | huggingface | Defaults `openai/gpt-oss-120b:groq` (Energy's) / `openai/whisper-large-v3` / `hexgrad/Kokoro-82M` (needs a live fal-ai route; checked at startup) / `af_heart`. |
| `XAI_API_KEY` | grok_cascade | Required (STT, TTS, and the LLM when it is Grok). |
| `VAMOS_GROK_CASCADE_LLM` | grok_cascade | `gpt-4.1-mini` (default, on OpenAI: needs `OPENAI_API_KEY`) or `grok-4.3` (on xAI, `reasoning_effort: "none"`). Anything else stops startup. |
| `VAMOS_GROK_STT_MODEL` / `VAMOS_GROK_VOICE` | grok_cascade | Defaults `grok-voice-transcribe-2.0` / `carina` (the cascade's default voice differs from `grok_voice`'s). |
| `GRADIUM_API_KEY` / `OPENAI_API_KEY` | gradium | Both required: Gradium STT/TTS, and the LLM gradbot calls. |
| `VAMOS_GRADIUM_LLM_MODEL` / `VAMOS_GRADIUM_VOICE_ID` | gradium | Defaults `gpt-4.1-mini` (Energy's) / `4SZHfMpw-p46Ywgs` (Gradium's "Harper"). |
| `VAPI_API_KEY` | vapi | Vapi **private** key. Required. |
| `PUBLIC_BASE_URL` | vapi | The pod's public https URL; set by Veris Bench when the candidate has `"public_endpoint": true`. Required. |
| `VAMOS_VAPI_MODEL` / `VAMOS_VAPI_MODEL_PROVIDER` | vapi | Defaults `gpt-4.1-mini` / `openai`. |
| `VAMOS_VAPI_TRANSCRIBER_MODEL` / `VAMOS_VAPI_TRANSCRIBER_PROVIDER` | vapi | Defaults `nova-3` / `deepgram` (keep a transcriber without its own end-of-turn model, or Vapi ignores `startSpeakingPlan`). |
| `VAMOS_VAPI_VOICE_ID` / `VAMOS_VAPI_VOICE_MODEL` / `VAMOS_VAPI_VOICE_PROVIDER` | vapi | Defaults `EXAVITQu4vr4xnSDxMaL` / `eleven_flash_v2` / `11labs`. |
| `CARTESIA_API_KEY` | cartesia | Required. No agent id: the provider creates one per call. |
| `VAMOS_CARTESIA_MODEL` / `VAMOS_CARTESIA_VOICE_ID` | cartesia | Defaults `gpt-4.1` / `db6b0ed5-d5d3-463d-ae85-518a07d3c2b4` ("Skylar"). |
| `OPENAI_API_KEY` / `DEEPGRAM_API_KEY` / `ELEVENLABS_API_KEY` | livekit | All required. |
| `VAMOS_LIVEKIT_LLM_MODEL` / `VAMOS_LIVEKIT_STT_MODEL` / `VAMOS_LIVEKIT_TTS_VOICE_ID` / `VAMOS_LIVEKIT_TTS_MODEL` | livekit | Defaults `gpt-4.1-mini` / `nova-3-general` / `EXAVITQu4vr4xnSDxMaL` / `eleven_flash_v2`. |
| `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | livekit | Optional. Default: the image's own server at `ws://localhost:7880`, started on loopback with credentials minted per process. Point `LIVEKIT_URL` elsewhere (with its key and secret) to use another server; then none is started. |

Agents:

| Variable | Agent | Meaning |
|---|---|---|
| `VERIS_AGENT_CONTEXT` keys `account_id`, `phone_id`, `call_time` | remi | Required; `TAS-NNNNNN`, a `phones` row id, ISO-8601 with offset. |
| `DATABASE_URL` | remi | Postgres DSN of the collections twin (bench-injected). Required. Payments are charged through the payment gateway inside that database (`gateway_tokenize_card`, `gateway_tokenize_ach`, `gateway_charge`). |
| `FINERACT_API_BASE` | remi | The Fineract twin's host (bench-injected): the ledger holding every account's money. Required. |
| `FINERACT_USER`, `FINERACT_PASSWORD`, `FINERACT_TENANT` | remi | Fineract's Basic pair and tenant (bench-wide candidate-env, set by utility-bench `publish world`). Required. |

Any missing required variable stops the process at startup (exit 2) with a message naming it.

**Key names.** Vendor keys are bench candidate-env secrets under their
standard names, `OPENAI_API_KEY`, `GEMINI_API_KEY` and `XAI_API_KEY`, as on
VAmoS Energy, so one bench-wide secret serves every candidate on that vendor.
Veris Bench allows these exact names (veris-sandbox
`benchmark/api/app/services/candidate_env.py`), while every other `GEMINI_*`,
`XAI_*`, `GROK_*` and `VERIS_*` name stays reserved (`MISTRAL_API_KEY`, `HF_TOKEN` and
`GRADIUM_API_KEY` are free names, used as-is): those namespaces belong to
the platform and to the coding-agent harnesses it runs (their CLIs keep their
own workspace key), and a bench-wide value there would re-route them. That is
why every other vendor setting here is `VAMOS_*`; a new variable must not use a
reserved prefix (`tests/test_parity.py` pins the key names). `VAPI_API_KEY`,
`CARTESIA_API_KEY`, `DEEPGRAM_API_KEY` and `ELEVENLABS_API_KEY` are standard
names too, allowed in candidate-env, and shared with the VAmoS Energy candidates.

## 12. Traces per vendor (what the judge sees)

The judge reads an agent trace built by the bench's OTel bundle; for Remi its
first check is that the account, dialed-phone and contact-history reads come
before anyone is asked for by name. The screen pop (section 6) makes that true
through the World's SQL log, which the grader accepts as well as tool calls;
the trace shows the reads' results in the session instructions.

| Vendor | `trace_adapter` | How the trace is built | Requirement |
|---|---|---|---|
| `openai_realtime` | `openai-realtime` | The adapter hooks the `websockets` asyncio client's `send`/`recv` and reads GA Realtime events. | Keep traffic on `wire.connect_websocket`, JSON text frames; keep `audio.input.transcription` on so the caller's words are traced. |
| `grok_voice` | `openai-realtime` | The Realtime adapter, which already reads Grok's dialect (top-level `turn_detection`, no `output_item.added`, no `response.done` for a response cancelled by speech). | `grok-transcribe` on, or the caller is missing from the trace. |
| `gpt_live` | `gpt-live` | Same hook: system and model from `session.start`, tools from `session.delegation.responses.tools`, tool calls from nested `response.output_item.done`, results from `response.item.create`, both speakers from timed transcript deltas. | Tools only on the delegation backend; results only via `response.item.create`. An opening instruction is `session.instructions.append` and never appears as the caller. |
| `gemini_live` | `gemini-live` | Same hook; camelCase setup/toolCall/toolResponse frames. | **Developer API only** (function calls carry `id`s; Vertex omits them). Opening notes appear as labelled `user` turns. |
| `pipecat` | none (omit) | The LLM runs in-process through the `openai` SDK, which the bundle instruments (OpenInference `OpenAIInstrumentor`): each completion is an LLM span with tool calls; results arrive in the next request. | Keep LLM calls on the `openai` SDK. |
| `deepgram` | `deepgram-voice-agent` | Same hook: system, tools and model from `Settings`, speech from `ConversationText`, calls from `FunctionCallRequest`, results from `FunctionCallResponse`. | Keep the raw protocol on `wire.connect_websocket`. |
| `elevenlabs` | `elevenlabs-convai` | Same hook: the system prompt from the `conversation_initiation_client_data` override (tools live on the stored agent, so the trace has no tool schemas), `user_transcript`, `agent_response`, `client_tool_call` / `client_tool_result`. | The raw protocol (not the SDK); the prompt override is always sent. |
| `fluxions` | `fluxions-realtime` | Same hook: `session.update` soul and tools, `session.created` model, `user.transcript`, `agent.sentence`, `tool.call` / `tool.result`, `agent.done`. | Keep the raw protocol on `wire.connect_websocket`. |
| `huggingface`, `grok_cascade` | none (omit) | Same: every round is one `openai` SDK call (`openai_chat.OpenAIChat`). | Keep LLM calls on the `openai` SDK. |
| `mistral` | none (omit) | Every round is one `mistralai` SDK `chat.complete_async`, which the bundle instruments (`MistralAIInstrumentor`). | Keep LLM calls on the `mistralai` SDK (2.x, as on Energy). |
| `gradium` | `gradbot` | The model is called from gradbot's Rust core, so no SDK span exists; the adapter wraps `gradbot.run` and reads messages and tool answers off the handles. | Call `gradbot.run` by attribute, keywords only, at call time. |
| `vapi` | `vapi` | Hooks the `websockets` client and reads what Vapi sends on the transport socket: `transcript` (final), `speech-update`, `tool-calls`, `tool-calls-result`, `conversation-update` (system prompt and tool results), `status-update`. It does not call Vapi's API. | Those types in the assistant's `clientMessages`; the socket on `wire.connect_websocket`. |
| `cartesia` | `cartesia` | Same hook on the agent WebSocket: `client_tool_call`, `turn_output_text_delta`, `turn_ended` received; `client_tool_result` sent. | Client tools (their calls and results cross this socket); `wire.connect_websocket`. |
| `livekit` | none (omit; the bench has no `livekit` adapter) | As Pipecat: LiveKit's OpenAI plugin calls the `openai` SDK in this process. | Keep the LLM on the OpenAI plugin. |

## 13. Build, push, register on the bench

Images: `us-central1-docker.pkg.dev/veris-ai-dev/bench-private-images/bench-e2e/<agent>-<vendor>`,
with the vendor's underscores as dashes (the registry's convention, e.g.
`riley-gemini-live`): `remi-openai-realtime`, `remi-gemini-live`, `remi-grok-voice`,
`remi-gpt-live`, `remi-pipecat`, `remi-deepgram`, `remi-elevenlabs`, `remi-fluxions`, `remi-mistral`,
`remi-huggingface`, `remi-grok-cascade`, `remi-gradium`, `remi-vapi`, `remi-cartesia`, `remi-livekit`.

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

The bench injects `DATABASE_URL` (the collections twin's DSN), `FINERACT_API_BASE`
(the Fineract twin's host) and `VERIS_AGENT_CONTEXT`, and its bench-wide
candidate-env holds `FINERACT_USER`, `FINERACT_PASSWORD` and `FINERACT_TENANT`.
That is Remi's whole world: the debt-collection World has no Stripe twin (Stripe
does not serve debt collection agencies); payments are charged through the
payment gateway inside the collections database and posted to the account's
Fineract loan (utility-bench `collections_bench/WORLD.md`, "The payment gateway"
and "Money: the Fineract ledger"). No `STRIPE_*` value is needed on the bench; one
left over in candidate-env is ignored.

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
| `deepgram` | `deepgram-voice-agent` | `DEEPGRAM_API_KEY` | `VAMOS_DEEPGRAM_LLM_MODEL` (`gpt-4.1-mini`, as Rory "Deepgram"; also the default) |
| `elevenlabs` | `elevenlabs-convai` | `ELEVENLABS_API_KEY` | `VAMOS_ELEVENLABS_LLM` (`gpt-4.1-mini`, as Rory "ElevenLabs"; also the default) |
| `fluxions` | `fluxions-realtime` | `FLUXIONS_API_KEY` | optional `VAMOS_FLUXIONS_BASE_URL`, `VAMOS_FLUXIONS_VOICE`. No `public_endpoint`: nothing calls back (the Energy candidate's `public_endpoint: true` is unused by its code) |
| `mistral` | omit | `MISTRAL_API_KEY` | `VAMOS_MISTRAL_LLM_MODEL=mistral-medium-2604` (Energy's; the other models default to Energy's too) |
| `huggingface` | omit | `HF_TOKEN` | `VAMOS_HF_LLM_MODEL=openai/gpt-oss-120b:groq` (Energy's) |
| `grok_cascade` (GPT-4.1 mini) | omit | `XAI_API_KEY`, `OPENAI_API_KEY` | none needed (`VAMOS_GROK_CASCADE_LLM` defaults to `gpt-4.1-mini`) |
| `grok_cascade` (Grok 4.3) | omit | `XAI_API_KEY` | `VAMOS_GROK_CASCADE_LLM=grok-4.3` (same image as the row above) |
| `gradium` | `gradbot` | `GRADIUM_API_KEY`, `OPENAI_API_KEY` | `VAMOS_GRADIUM_LLM_MODEL=gpt-4.1-mini` (Energy's) |
| `vapi` | `vapi` | `VAPI_API_KEY` (private key) | `VAMOS_VAPI_MODEL` (`gpt-4.1-mini`, as Rory "Vapi"); **and `"public_endpoint": true` in the `image` block** |
| `cartesia` | `cartesia` | `CARTESIA_API_KEY` | optional `VAMOS_CARTESIA_MODEL` (`gpt-4.1`), `VAMOS_CARTESIA_VOICE_ID`; no public endpoint |
| `livekit` | omit | `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY` | optional `VAMOS_LIVEKIT_LLM_MODEL` (`gpt-4.1-mini`); no `LIVEKIT_*` (the image's own server) |

Every candidate (every row above) sets `"protocol": "json"` in its channel and
`VAMOS_VOICE_FRAMING=json` in its `image.env`, together (both, or neither): with `binary` an agent that ends the call is
recorded as `voice_ws_disconnect` and the attempt fails ungraded (section 3,
"Framing"). A mismatch breaks the call outright: each side ignores the other's
frames.

The Vapi candidate's `image` block also carries `"public_endpoint": true`
(not top level; `port` must not be 80), so the pod gets `PUBLIC_BASE_URL` and
Vapi can reach `/vendor/vapi/tool` (section 10, "Providers that are called
back"). The LiveKit image runs `livekit-server` as a second process, started by
the provider before `/health` turns ready; allow it a few seconds more readiness.

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
| Unauthenticated inbound webhooks / tunnels for hosted vendors | No tunnels. Only a provider's own `/vendor/<name>` routes, authenticated with a per-call secret and checked against the live call, dispatching through `CallSession`; Cartesia needs none (client tools on its socket). |
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
| Speaks first with a fixed `GREETING` ("Thanks for calling Acme Energy, this is Rory…"), triggered 15 ways | `call_start = agent_first(f'Greet the caller with exactly: "{GREETING}"')`; no screen pop (the caller is unknown until verified) |
| No per-call context; `RORY_REFERENCE_TIME` (ISO or epoch), frozen, falls back to wall time | `from_env` parses `RORY_REFERENCE_TIME` (required, no wall-time fallback); `call_time()` returns it; `CallClock` adds elapsed time; `AgentContext` stays empty |
| `verify_caller(account_number, name_on_account, second_factor)`; everything except `verify_caller`/`transfer_to_human` refused until `session.account` is set; 3 attempts | `GateLadder` with levels `CALLER < VERIFIED`; the two tools declare no `requires`, the other 14 declare `VERIFIED`; attempts counted on the call state |
| 16 tools: Stripe-only (bills, payment, extension, paperless, supplier), Fineract+Stripe (account, history, arrangements), none (transfer) | One `ToolRegistry` in `vamos_agents/rory/tools/`; `make_payment` declares its card fields in `redact` |
| Stripe via module globals; Fineract via `httpx` Basic auth + `Fineract-Platform-TenantId` (`FINERACT_API_BASE`, `_USER`, `_PASSWORD`, `_TENANT`) | `required_env = ("STRIPE_API_KEY", "FINERACT_API_BASE")`; `from_env` builds a `StripeClient` instance and a Fineract client; `connect()` checks both |
| Prompt `agent_desc.txt` + `today_context()` | `instructions(clock)`: the prompt plus the date from `clock` |
| Per-call `CallSession` with `account`, `failed_attempts`; no `end_call` | `open_call(clock)` returns that state; add an `end_call` tool so providers can hang up (or rely on the caller hanging up; `ended` stays False) |
| Vapi/PolyAI/Cartesia hosted vendors calling back over HTTP | `vapi` and `cartesia` providers exist (section 10); a callback is a provider route under `/vendor/<name>` with its own per-call authentication |

Migration path (optional, later): move `rory_tools/` into `vamos_agents/rory/` as the
pack above; port vendors one at a time into `vamos_voice/providers/` only where
the framework lacks them (Deepgram Agent, ElevenLabs...; Grok Voice and
GPT-Live already exist here); add `rory` to `AGENT_CASES`; build
`bench-e2e/rory-<vendor>` and switch candidates over by digest. Riley follows
the same path with its own world clients. Until then their images keep coming
from their own repos.

## 16. Testing

```bash
# LiveKit Agents needs openai<3 and the pipecat extra pins openai>=3, so the two extras are locked
# as separate forks ([tool.uv] conflicts) and the suite runs twice; --all-extras alone is refused.
uv run --all-extras --no-extra livekit pytest -q   # framework, parity matrix, every pack, every vendor but LiveKit
uv run --all-extras --no-extra pipecat --no-extra huggingface --no-extra grok-cascade pytest -q   # the same with LiveKit (those vendors skip)
uv run ruff check . && uv run ruff format --check .
```

- `tests/test_framework.py`: the boundary (no `agents` import, no wall clock),
  discovery, `AgentContext`, the gate engine and dispatch (refusals, redaction, never raises),
  the screen pop (appended, marks the state; a failure is logged and the call goes on).
- `tests/test_parity.py`: agents x vendors, on the frames each provider sends, including
  Remi's screen pop (reads before any vendor frame, section in the setup, no secrets, no
  forced tool choice, gates past the reads).
- `tests/test_grok_voice.py`, `tests/test_gpt_live.py`: each vendor's own
  frame translation (session shape, the screen pop in both GPT-Live instruction sets,
  barge-in, end of call), pinned where it differs from OpenAI Realtime.
- `tests/test_deepgram.py`, `tests/test_elevenlabs.py`, `tests/test_fluxions.py`: the managed
  voice agents' own protocol (Deepgram's `Settings` and audio held until `SettingsApplied`;
  ElevenLabs' per-call agent, always deleted, and its parameter descriptions; Fluxions' soul
  cap, full voice id, 16 kHz uplink and playback buffer), each with its hang-up timing.
- `tests/test_cascade.py`: the shared in-pod cascade with scripted legs (`tests/cascade_fakes.py`):
  callee first / agent first, rounds and tools, hang-up, barge-in (tools still run, history trimmed;
  `BargeInRule`: a blip does not interrupt, 0.5 s does, interim words required where the leg streams them),
  playout pacing; `SilenceEndpoint`; Silero on recorded speech (`tests/fixtures/caller_speech_24k.pcm`).
- `tests/test_mistral.py`, `tests/test_huggingface.py`, `tests/test_grok_cascade.py`, `tests/test_gradium.py`:
  each cascade's own legs (Voxtral frames and flush, Whisper/fal requests on `httpx.MockTransport`,
  xAI STT/TTS frames and `text.clear`, the streamed `openai` round; gradbot via a fake `gradbot.run`:
  re-issued tool calls answered once, the hang-up after the last words).
- `tests/test_realtime_turns.py`: the Realtime call flow (nothing but the session at call start,
  vendor-native turn taking, a request refused for an active response).
- `tests/test_smoke_inbound.py`: the real server with the fake inbound agent over `/voice`.
- `tests/test_hosted_vendors.py`: Vapi and Cartesia through scripted calls (fake socket,
  `httpx.MockTransport` for their REST APIs): the call held for the callee, Vapi's webhook
  refusing a wrong secret or call and dispatching through `CallSession`, Cartesia's client
  tools and reserved names, tool reuse, the agent deleted however the call ends; LiveKit's
  turn-taking mapping, loopback-only server, session assembly and tool handlers; provider
  routes mounted under `/vendor/<name>`.
- `tests/test_framing.py`: both framings, alone and through the real server (agent hang-up
  sends `end` before the 1000 close, a caller's `end` finalizes the call, binary unchanged),
  and Pipecat's serializer.
- `vamos_agents/remi/tests/`: Remi's rules, tools against a real Postgres
  (`docker run postgres:16-alpine`, or `REMI_TEST_DATABASE_URL`) loaded with
  the World's own SQL (schema, payment gateway, rows; vendored in
  `tests/fixtures/collections_world.sql`, resynced with `resync_world.sh`),
  the payment gateway client against the real gateway functions, the Fineract
  ledger client on the wire (and, opt-in, against a real Fineract:
  `REMI_TEST_FINERACT_URL`), `/voice` smoke test.

No test calls a paid API.

## 17. Hard rules

- `vamos_voice` never imports `agents.*`; providers see only the `CallRuntime`. Rules live in packs.
- One clock: `CallClock`. No wall time, no SQL `now()`.
- Card number, expiry, CVC and bank account numbers: only to the payment processor; declared `redact`; never logged, stored or returned.
- Every tool call goes through `CallSession.dispatch`; tools return `{"error": ...}`, never crash the call.
- Every dependency bounded; `uv.lock` committed; base images pinned. No secrets in code or images.
- One `Dockerfile`; one image per agent x vendor.
- No inbound endpoints beyond `/health`, `/voice` and a provider's own `/vendor/<name>` routes, each authenticated per call and dispatching only through `CallSession`.
- Vendor traffic via `wire.connect_websocket` or an instrumented SDK, so the judge's trace exists.
- Never import or copy a benchmark's grader into a pack.
