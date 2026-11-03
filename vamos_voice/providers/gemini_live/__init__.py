"""The agent on the Gemini Live API (speech-to-speech), Gemini Developer API.

Raw protocol over the ``websockets`` asyncio client, camelCase JSON, which is
exactly what the bench's ``gemini-live`` trace adapter reads off ``send`` and
``recv``. The Developer API (not Vertex AI) is required: only there do
function calls carry an ``id`` the trace can match each tool result to.

Language: every VAmoS call is in English. The caller's transcription is
pinned with ``inputAudioTranscription.languageCodes: ["en"]`` (the Live API's
code for English). The language the model speaks has no setting: native-audio
models (every Live model this provider runs, Gemini 2.5 Flash native audio and
the Gemini 3 Live models) "don't support explicitly setting the language code",
so the prompt holds it.

Audio: the channel's 24 kHz PCM16 is resampled to Gemini's native 16 kHz on
the way in; Gemini's output is 24 kHz, forwarded as is.

Call start: the agent's screen pop is already in ``systemInstruction``, so
the model knows the account before it hears anything; caller audio flows
right after ``setupComplete``. Audio the model produces before the callee
speaks is dropped by the shared ``OutboundAudio`` gate. For an agent that
speaks first, the opening is requested with a text turn carrying the policy's
``opening_note`` (labelled as a call-system note so the judge never mistakes
it for the caller): the Live API has no other way to ask for a turn.

Tools: each ``toolCall``'s function calls run in order through dispatch as it
arrives, and one ``toolResponse`` carries their true results, before the next
server message is read. When caller speech interrupts a turn whose calls are
pending, Gemini sends ``toolCallCancellation`` with their ids (the calls
"should not have been executed"; clients "may attempt to undo" side effects).
By then they have run and been answered, so the provider only logs a warning:
each id, its tool name and whether it returned an error (never arguments or
results). It undoes, skips and sends nothing: the Live API documents no
response to a cancellation and no way to add context that neither starts nor
interrupts generation (``clientContent`` "will interrupt any current model
generation").
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Mapping
from typing import Any, Self

import websockets

from vamos_voice.audio import CHANNEL_RATE_HZ, Resampler
from vamos_voice.providers.base import CallRuntime, SessionSetup, VoiceProvider
from vamos_voice.providers.wire import Connect, connect_websocket, run_until_first_done
from vamos_voice.tools import ToolSpec

logger = logging.getLogger(__name__)

ENDPOINT = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)
DEFAULT_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
DEFAULT_VOICE = "Puck"
#: The caller transcription's language (BCP-47, the Live API's code for English).
LANGUAGE_CODES = ["en"]
INPUT_RATE_HZ = 16_000

#: The schema keywords Gemini's function-declaration Schema accepts.
_SCHEMA_KEYS = {
    "type",
    "description",
    "enum",
    "properties",
    "required",
    "items",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "pattern",
}


class GeminiToolFormat:
    """``tools: [{"functionDeclarations": [...]}]`` with schemas cut to Gemini's subset."""

    def convert(self, tools: list[ToolSpec]) -> list[dict[str, Any]]:
        return [
            {
                "functionDeclarations": [
                    {"name": t.name, "description": t.description, "parameters": _gemini_schema(t.parameters)}
                    for t in tools
                ]
            }
        ]


def _gemini_schema(node: Any) -> Any:
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "properties":
            out[key] = {name: _gemini_schema(sub) for name, sub in value.items()}
        elif key == "items":
            out[key] = _gemini_schema(value)
        elif key == "exclusiveMinimum":
            out["minimum"] = value + 1 if node.get("type") == "integer" else value
        elif key in _SCHEMA_KEYS:
            out[key] = value
    return out


class GeminiLiveProvider(VoiceProvider):
    name = "gemini_live"
    trace_adapter = "gemini-live"
    # The standard name, as on the other VAmoS benches; Veris Bench allows this exact
    # candidate-env key (other GEMINI_* names stay reserved for its harnesses).
    required_env = ("GEMINI_API_KEY",)
    tool_format = GeminiToolFormat()
    connect: Connect = staticmethod(connect_websocket)

    def __init__(self, api_key: str, model: str, voice: str) -> None:
        self._api_key = api_key
        self.model, self.voice = model, voice

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        return cls(
            api_key=env["GEMINI_API_KEY"],
            model=env.get("VAMOS_GEMINI_MODEL") or DEFAULT_MODEL,
            voice=env.get("VAMOS_GEMINI_VOICE") or DEFAULT_VOICE,
        )

    def setup_message(self, setup: SessionSetup) -> dict[str, Any]:
        return {
            "setup": {
                "model": f"models/{self.model}",
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self.voice}}},
                },
                "systemInstruction": {"parts": [{"text": setup.instructions}]},
                "tools": self.tool_format.convert(setup.tools),
                # Gemini's own automatic activity detection, on its default sensitivities and timings.
                "realtimeInputConfig": {
                    "automaticActivityDetection": {"disabled": False},
                    "activityHandling": "START_OF_ACTIVITY_INTERRUPTS",
                },
                "inputAudioTranscription": {"languageCodes": LANGUAGE_CODES},
                "outputAudioTranscription": {},
            }
        }

    async def run_call(self, call: CallRuntime) -> None:
        async with self.connect(ENDPOINT, {"x-goog-api-key": self._api_key}) as ws:
            await _GeminiCall(self, ws, call).run()


