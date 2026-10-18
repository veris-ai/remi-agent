# Remi Agent

Remi is a debt-collection voice agent built on many different voice stacks. Each
implementation gives Remi the same job, the same prompt and the same tools; only
the voice platform or speech model underneath changes. Remi is the agent under
test in Veris's **VAmoS Debt Collection** benchmark.

- **Results:** [80% of the money voice agents collected broke the creditor's rules](https://veris.ai/blog/vamos-debt-collection-policy-vs-money)
- **The exact code that was benchmarked:** tag
  [`v0.2.0`](https://github.com/veris-ai/remi-agent/tree/v0.2.0)
  (commit `3e14954`), run October 15 to 17, 2026. Later commits on `main` are not
  part of those results.

It is the sibling of [Rory Agent](https://github.com/veris-ai/rory-agent) (utility
billing) and [Riley Agent](https://github.com/veris-ai/riley-agent) (card
support). Where those agents take calls, **Remi places them**: it calls consumers
about charged-off accounts and has to collect the way the creditor allows.

> [!IMPORTANT]
> Remi is a simulation and reference project, not a production collections
> service. The agency (Tallmere Account Services), the creditors, the consumers
> and their accounts are synthetic. Review authentication, authorization,
> privacy, consumer-protection law and provider-cost controls before adapting
> any implementation for real consumers or data.

## What Remi does

Remi works for an invented third-party agency in New York City. On every call it:

1. Waits for whoever answers to speak first, identifies the agency and that it is
   an automated assistant, and asks for the account holder.
2. Verifies it is speaking to the right person before saying anything about the
   debt, then gives the required disclosures.
3. Handles what the consumer brings: a payment, a plan, a hardship, a dispute, a
   request to stop calling, a better time to call, a request for a person.
4. When the consumer is ready to pay, fetches the creditor's payment options and
   offers them by the creditor's policy:
   - payment in full first, then a plan, then a settlement only after the
     consumer describes a hardship;
   - never reveal the creditor's limits (biggest discount, lowest amount, longest
     term) before offering them;
   - never suggest a hardship, or say one would change the offer;
   - no fake "final offer" or deadline; get a yes to a plan's terms before
     creating it; request approval before an approval-tier settlement.
5. Records what happened: payments, plans, disputes, flags, callbacks and one
   contact record per call.

The policy is enforced twice: in the prompt (`vamos_agents/remi/prompt/`) and in
code, where gates refuse a tool called too early with a message saying what must
happen first (`gates.py`, `policy.py`).

## Implementations

Every row is the same Remi. `VENDOR` is the image build argument.

| `VENDOR` | Voice stack | Model that runs the conversation |
| --- | --- | --- |
| `gpt_live` | OpenAI GPT-Live: full-duplex live model, tool use delegated to a backend model | `gpt-live-1` (backend `gpt-5.6-terra`) |
| `openai_realtime` | OpenAI Realtime API: speech-to-speech | `gpt-realtime-2.1` or `gpt-realtime` |
| `gemini_live` | Gemini Live API: speech-to-speech | `gemini-3.1-flash-live-preview` or `gemini-3.8-live` |
| `grok_voice` | xAI Grok speech-to-speech | `grok-voice-think-fast-2.0` |
| `elevenlabs` | ElevenLabs Agents: hosted ASR, LLM and TTS | `gpt-4.1-mini` |
| `vapi` | Vapi: hosted orchestrator (Deepgram, LLM, ElevenLabs) | `gpt-4.1-mini` |
| `cartesia` | Cartesia managed agent: Ink STT, LLM, Sonic TTS | `gpt-4.1-mini` |
| `deepgram` | Deepgram Voice Agent API | `gpt-4.1-mini` |
| `livekit` | LiveKit Agents in a room on the image's own LiveKit server (Deepgram, LLM, ElevenLabs) | `gpt-4.1-mini` |
| `pipecat` | Pipecat: Deepgram STT, OpenAI LLM, OpenAI TTS | `gpt-4.1-mini` |
| `grok_cascade` | xAI Grok streaming STT and TTS around a chat model | `gpt-4.1-mini` or `grok-4.3` |
| `mistral` | Voxtral STT and TTS around a Mistral chat model | `mistral-medium-2604` |
| `huggingface` | Hugging Face Inference: Whisper, a router chat model, Kokoro | `openai/gpt-oss-120b` (via Groq) |
| `gradium` | gradbot: Gradium streaming STT and TTS around an OpenAI-compatible LLM | `gpt-4.1-mini` |
| `fluxions` | Fluxions voice-agent API: ASR, a vLLM-served chat model and VUI TTS in one realtime socket | the vendor's hosted model (not in the October 2026 run) |
| `silent` | A null agent that never speaks: the benchmark's zero baseline | none |

The model column shows what the benchmark ran; most are set by an environment
variable (see [AGENTS.md](AGENTS.md), "Config contract"). Each stack runs with its
vendor's own turn-taking, voice-activity detection and noise handling. Nothing
was tuned per stack.

## Repository layout

```
vamos_voice/        the shared framework: server, the voice_ws audio channel,
                    call start, tool dispatch and gates, and one adapter per vendor
  providers/<vendor>/
vamos_agents/remi/  Remi itself: prompt, tools, gates, policy, world clients, tests
AGENTS.md           architecture, the AgentSpec contract, adding a vendor, config
```

Each implementation is only a transport. The prompt, the tools, the gates and the
policy are shared, so every vendor gets the same agent. An image carries one
vendor: vendors install as extras, so an image ships only its own voice SDK.

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

Remi needs its world to do anything useful: a Postgres collections database (with
the payment gateway inside it) at `DATABASE_URL`, and a Fineract ledger at
`FINERACT_API_BASE`. In the benchmark, Veris provides both as a simulated copy of
the agency's systems, reset for every call. The voice channel is
`ws://<host>:8008/voice` (PCM16, 24 kHz, mono). Vendor keys, framing and every
other setting are in [AGENTS.md](AGENTS.md).

## Contributing

Issues and pull requests are welcome, including new voice-stack implementations.
A new vendor is a folder under `vamos_voice/providers/`; AGENTS.md explains the
contract. Changes on `main` don't alter the published results, which are pinned
to the tag above.

## License

[MIT](LICENSE)
