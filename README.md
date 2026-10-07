# Remi

Remi is the debt-collection voice agent, the candidate under test, for the
**VAmoS Debt Collection** benchmark on Veris Bench. It places outbound calls
for Tallmere Account Services, LLC to consumers in New York City, follows the
benchmark's collections policy (RULES.md), and records what happened in the
benchmark's world (a Postgres collections database and a Stripe twin).

One codebase, one Dockerfile, three voice stacks: OpenAI Realtime, Gemini Live,
and a Pipecat cascade (Deepgram → OpenAI → OpenAI TTS by default).

## Quickstart

```bash
uv sync --all-extras
uv run --all-extras pytest -q                      # needs docker for the Postgres tests
uv run ruff check . && uv run ruff format --check .

docker build --platform linux/amd64 --build-arg VENDOR=openai_realtime -t remi:openai_realtime .
docker run --rm -p 8008:8008 \
  -e VERIS_AGENT_CONTEXT='{"account_id":"TAS-420101","call_time":"2027-02-10T09:00:00-05:00","phone_id":"ph_420101_1"}' \
  -e DATABASE_URL=postgresql://... -e STRIPE_API_KEY=sk_test_... -e OPENAI_API_KEY=... \
  remi:openai_realtime
curl localhost:8008/health
```

`VENDOR` is one of `openai_realtime`, `gemini_live`, `pipecat`. The voice channel
is `ws://<host>:8008/voice` (PCM16, 24 kHz, mono, binary frames; the caller speaks first).

See **AGENTS.md** for the architecture, the config contract and how to register
Remi as a bench candidate.
