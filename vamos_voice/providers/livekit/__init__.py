"""The agent on LiveKit Agents: a cascaded pipeline (Deepgram STT -> OpenAI LLM -> ElevenLabs TTS) in a LiveKit room.

LiveKit is WebRTC end to end and cannot terminate the bench's ``voice_ws``
socket, so the image runs a **local LiveKit server** beside the Python
server (the Dockerfile adds the ``livekit-server`` binary for ``VENDOR=livekit``;
``warm_up`` starts it, waits for it, and ``/health`` stays 503 until then). Per
call the provider creates a room and joins it twice, in this process:

- as ``caller``: the bridge. Caller audio from the line is published as a
  microphone track; the agent's track is subscribed and its audio goes to the
  caller through ``call.outbound`` (the call-start gate);
- as ``agent``: a LiveKit ``AgentSession`` started on that room, with the
  session instructions (screen pop included) and the agent's tools as raw
  function tools that call ``call.session.dispatch``.

The session runs in-process rather than in a separate ``AgentServer`` worker,
so the tools dispatch through this call's ``CallSession`` like every other
vendor's, with no inter-process hop. The room is deleted at the end of the call.

Call start: a callee-first agent says nothing until the caller has spoken
(LiveKit answers user turns only); an agent that speaks first gets
``generate_reply(instructions=opening_instruction)`` once the session starts.
The callee-spoke signal is the first non-empty user transcript.

Turn taking is LiveKit's own: Silero VAD with the shared numbers (activation
threshold, prefix padding, end-of-turn silence) and endpointing ``min_delay``
at the same silence, no turn-detector model, as on the VAmoS Energy LiveKit
candidate. Interruptions are LiveKit's. Tool calls run one per step
(``parallel_tool_calls=False``: the tools share verification and payment
state) and a turn may chain up to 10 tool steps (LiveKit's default of 3 forces
a spoken reply mid-lookup).

Trace: no ``trace_adapter``. The LLM runs in this process through the
``openai`` SDK (the LiveKit OpenAI plugin), which the bench's OTel bundle
instruments, as for Pipecat.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Self
from urllib.parse import urlparse

import aiohttp
from livekit import api, rtc
from livekit.agents import Agent, AgentSession, RunContext
from livekit.agents.llm import StopResponse, function_tool
from livekit.agents.voice import room_io
from livekit.plugins import deepgram, elevenlabs, openai, silero

from vamos_voice.audio import CHANNEL_RATE_HZ
from vamos_voice.providers.base import CallRuntime, SessionSetup, TurnTaking, VoiceProvider
from vamos_voice.providers.wire import run_until_first_done
from vamos_voice.tools import ToolSpec

logger = logging.getLogger(__name__)

CALLER_IDENTITY = "caller"
AGENT_IDENTITY = "agent"
#: How long ``warm_up`` waits for the local LiveKit server to accept connections.
SERVER_START_TIMEOUT_S = 30.0
#: How long the bridge waits for the agent's audio track to appear in the room.
AGENT_TRACK_TIMEOUT_S = 30.0
#: Tool steps one agent turn may chain (LiveKit's default is 3).
MAX_TOOL_STEPS = 10
_LOCAL_HOSTS = {"localhost", "127.0.0.1"}
#: The agent session's state stream (``livekit.agents.voice.remote_session``).
SESSION_TOPIC = "lk.agent.session"


@dataclass(frozen=True)
class LiveKitConfig:
    """Keys, the room server and the pipeline (defaults: the VAmoS Energy LiveKit candidate)."""

    openai_api_key: str = field(repr=False)
    deepgram_api_key: str = field(repr=False)
    elevenlabs_api_key: str = field(repr=False)
    url: str = "ws://localhost:7880"
    #: Credentials of the room server. For the image's own server they are minted per process
    #: (nobody else needs them); set ``LIVEKIT_API_KEY``/``LIVEKIT_API_SECRET`` for another server.
    room_api_key: str = "vamos"
    room_api_secret: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    llm_model: str = "gpt-4.1-mini"
    stt_model: str = "nova-3-general"
    tts_voice_id: str = "EXAVITQu4vr4xnSDxMaL"
    tts_model: str = "eleven_flash_v2"
    server_binary: str = "livekit-server"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> LiveKitConfig:
        return cls(
            openai_api_key=env["OPENAI_API_KEY"],
            deepgram_api_key=env["DEEPGRAM_API_KEY"],
            elevenlabs_api_key=env["ELEVENLABS_API_KEY"],
            url=env.get("LIVEKIT_URL") or cls.url,
            room_api_key=env.get("LIVEKIT_API_KEY") or cls.room_api_key,
            room_api_secret=env.get("LIVEKIT_API_SECRET") or secrets.token_urlsafe(32),
            llm_model=env.get("VAMOS_LIVEKIT_LLM_MODEL") or cls.llm_model,
            stt_model=env.get("VAMOS_LIVEKIT_STT_MODEL") or cls.stt_model,
            tts_voice_id=env.get("VAMOS_LIVEKIT_TTS_VOICE_ID") or cls.tts_voice_id,
            tts_model=env.get("VAMOS_LIVEKIT_TTS_MODEL") or cls.tts_model,
            server_binary=env.get("VAMOS_LIVEKIT_SERVER_BINARY") or cls.server_binary,
        )

    @property
    def local_server(self) -> bool:
        """Is the room server this image's own (started by ``warm_up``)?"""
        return urlparse(self.url).hostname in _LOCAL_HOSTS

    @property
    def http_url(self) -> str:
        return self.url.replace("ws://", "http://", 1).replace("wss://", "https://", 1)


