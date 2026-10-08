"""The agent on gradbot: Gradium streaming STT and TTS around an OpenAI-compatible LLM, in gradbot's engine.

gradbot (Gradium's open-source voice-agent engine, a Rust core behind
``gradbot.run``) runs the three legs concurrently inside this process and owns
turn taking, barge-in and the conversation history, so unlike the other
cascades nothing of ``cascade.py`` applies: the provider bridges the caller's
line to gradbot's handles and answers its tool calls, as the VAmoS Energy
gradbot candidate does (rory-agent ``gradium/``).

- **Session**: ``SessionConfig(instructions=setup.instructions, tools=ToolDef...,
  voice_id, language=En, silence_timeout_s=0)``. gradbot wraps the
  instructions in its own scaffold (speaking style, interruptions) and adds an
  internal ``reset_asr`` tool it handles itself. ``silence_timeout_s=0`` turns
  off gradbot's nudging and hanging up on a quiet caller: no other candidate
  does that, so it stays off, as on Energy.
- **Call start**: ``assistant_speaks_first`` is the policy's ``agent_opens``.
  Callee first, gradbot waits for the caller's first turn; the callee-spoke
  signal is gradbot's first ``stt_text``. Agent first, gradbot opens with a
  literal ``[start]`` user message, and the only place to say what the opening
  should do is the instructions: the policy's ``opening_instruction`` is
  appended to them (as OpenAI's opening ``response.create`` does).
- **Turns and barge-in** are gradbot's: endpointing (a flush of silence through
  the recogniser once transcripts stop), interruption (it stops emitting audio
  and keeps only the words that were transmitted in its history). The
  provider only marks the playout interrupted on gradbot's ``interrupted`` event.
- **Audio**: caller PCM16 24 kHz goes in untouched; gradbot's PCM out is 48 kHz,
  resampled to 24 kHz.
- **Tools**: every call through ``CallSession.dispatch``, one at a time, each
  in its own task (as gradbot's reference server), so a re-issued call is
  read at once. gradbot re-prompts the model a second or two into a pending
  tool call and the model often issues the same call again before the first
  result reaches it; ``ToolCalls`` answers such a re-issue with the earlier
  result instead of running the tool twice (the Energy candidate's rule).
- **Hang-up**: when a tool ends the call, the agent's last words play out
  (until gradbot's ``end_tts_audio``, or ``QUIET_S`` without audio), then
  ``call.hang_up()``.

Trace: ``trace_adapter = "gradbot"``. The model is called from Rust, so no SDK
span exists; the bench's gradbot adapter wraps ``gradbot.run`` and reads the
session's messages and tool answers off the handles. So ``gradbot.run`` is
called by attribute, with keywords only, at call time.

Config: ``GRADIUM_API_KEY`` (Gradium STT and TTS) and ``OPENAI_API_KEY`` (the
LLM), both required; ``VAMOS_GRADIUM_LLM_MODEL`` (default ``gpt-4.1-mini``,
the Energy candidate's), ``VAMOS_GRADIUM_VOICE_ID`` (default
``4SZHfMpw-p46Ywgs``, Gradium's "Harper").
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Self

import gradbot

from vamos_voice.audio import CHANNEL_RATE_HZ, Resampler
from vamos_voice.providers.base import CallRuntime, SessionSetup, VoiceProvider
from vamos_voice.providers.wire import run_until_first_done
from vamos_voice.session import CallSession
from vamos_voice.tools import ToolSpec

logger = logging.getLogger(__name__)

GRADBOT_OUTPUT_RATE_HZ = 48_000
#: After the agent ends the call: hang up once gradbot has sent no audio for this long.
QUIET_S = 1.0


class GradbotToolFormat:
    """``gradbot.ToolDef(name, description, parameters_json)``."""

    def convert(self, tools: list[ToolSpec]) -> list[Any]:
        return [gradbot.ToolDef(t.name, t.description, json.dumps(t.parameters)) for t in tools]


@dataclass(frozen=True)
class GradiumConfig:
    gradium_api_key: str = field(repr=False)
    llm_api_key: str = field(repr=False)
    llm_model: str = "gpt-4.1-mini"
    voice_id: str = "4SZHfMpw-p46Ywgs"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> GradiumConfig:
        return cls(
            gradium_api_key=env["GRADIUM_API_KEY"],
            llm_api_key=env["OPENAI_API_KEY"],
            llm_model=env.get("VAMOS_GRADIUM_LLM_MODEL") or cls.llm_model,
            voice_id=env.get("VAMOS_GRADIUM_VOICE_ID") or cls.voice_id,
        )


@dataclass
class _Issued:
    """A dispatched call, and how many prompts gradbot had built when its result went back."""

    result: asyncio.Future[str]
    answered_at_push: int | None = None


class ToolCalls:
    """One call's tool calls: through ``CallSession.dispatch``, one at a time, never twice by accident.

    A call identical (tool and arguments) to an earlier one is answered with the
    earlier result while that call is still running, or when the generation
    issuing it was built from a prompt gradbot pushed before that result went
    back: gradbot emits ``push_to_llm`` when it builds a prompt and
    ``llm_started`` when that prompt starts generating, and a prompt pushed
    while a tool ran can start generating after the result went back. An
    identical call from a generation built after the result went back is the
    model's own repeat, and runs.
    """

    def __init__(self, session: CallSession, on_ended: asyncio.Event) -> None:
        self._session = session
        self._on_ended = on_ended
        self._lock = asyncio.Lock()
        self._pushes = 0
        self._generation_prompt = 0
        self._issued: dict[tuple[str, str], _Issued] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def pushed_to_llm(self) -> None:
        self._pushes += 1

    def llm_started(self) -> None:
        self._generation_prompt = self._pushes

    def start(self, info: Any, handle: Any) -> None:
        try:
            args = json.loads(info.args_json or "{}")
        except json.JSONDecodeError:
            args = None
        key = (info.tool_name, json.dumps(args, sort_keys=True))
        earlier = self._issued.get(key)
        if earlier is not None and (
            earlier.answered_at_push is None or self._generation_prompt <= earlier.answered_at_push
        ):
            logger.info(
                "gradbot re-issued %s before the model saw its result; answering with it", info.tool_name
            )
            work = self._answer_again(earlier, handle)
        else:
            issued = _Issued(asyncio.get_running_loop().create_future())
            self._issued[key] = issued
            work = self._run(info.tool_name, args, issued, handle)
        task = asyncio.create_task(work, name=f"tool {info.tool_name}")
        self._tasks.add(task)
        task.add_done_callback(self._finished)

    async def _run(self, name: str, args: Any, issued: _Issued, handle: Any) -> None:
        async with self._lock:
            # Shielded: a hang-up cancels the bridge, but a payment or a write that has started must finish.
            if isinstance(args, dict):
                result = await asyncio.shield(self._session.dispatch(name, args))
            else:
                result = {"error": "The arguments were not valid JSON. Call the tool again."}
        output = json.dumps(result, default=str)
        issued.answered_at_push = self._pushes
        issued.result.set_result(output)
        await handle.send(output)
        if self._session.ended:
            self._on_ended.set()

    @staticmethod
    async def _answer_again(earlier: _Issued, handle: Any) -> None:
        await handle.send(await earlier.result)

    def _finished(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("%s failed", task.get_name(), exc_info=error)

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


class GradiumProvider(VoiceProvider):
    name = "gradium"
    trace_adapter = "gradbot"
    required_env = ("GRADIUM_API_KEY", "OPENAI_API_KEY")
    tool_format = GradbotToolFormat()

    def __init__(self, config: GradiumConfig) -> None:
        self.config = config

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        return cls(GradiumConfig.from_env(env))

    def instructions(self, setup: SessionSetup) -> str:
        policy = setup.start_policy
        if not policy.agent_opens:
            return setup.instructions
        return f"{setup.instructions}\n\n{policy.opening_instruction}"

    def session_config(self, setup: SessionSetup) -> Any:
        return gradbot.SessionConfig(
            voice_id=self.config.voice_id,
            instructions=self.instructions(setup),
            language=gradbot.Lang.En,
            tools=self.tool_format.convert(setup.tools),
            assistant_speaks_first=setup.start_policy.agent_opens,
            silence_timeout_s=0.0,
        )

    async def run_call(self, call: CallRuntime) -> None:
        # By attribute and keywords only: the bench's trace adapter wraps gradbot.run.
        input_handle, output_handle = await gradbot.run(
            gradium_api_key=self.config.gradium_api_key,
            llm_api_key=self.config.llm_api_key,
            llm_model_name=self.config.llm_model,
            session_config=self.session_config(call.setup),
            input_format=gradbot.AudioFormat.Pcm,
            output_format=gradbot.AudioFormat.Pcm,
        )
        logger.info("gradbot session started: %s", self.config)
        bridge = _GradbotCall(call, output_handle)
        call.inbound.bind(input_handle.send_audio)
        try:
            await run_until_first_done(
                caller_to_gradbot=call.inbound.run(),
                gradbot_to_caller=bridge.events(),
                hang_up=bridge.hang_up_when_done(),
            )
        finally:
            await bridge.tools.close()
            await input_handle.close()


class _GradbotCall:
    """gradbot's messages for one call: audio to the caller, tool calls to ``ToolCalls``."""

    def __init__(self, call: CallRuntime, output: Any) -> None:
        self.call, self.output = call, output
        self.ended = asyncio.Event()
        self.tools = ToolCalls(call.session, self.ended)
        self.resampler = Resampler(GRADBOT_OUTPUT_RATE_HZ, CHANNEL_RATE_HZ)
        self.generation = 0
        self.last_audio_at = time.monotonic()
        self.spoke_last_words = asyncio.Event()

    async def events(self) -> None:
        while (msg := await self.output.receive()) is not None:
            kind = msg.msg_type
            if kind == "audio" and msg.data:
                self.last_audio_at = time.monotonic()
                await self.call.outbound.send(self.resampler.process(msg.data), f"gen-{self.generation}")
            elif kind == "stt_text":
                self.call.start.mark_callee_spoke()
            elif kind == "tool_call":
                self.tools.start(msg.tool_call, msg.tool_call_handle)
            elif kind == "event":
                self.on_event(msg.event.event_type)
        logger.info("gradbot session ended")

    def on_event(self, kind: str) -> None:
        if kind == "push_to_llm":
            self.tools.pushed_to_llm()
        elif kind == "llm_started":
            self.generation += 1
            self.tools.llm_started()
        elif kind == "interrupted":
            self.call.outbound.interrupted()
        elif kind == "end_tts_audio" and self.ended.is_set():
            self.spoke_last_words.set()

    async def hang_up_when_done(self) -> None:
        """Once a tool has ended the call: let the last words play, then hang up."""
        await self.ended.wait()
        while (quiet_left := QUIET_S - (time.monotonic() - self.last_audio_at)) > 0:
            try:
                await asyncio.wait_for(self.spoke_last_words.wait(), quiet_left)
                break
            except TimeoutError:
                continue  # audio may have arrived meanwhile: measure the quiet again
        await self.call.hang_up()


PROVIDER = GradiumProvider
