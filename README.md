# vamos-agents

Every VAmoS voice agent in one repository: the candidates under test on Veris
Bench's VAmoS benchmarks. A shared framework, **`vamos_voice`**, owns the
server, the bench's `voice_ws` audio channel, call-start etiquette, tool
dispatch and gates, and the voice vendors. Each agent is a small **pack**
under `vamos_agents/` that supplies its prompt, tools, rules and world clients.

| Agent | Benchmark | Calls | Vendors |
|---|---|---|---|
| [Remi](vamos_agents/remi/README.md) | VAmoS Debt Collection | outbound (the callee speaks first) | OpenAI Realtime, Grok Voice, GPT-Live, Gemini Live, Pipecat (Deepgram → OpenAI → OpenAI TTS) |

## Quickstart

```bash
uv sync --all-extras
uv run --all-extras pytest -q                      # Remi's tool tests need docker (Postgres 16)
uv run ruff check . && uv run ruff format --check .

docker build --platform linux/amd64 --build-arg AGENT=remi --build-arg VENDOR=openai_realtime \
  -t remi-openai-realtime .
docker run --rm -p 8008:8008 \
  -e VERIS_AGENT_CONTEXT='{"account_id":"TAS-420101","call_time":"2027-02-10T09:00:00-05:00","phone_id":"ph_420101_1"}' \
  -e DATABASE_URL=postgresql://... -e STRIPE_API_KEY=sk_test_... -e OPENAI_API_KEY=... \
  remi-openai-realtime
curl localhost:8008/health
```

`AGENT` is a folder under `vamos_agents/`; `VENDOR` is one of `openai_realtime`,
`grok_voice` (xAI, key `XAI_API_KEY`), `gpt_live` (OpenAI GPT-Live),
`gemini_live`, `pipecat`. Vendors differ in what they support (forced tool
choice, barge-in truncation, VAD settings); AGENTS.md section 10 lists the
differences and how each is handled. The voice channel is `ws://<host>:8008/voice`
(PCM16, 24 kHz, mono, binary frames).

See **[AGENTS.md](AGENTS.md)** for the architecture, the `AgentSpec` contract,
how to add an agent or a vendor, the config contract and how to register a
candidate on the bench.