def vad_options(turns: TurnTaking) -> dict[str, float]:
    """The shared turn-taking numbers as Silero VAD options."""
    return {
        "min_silence_duration": turns.silence_ms / 1000,
        "prefix_padding_duration": turns.prefix_padding_ms / 1000,
        "activation_threshold": turns.vad_threshold,
    }


def turn_handling(turns: TurnTaking) -> dict[str, Any]:
    """Endpointing on silence alone: the effective end of turn is max(VAD silence, ``min_delay``)."""
    return {"endpointing": {"min_delay": turns.silence_ms / 1000}}


class LiveKitToolFormat:
    """Raw-schema function tools: the shared JSON schema verbatim, each handler dispatching
    through the call's ``CallSession``."""

    def __init__(self, call: CallRuntime, on_ended: asyncio.Event) -> None:
        self.call, self.on_ended = call, on_ended

    def convert(self, tools: list[ToolSpec]) -> list[Any]:
        return [self._tool(t) for t in tools]

    def _tool(self, spec: ToolSpec) -> Any:
        call, ended = self.call, self.on_ended

        async def handle(raw_arguments: dict[str, Any], context: RunContext) -> dict[str, Any]:
            # Shielded: a payment that has started must finish even if the caller barges in.
            result = await asyncio.shield(call.session.dispatch(spec.name, dict(raw_arguments)))
            if call.session.ended:
                await context.wait_for_playout()  # the words before the hang-up tool
                ended.set()
                raise StopResponse()  # no reply after the agent hung up
            return result

        return function_tool(
            handle,
            raw_schema={"name": spec.name, "description": spec.description, "parameters": spec.parameters},
        )


