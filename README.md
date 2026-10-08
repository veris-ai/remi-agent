# vamos-agents

Every VAmoS voice agent in one repository: the candidates under test on Veris
Bench's VAmoS benchmarks. A shared framework, **`vamos_voice`**, owns the
server, the bench's `voice_ws` audio channel, call-start etiquette, tool
dispatch and gates, and the voice vendors. Each agent is a small **pack**
under `vamos_agents/` that supplies its prompt, tools, rules and world clients.

| Agent | Benchmark | Calls | Vendors |
|---|---|---|---|
| [Remi](vamos_agents/remi/README.md) | VAmoS Debt Collection | outbound (the callee speaks first) | OpenAI Realtime, Grok Voice, GPT-Live, Gemini Live, Pipecat (Deepgram → OpenAI → OpenAI TTS), Mistral (Voxtral cascade), Hugging Face (Whisper → gpt-oss → Kokoro), Grok STT + TTS cascade, Gradium (gradbot), Vapi, Cartesia, LiveKit |

## Quickstart

```bash
uv run --all-extras --no-extra livekit pytest -q   # Remi's tool tests need docker (Postgres 16)
uv run --all-extras --no-extra pipecat --no-extra huggingface --no-extra grok-cascade pytest -q   # LiveKit pins openai<3: separate forks
uv run ruff check . && uv run ruff format --check .

docker build --platform linux/amd64 --build-arg AGENT=remi --build-arg VENDOR=openai_realtime \
  -t remi-openai-realtime .
docker run --rm -p 8008:8008 \
  -e VERIS_AGENT_CONTEXT='{"account_id":"TAS-420101","call_time":"2027-02-10T09:00:00-05:00","phone_id":"ph_420101_1"}' \
  -e DATABASE_URL=postgresql://... -e OPENAI_API_KEY=... \
  remi-openai-realtime
curl localhost:8008/health
```

`AGENT` is a folder under `vamos_agents/`; `VENDOR` is one of `openai_realtime`,
`grok_voice` (xAI, key `XAI_API_KEY`), `gpt_live` (OpenAI GPT-Live),
`gemini_live`, `pipecat`, and the cascades `mistral` (`MISTRAL_API_KEY`), `huggingface`
(`HF_TOKEN`), `grok_cascade` (`XAI_API_KEY`, plus `OPENAI_API_KEY` for its default
`gpt-4.1-mini`) and `gradium` (`GRADIUM_API_KEY`, `OPENAI_API_KEY`). `vapi` and `cartesia`
(hosted orchestrators), `livekit` (LiveKit Agents; the image runs its own LiveKit server). Vendors differ in what they support (barge-in
truncation, VAD settings); what the agent knows when the call connects does
not: the agent's own screen pop puts it in every vendor's instructions
(AGENTS.md section 6). AGENTS.md section 10 lists the
differences and how each is handled. The voice channel is `ws://<host>:8008/voice`
(PCM16, 24 kHz, mono): raw binary frames by default, or base64 JSON envelopes
with an in-band `{"type": "end"}` hang-up when `VAMOS_VOICE_FRAMING=json`, which
bench candidates use (AGENTS.md section 3, "Framing").

See **[AGENTS.md](AGENTS.md)** for the architecture, the `AgentSpec` contract,
how to add an agent or a vendor, the config contract and how to register a
candidate on the bench.
