"""The agent on the Fluxions voice-agent API (``wss://<host>/v1/realtime``), bring-your-own tools.

One socket carries Fluxions' ASR, end-of-utterance model, a vLLM-served chat
model and VUI TTS. Bench candidate: ``trace_adapter: fluxions-realtime``,
which reads ``session.update`` (``soul``, ``tools``), ``session.created``,
``user.transcript``, ``agent.sentence``, ``tool.call``, ``tool.result`` and
``agent.done`` off the ``websockets`` client this provider uses.

The protocol (``<base>/docs/agent-tools-api.md``, and the VAmoS Energy
Fluxions candidate):

- the key goes in the handshake's ``Authorization`` header, bare (no
  ``Bearer``; the gateway answers ``human verification required`` to a
  ``Bearer`` or to no key at all);
- the first message is ``session.update``: ``soul`` (the system prompt,
  connect-only), ``voice`` (the full ``voice_id`` from ``GET /voices``: the
  host silently closes a session that names a voice by its short id), ``tools`` (flat ``{name, description,
  parameters}``) and the session knobs. ``session.created`` echoes the soul as
  the host kept it, the registered ``client_tools`` and anything ``denied``;
- audio is mixed on the socket: binary frames, PCM16 mono, **16 kHz up** (the
  channel's 24 kHz is resampled) and **24 kHz down** (forwarded as is);
- ``tool.call`` (``call_id``, ``name``, ``arguments``) is answered with
  ``tool.result`` (``call_id``, ``output`` as a string) within 12 s.

The host caps the soul (``GET /config`` ``max_soul_chars``; 4,000 on
``api.fluxions.ai``) and truncates a longer one **silently**. Instructions are
never shortened here: ``warm_up`` reads the cap, a call whose instructions
exceed it fails before connecting, and ``session.created`` is checked for a
truncated soul or a rejected tool.

**Playback is the client's job on this API.** Fluxions sends speech about 6x
faster than real time and, on barge-in, ``audio.flush``: "drop queued playback
immediately" (the docs call it not optional). So this provider is the playback
buffer the vendor expects: agent audio is queued and released to the caller no
more than ``PLAYBACK_LEAD_S`` ahead of real time, ``audio.flush`` drops the
queue, and ``playback.pos`` reports every ``PLAYBACK_POS_S`` how much the
caller has heard (what Fluxions times its barge-in against). Nothing about the
caller's audio is gated or processed: turn taking, VAD and barge-in are
Fluxions' own (an adaptive end-of-utterance model with no knob, so
``TurnTaking`` is not mapped).

Session knobs (as on VAmoS Energy): ``greet`` only for an agent that speaks
first (below), ``allow_hangup: false`` (the agent's ``end_call``
tool is the one hang-up path, so the call always finalizes the same way), the
idle check-in and idle hang-up pushed out to an hour (no other vendor prompts
or hangs up on a quiet line), ``echo_guard: false`` (injected PCM has no
acoustic loopback).

Call start: ``CALLEE_FIRST`` sends ``greet: false`` and asks nothing of the
model; the first non-empty ``user.transcript`` opens the outbound gate.
``AGENT_FIRST``: ``greet: true``, Fluxions' own opening turn, generated from the
soul (which carries the agent's greeting). The socket has no message that asks
the model for a turn (``user.text`` is ignored on ``/v1/realtime``), so the
policy's ``opening_instruction`` itself is not sent: a declared difference.

Hang-up: once ``end_call`` has run, the line closes when the agent's turn is
over (``agent.done``) and its queued audio has played.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import Any, Self

import httpx
import websockets

from vamos_voice.audio import CHANNEL_RATE_HZ, Resampler, pcm_ms
from vamos_voice.providers.base import CallRuntime, SessionSetup, VoiceProvider
from vamos_voice.providers.wire import Connect, connect_websocket, run_until_first_done
from vamos_voice.tools import ToolSpec

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.fluxions.ai/vui"
DEFAULT_VOICE = "maeve"
MIC_RATE_HZ = 16_000
HTTP_TIMEOUT_S = 30.0
#: How far ahead of real time agent audio may be released to the caller.
PLAYBACK_LEAD_S = 1.0
#: How often Fluxions is told how much the caller has heard.
PLAYBACK_POS_S = 0.25
#: A session that never prompts or hangs up on a quiet caller (seconds).
IDLE_S = 3600
AUDIO_ITEM = "fluxions"


class FluxionsToolFormat:
    """``session.update`` ``tools``: flat ``{name, description, parameters}``."""

    def convert(self, tools: list[ToolSpec]) -> list[dict[str, Any]]:
        return [{"name": t.name, "description": t.description, "parameters": t.parameters} for t in tools]


class FluxionsProvider(VoiceProvider):
    name = "fluxions"
    trace_adapter = "fluxions-realtime"
    required_env = ("FLUXIONS_API_KEY",)
    tool_format = FluxionsToolFormat()
    connect: Connect = staticmethod(connect_websocket)

    def __init__(self, api_key: str, base_url: str, voice: str) -> None:
        self._api_key = api_key
        self.base_url, self.voice = base_url.rstrip("/"), voice
        #: The host's soul cap, read by ``warm_up`` (None: not read, nothing checked before connecting).
        self.max_soul_chars: int | None = None
        #: The voice's full ``voice_id`` (``maeve`` -> ``maeve.<hash>``), resolved by ``warm_up``: the
        #: host closes a session at once (1000, no error) when ``session.update`` names the short id.
        self.voice_id = voice

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        base_url = env.get("VAMOS_FLUXIONS_BASE_URL") or DEFAULT_BASE_URL
        if not base_url.startswith("https://"):
            raise ValueError(f"VAMOS_FLUXIONS_BASE_URL must be an https:// URL, got {base_url!r}")
        return cls(
            api_key=env["FLUXIONS_API_KEY"],
            base_url=base_url,
            voice=env.get("VAMOS_FLUXIONS_VOICE") or DEFAULT_VOICE,
        )

    @property
    def url(self) -> str:
        return "wss://" + self.base_url.removeprefix("https://") + "/v1/realtime"

    async def warm_up(self) -> None:
        """Read the host's soul cap and resolve the voice's full id, before /health says ready."""
        async with httpx.AsyncClient(
            base_url=self.base_url, headers={"Authorization": self._api_key}, timeout=HTTP_TIMEOUT_S
        ) as http:
            config = (await http.get("/config")).raise_for_status().json()
            voices = (await http.get("/voices")).raise_for_status().json().get("voices", [])
        self.max_soul_chars = int(config["max_soul_chars"])
        matches = [v["voice_id"] for v in voices if self.voice in (v.get("id"), v.get("voice_id"))]
        if not matches:
            raise RuntimeError(f"fluxions voice {self.voice!r} is not on {self.base_url}")
        self.voice_id = matches[0]
        logger.info(
            "fluxions: host %s model=%s voice=%s max_soul_chars=%d",
            self.base_url,
            config.get("model"),
            self.voice_id,
            self.max_soul_chars,
        )

    def session_update(self, setup: SessionSetup) -> dict[str, Any]:
        """The first message: the whole session."""
        return {
            "type": "session.update",
            "soul": setup.instructions,
            "voice": self.voice_id,
            "tools": self.tool_format.convert(setup.tools),
            "greet": setup.start_policy.agent_opens,
            "allow_hangup": False,
            "idle_check_s": IDLE_S,
            "idle_hangup_s": IDLE_S,
            "echo_guard": False,
        }

    async def run_call(self, call: CallRuntime) -> None:
        soul = len(call.setup.instructions)
        if self.max_soul_chars is not None and soul > self.max_soul_chars:
            raise RuntimeError(
                f"the instructions are {soul} characters and {self.base_url} keeps only "
                f"{self.max_soul_chars} of the soul: the agent would run on a truncated prompt"
            )
        async with self.connect(self.url, {"Authorization": self._api_key}) as ws:
            await _FluxionsCall(self, ws, call).run()


class _FluxionsCall:
    """One call's realtime session: events, tool calls and the playback buffer."""

    def __init__(self, provider: FluxionsProvider, ws: Any, call: CallRuntime) -> None:
        self.provider, self.ws, self.call = provider, ws, call
        self.resampler = Resampler(CHANNEL_RATE_HZ, MIC_RATE_HZ)
        self.playback: asyncio.Queue[bytes] = asyncio.Queue()
        self.flushes = 0
        self.released_s = 0.0  # Fluxions audio handed to the caller
        self.play_until = 0.0  # loop time the caller's buffer runs dry
        self.turn_active = False
        self.hanging_up = False

    def now(self) -> float:
        return asyncio.get_running_loop().time()

    async def send(self, event: dict[str, Any]) -> None:
        await self.ws.send(json.dumps(event))

    async def recv(self) -> str | bytes | None:
        try:
            return await self.ws.recv()
        except websockets.ConnectionClosed as closed:
            logger.info("fluxions socket closed: %s", closed)
            return None

    async def run(self) -> None:
        call = self.call
        update = self.provider.session_update(call.setup)
        await self.send(update)
        while (message := await self.recv()) is not None:
            if isinstance(message, bytes):
                continue
            event = json.loads(message)
            if event.get("type") == "session.created":
                self.check_session(event, update)
                break
            if event.get("type") == "error":
                raise RuntimeError(f"fluxions error before session.created: {event}")
        if message is None:
            raise RuntimeError("Fluxions closed the session before session.created")

        async def forward(pcm: bytes) -> None:
            data = self.resampler.process(pcm)
            if data:
                await self.ws.send(data)

        call.inbound.bind(forward)
        await run_until_first_done(
            caller_to_vendor=call.inbound.run(),
            vendor_to_caller=self.events(),
            playback=self.release(),
            playback_pos=self.report_position(),
        )

    @staticmethod
    def check_session(event: dict[str, Any], update: dict[str, Any]) -> None:
        """Fail the call rather than run it on less than was sent."""
        rates = (event.get("mic_sample_rate"), event.get("audio_sample_rate"))
        if rates != (MIC_RATE_HZ, CHANNEL_RATE_HZ):
            raise RuntimeError(f"fluxions session rates {rates}, expected ({MIC_RATE_HZ}, {CHANNEL_RATE_HZ})")
        problems = []
        if event.get("denied"):
            problems.append(f"denied {event['denied']}")
        if event.get("client_tools_rejected"):
            problems.append(f"rejected tools {event['client_tools_rejected']}")
        if isinstance(event.get("soul"), str) and len(event["soul"]) < len(update["soul"]):
            problems.append(f"soul truncated to {len(event['soul'])} of {len(update['soul'])} characters")
        missing = {t["name"] for t in update["tools"]} - set(event.get("client_tools") or [])
        if missing:
            problems.append(f"tools not registered {sorted(missing)}")
        if problems:
            raise RuntimeError("fluxions session differs from the one sent: " + "; ".join(problems))
        logger.info("fluxions: session %s model=%s", event.get("session_id"), event.get("model"))

    async def events(self) -> None:
        while (message := await self.recv()) is not None:
            if isinstance(message, bytes):
                self.turn_active = True
                self.playback.put_nowait(message)
                continue
            if await self.on_event(json.loads(message)):
                return

    async def on_event(self, event: dict[str, Any]) -> bool:
        """Handle one server event; True when the call is over."""
        kind = event.get("type", "")
        call = self.call
        if kind == "user.transcript":
            if (event.get("text") or "").strip():
                call.start.mark_callee_spoke()
        elif kind == "agent.sentence":
            self.turn_active = True
        elif kind == "audio.flush":
            self.flush()
        elif kind == "tool.call":
            await self.run_tool(event)
            if call.session.ended:
                self.hanging_up = True
                if not self.turn_active:
                    return await self.hang_up()
        elif kind == "agent.done":
            self.turn_active = False
            if self.hanging_up:
                return await self.hang_up()
        elif kind == "agent.hangup":
            logger.info("fluxions: the vendor ended the call")
            return True
        elif kind == "error":
            raise RuntimeError(f"fluxions error: {event}")
        return False

    async def run_tool(self, event: dict[str, Any]) -> None:
        arguments = event.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except json.JSONDecodeError:
                arguments = None
        if isinstance(arguments, dict):
            result = await self.call.session.dispatch(event["name"], arguments)
        else:
            result = {"error": "the arguments were not a JSON object; call the tool again"}
        await self.send(
            {"type": "tool.result", "call_id": event["call_id"], "output": json.dumps(result, default=str)}
        )

    # --- playback: the buffer the vendor expects the client to be -----------------------------

    def flush(self) -> None:
        """Barge-in: drop what the caller has not been sent yet."""
        while not self.playback.empty():
            self.playback.get_nowait()
        self.flushes += 1
        self.call.outbound.interrupted()

    async def release(self) -> None:
        """Queue to caller, never more than ``PLAYBACK_LEAD_S`` ahead of real time. A chunk taken
        before a flush and due after it belongs to the cancelled reply and is dropped."""
        while True:
            chunk = await self.playback.get()
            flushes = self.flushes
            ahead = self.play_until - self.now()
            if ahead > PLAYBACK_LEAD_S:
                await asyncio.sleep(ahead - PLAYBACK_LEAD_S)
                if self.flushes != flushes:
                    continue
            if await self.call.outbound.send(chunk, AUDIO_ITEM):
                seconds = pcm_ms(len(chunk)) / 1000
                self.play_until = max(self.now(), self.play_until) + seconds
                self.released_s += seconds

    def played_s(self) -> float:
        return max(0.0, self.released_s - max(0.0, self.play_until - self.now()))

    async def report_position(self) -> None:
        reported = None
        while True:
            await asyncio.sleep(PLAYBACK_POS_S)
            played = round(self.played_s(), 3)
            if played != reported:
                reported = played
                await self.send({"type": "playback.pos", "played_s": played})

    async def hang_up(self) -> bool:
        """Hand the caller whatever is still queued (its playout is waited for), then hang up."""
        while not self.playback.empty():
            await self.call.outbound.send(self.playback.get_nowait(), AUDIO_ITEM)
        await self.call.hang_up()
        return True


PROVIDER = FluxionsProvider