class LiveKitProvider(VoiceProvider):
    name = "livekit"
    trace_adapter = None
    required_env = ("OPENAI_API_KEY", "DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY")

    def __init__(self, config: LiveKitConfig) -> None:
        self.config = config
        self._server: asyncio.subprocess.Process | None = None
        self._vad: Any = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        return cls(LiveKitConfig.from_env(env))

    # --- the local room server --------------------------------------------------------------

    def server_command(self) -> list[str]:
        """``livekit-server`` in dev mode on loopback only: nothing outside the pod reaches it."""
        c = self.config
        port = urlparse(c.url).port or 7880
        return [
            c.server_binary,
            "--dev",
            "--bind",
            "127.0.0.1",
            "--node-ip",
            "127.0.0.1",
            "--port",
            str(port),
            "--keys",
            f"{c.room_api_key}: {c.room_api_secret}",
        ]

    async def warm_up(self) -> None:
        self._vad = await asyncio.to_thread(silero.VAD.load, **vad_options(TurnTaking()))
        if not self.config.local_server:
            return
        command = self.server_command()
        if shutil.which(command[0]) is None:
            raise RuntimeError(
                f"{command[0]} not found: the livekit image ships it (Dockerfile, VENDOR=livekit)"
            )
        self._server = await asyncio.create_subprocess_exec(*command)
        await _wait_for_server(self.config.http_url, self._server)
        logger.info("livekit-server ready at %s", self.config.url)

    async def close(self) -> None:
        if self._server is not None and self._server.returncode is None:
            self._server.terminate()
            await self._server.wait()

    # --- the pipeline ---------------------------------------------------------------------

    def build_session(self, setup: SessionSetup, http: aiohttp.ClientSession) -> AgentSession:
        c, turns = self.config, setup.turn_taking
        return AgentSession(
            # English-only bench: the STT language pinned (the TTS model, eleven_flash_v2, is English-only).
            stt=deepgram.STT(
                api_key=c.deepgram_api_key, model=c.stt_model, language="en-US", http_session=http
            ),
            llm=openai.LLM(api_key=c.openai_api_key, model=c.llm_model, parallel_tool_calls=False),
            tts=elevenlabs.TTS(
                api_key=c.elevenlabs_api_key, voice_id=c.tts_voice_id, model=c.tts_model, http_session=http
            ),
            vad=self._vad or silero.VAD.load(**vad_options(turns)),
            turn_handling=turn_handling(turns),
            max_tool_steps=MAX_TOOL_STEPS,
        )

    def build_agent(self, call: CallRuntime, ended: asyncio.Event) -> Agent:
        tools = LiveKitToolFormat(call, ended).convert(call.setup.tools)
        return Agent(instructions=call.setup.instructions, tools=tools)

    def token(self, room: str, identity: str) -> str:
        # The agent session publishes its state as participant attributes.
        grants = api.VideoGrants(
            room_join=True, room=room, can_publish=True, can_subscribe=True, can_update_own_metadata=True
        )
        return (
            api.AccessToken(self.config.room_api_key, self.config.room_api_secret)
            .with_identity(identity)
            .with_grants(grants)
            .to_jwt()
        )

    # --- the call --------------------------------------------------------------------------

    async def run_call(self, call: CallRuntime) -> None:
        room_name = f"vamos-{secrets.token_hex(6)}"
        caller, agent_room = rtc.Room(), rtc.Room()
        try:
            async with aiohttp.ClientSession() as http:
                await caller.connect(self.config.url, self.token(room_name, CALLER_IDENTITY))
                await agent_room.connect(self.config.url, self.token(room_name, AGENT_IDENTITY))
                await _LiveKitCall(self, call, caller, agent_room, http).run()
        finally:
            await asyncio.gather(caller.disconnect(), agent_room.disconnect(), return_exceptions=True)
            await self.delete_room(room_name)

    async def delete_room(self, room_name: str) -> None:
        try:
            async with api.LiveKitAPI(
                self.config.http_url, self.config.room_api_key, self.config.room_api_secret
            ) as lk:
                await lk.room.delete_room(api.DeleteRoomRequest(room=room_name))
        except Exception:
            logger.exception("livekit: room %s not deleted", room_name)