def _note_turn(text: str) -> dict[str, Any]:
    return {"clientContent": {"turns": [{"role": "user", "parts": [{"text": text}]}], "turnComplete": True}}


class _GeminiCall:
    def __init__(self, provider: GeminiLiveProvider, ws: Any, call: CallRuntime) -> None:
        self.provider, self.ws, self.call = provider, ws, call
        self.turn = 0
        self.resampler = Resampler(CHANNEL_RATE_HZ, INPUT_RATE_HZ)
        #: Every function call this call ran, by id: (tool name, whether it returned an error).
        self.ran: dict[str | None, tuple[str, bool]] = {}

    async def send(self, message: dict[str, Any]) -> None:
        await self.ws.send(json.dumps(message))

    async def recv(self) -> dict[str, Any] | None:
        try:
            raw = await self.ws.recv()
        except websockets.ConnectionClosed as closed:
            logger.info("gemini socket closed: %s", closed)
            return None
        return json.loads(raw)

    async def run(self) -> None:
        call = self.call
        await self.send(self.provider.setup_message(call.setup))
        while (message := await self.recv()) is not None and "setupComplete" not in message:
            logger.debug("gemini: before setupComplete: %s", list(message))
        if message is None:
            raise RuntimeError("Gemini closed the session before setupComplete")

        async def forward(pcm: bytes) -> None:
            data = self.resampler.process(pcm)
            if data:
                await self.send(
                    {
                        "realtimeInput": {
                            "audio": {
                                "data": base64.b64encode(data).decode(),
                                "mimeType": f"audio/pcm;rate={INPUT_RATE_HZ}",
                            }
                        }
                    }
                )

        call.inbound.bind(forward)
        if call.setup.start_policy.agent_opens:
            await self.send(_note_turn(call.setup.start_policy.opening_note))
        await run_until_first_done(caller_to_vendor=call.inbound.run(), vendor_to_caller=self.events())

    async def events(self) -> None:
        while (message := await self.recv()) is not None:
            if await self.on_message(message):
                return

    async def on_message(self, message: dict[str, Any]) -> bool:
        """Handle one server message; True when the call is over."""
        call = self.call
        content = message.get("serverContent") or {}
        if (content.get("inputTranscription") or {}).get("text", "").strip():
            call.start.mark_callee_spoke()
        for part in (content.get("modelTurn") or {}).get("parts", []):
            blob = part.get("inlineData") or {}
            if blob.get("data"):
                await call.outbound.send(base64.b64decode(blob["data"]), f"turn{self.turn}")
        if content.get("interrupted"):
            call.outbound.interrupted()
        if "toolCall" in message:
            await self.run_tools(message["toolCall"].get("functionCalls", []))
            if call.session.ended:
                await call.hang_up()
                return True
        if content.get("turnComplete"):
            self.turn += 1
            if call.session.ended:
                await call.hang_up()
                return True
        if "toolCallCancellation" in message:
            self.log_cancellation(message["toolCallCancellation"].get("ids") or [])
        if "goAway" in message:
            logger.warning("gemini: goAway %s", message["goAway"])
        return False

    async def run_tools(self, calls: list[dict[str, Any]]) -> None:
        responses = []
        for fc in calls:
            result = await self.call.session.dispatch(fc["name"], fc.get("args") or {})
            self.ran[fc.get("id")] = (fc["name"], "error" in result)
            responses.append({"id": fc.get("id"), "name": fc["name"], "response": result})
        await self.send({"toolResponse": {"functionResponses": responses}})

    def log_cancellation(self, ids: list[str]) -> None:
        """Gemini cancelled tool calls (caller speech interrupted their turn): say which, and what ran.

        Tool names and outcomes only, never arguments or results (they can hold card data). Nothing
        is undone, skipped or sent: a call that ran has already had its true ``toolResponse``.
        """
        cancelled = []
        for call_id in ids:
            if call_id in self.ran:
                name, failed = self.ran[call_id]
                outcome = "with an error" if failed else "with a result"
                cancelled.append(f"{call_id} ({name}: already run and answered {outcome})")
            else:
                cancelled.append(f"{call_id} (not run by this call)")
        logger.warning("gemini: toolCallCancellation for %s", ", ".join(cancelled) or "no ids")


PROVIDER = GeminiLiveProvider