class _LiveKitCall:
    def __init__(
        self,
        provider: LiveKitProvider,
        call: CallRuntime,
        caller: rtc.Room,
        agent_room: rtc.Room,
        http: aiohttp.ClientSession,
    ) -> None:
        self.provider, self.call, self.caller, self.agent_room, self.http = (
            provider,
            call,
            caller,
            agent_room,
            http,
        )
        self.ended = asyncio.Event()
        self.agent_track: asyncio.Future[rtc.Track] = asyncio.get_running_loop().create_future()
        self.turn = 0

    async def run(self) -> None:
        call = self.call

        # The agent session streams its state and transcripts into the room for front ends; the
        # bridge carries audio only.
        self.caller.register_byte_stream_handler(SESSION_TOPIC, _discard_stream)

        @self.caller.on("track_subscribed")
        def _subscribed(track: rtc.Track, _pub: Any, participant: rtc.RemoteParticipant) -> None:
            if (
                participant.identity == AGENT_IDENTITY
                and track.kind == rtc.TrackKind.KIND_AUDIO
                and not self.agent_track.done()
            ):
                self.agent_track.set_result(track)

        source = rtc.AudioSource(CHANNEL_RATE_HZ, 1)
        mic = rtc.LocalAudioTrack.create_audio_track("caller-mic", source)
        await self.caller.local_participant.publish_track(
            mic, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )

        async def forward(pcm: bytes) -> None:
            await source.capture_frame(rtc.AudioFrame(pcm, CHANNEL_RATE_HZ, 1, len(pcm) // 2))

        session = self.provider.build_session(call.setup, self.http)

        @session.on("user_input_transcribed")
        def _transcribed(event: Any) -> None:
            if event.transcript.strip():
                call.start.mark_callee_spoke()

        @session.on("agent_state_changed")
        def _agent_state(event: Any) -> None:
            if event.new_state == "speaking":
                self.turn += 1

        await session.start(
            agent=self.provider.build_agent(call, self.ended),
            room=self.agent_room,
            room_options=room_io.RoomOptions(
                participant_identity=CALLER_IDENTITY,
                audio_input=room_io.AudioInputOptions(sample_rate=CHANNEL_RATE_HZ),
                audio_output=room_io.AudioOutputOptions(sample_rate=CHANNEL_RATE_HZ),
                text_output=False,
            ),
        )
        try:
            if call.setup.start_policy.agent_opens:
                session.generate_reply(instructions=call.setup.start_policy.opening_instruction)
            call.inbound.bind(forward)
            await run_until_first_done(
                caller_to_vendor=call.inbound.run(),
                vendor_to_caller=self.agent_audio(),
                agent_hang_up=self.hang_up_when_ended(),
            )
        finally:
            await session.aclose()

    async def agent_audio(self) -> None:
        track = await asyncio.wait_for(asyncio.shield(self.agent_track), AGENT_TRACK_TIMEOUT_S)
        stream = rtc.AudioStream(track, sample_rate=CHANNEL_RATE_HZ, num_channels=1)
        try:
            async for event in stream:
                await self.call.outbound.send(bytes(event.frame.data), f"turn{self.turn}")
        finally:
            await stream.aclose()

    async def hang_up_when_ended(self) -> None:
        await self.ended.wait()
        await self.call.hang_up()


def _discard_stream(_reader: Any, _participant_identity: str) -> None:
    """A room stream nobody on the call reads."""


async def _wait_for_server(http_url: str, process: asyncio.subprocess.Process) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SERVER_START_TIMEOUT_S
    async with aiohttp.ClientSession() as http:
        while loop.time() < deadline:
            if process.returncode is not None:
                raise RuntimeError(f"livekit-server exited with {process.returncode}")
            try:
                async with http.get(http_url, timeout=aiohttp.ClientTimeout(total=2)) as response:
                    if response.status < 500:
                        return
            except (aiohttp.ClientError, TimeoutError):
                logger.debug("livekit-server not answering yet")
            await asyncio.sleep(0.2)
    raise RuntimeError(f"livekit-server did not answer at {http_url} within {SERVER_START_TIMEOUT_S:.0f}s")


PROVIDER = LiveKitProvider
