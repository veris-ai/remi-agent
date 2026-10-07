"""The agent on Pipecat: a cascaded STT -> LLM -> TTS pipeline.

Defaults (each overridable by env, see AGENTS.md "Config contract"):

- STT: Deepgram ``nova-3`` streaming (``VAMOS_PIPECAT_STT=deepgram``), or OpenAI (``openai``).
- LLM: OpenAI chat completions, ``gpt-4.1`` (``VAMOS_PIPECAT_LLM_MODEL``).
- TTS: OpenAI ``gpt-4o-mini-tts`` voice ``marin`` (``VAMOS_PIPECAT_TTS=openai``), or
  Cartesia (``cartesia``, needs ``CARTESIA_API_KEY`` and ``VAMOS_CARTESIA_VOICE_ID``).

Trace: no ``trace_adapter``. The LLM runs in this process through the
``openai`` SDK, which the bench's OTel bundle instruments (OpenInference
``OpenAIInstrumentor``), so every chat completion, with its tool calls and the
tool results in the next request, becomes an LLM span in the agent trace.

Call start: priming is a direct chat completion (same SDK, so it is traced)
with ``tool_choice`` forcing the connect reads, before the pipeline starts.
The pipeline then begins with those calls and results already in its context,
and the LLM runs only on a user turn, so the callee speaks first. For an agent
that speaks first, the pipeline's first frames (queued when the caller's
socket connects) append the policy's opening instruction and run the LLM.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from typing import Any, Self

from openai import AsyncOpenAI
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.adapters.services.open_ai_adapter import OpenAILLMAdapter
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import EndFrame, Frame, FunctionCallResultProperties, LLMMessagesAppendFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.turns.user_start import TranscriptionUserTurnStartStrategy
from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from vamos_voice.audio import CHANNEL_RATE_HZ
from vamos_voice.providers.base import CallRuntime, SessionSetup, TurnTaking, VoiceProvider
from vamos_voice.session import CallSession
from vamos_voice.tools import ToolSpec

from .serializer import RawPCM16Serializer
from .services import STT_FACTORIES, TTS_FACTORIES, ServiceConfig

logger = logging.getLogger(__name__)

DEFAULT_LLM_MODEL = "gpt-4.1"
LLM_TIMEOUT_S = 20
VAD_EDGE_S = 0.2


def vad_params(turns: TurnTaking) -> VADParams:
    """Silero's edges are fixed short; the shared end-of-turn silence is made up by the stop strategy."""
    return VADParams(confidence=turns.vad_threshold, start_secs=VAD_EDGE_S, stop_secs=VAD_EDGE_S)


def user_speech_timeout_s(turns: TurnTaking) -> float:
    """Silero's stop edge plus this timeout is the shared ``silence_ms``."""
    return max(0.0, turns.silence_ms / 1000 - VAD_EDGE_S)


class PipecatToolFormat:
    """Pipecat's own ``ToolsSchema``; its OpenAI adapter renders it per request."""

    def convert(self, tools: list[ToolSpec]) -> ToolsSchema:
        return ToolsSchema(
            standard_tools=[
                FunctionSchema(t.name, t.description, t.parameters["properties"], t.parameters["required"])
                for t in tools
            ]
        )


class PipecatProvider(VoiceProvider):
    name = "pipecat"
    trace_adapter = None
    required_env = ("OPENAI_API_KEY",)
    tool_format = PipecatToolFormat()

    def __init__(self, config: ServiceConfig, llm_model: str) -> None:
        self.config, self.llm_model = config, llm_model
        self._vad: SileroVADAnalyzer | None = None
        #: Builds the SDK client used for priming; tests swap in one with a mock transport.
        self.openai_client: Callable[[], AsyncOpenAI] = lambda: AsyncOpenAI(
            api_key=config.openai_api_key, timeout=LLM_TIMEOUT_S, max_retries=1
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        config = ServiceConfig.from_env(env)
        return cls(config, env.get("VAMOS_PIPECAT_LLM_MODEL") or DEFAULT_LLM_MODEL)

    async def warm_up(self) -> None:
        # Silero's ONNX model takes seconds to load: do it before /health says ready, not on the call.
        self._vad = await asyncio.to_thread(SileroVADAnalyzer, params=vad_params(TurnTaking()))

    # --- what goes to the LLM ----------------------------------------------------------------

    def build_context(self, setup: SessionSetup) -> LLMContext:
        """The conversation context; the instructions go on the LLM service (``system_instruction``)."""
        return LLMContext([], self.tool_format.convert(setup.tools))

    def build_llm(self, setup: SessionSetup) -> OpenAILLMService:
        return OpenAILLMService(
            api_key=self.config.openai_api_key,
            settings=OpenAILLMService.Settings(model=self.llm_model, system_instruction=setup.instructions),
        )

    def openai_tools(self, setup: SessionSetup) -> list[Any]:
        """The same tools in chat-completions form, rendered by Pipecat's own OpenAI adapter."""
        return OpenAILLMAdapter().to_provider_tools_format(self.tool_format.convert(setup.tools))

    async def prime(self, call: CallRuntime, context: LLMContext) -> None:
        """Run the connect reads as real, traced model tool calls before the callee speaks."""
        if not call.setup.start_policy.connect_reads:
            return
        client = self.openai_client()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": call.setup.instructions},
            {"role": "system", "content": call.setup.start_policy.prime_instruction},
        ]
        tools = self.openai_tools(call.setup)
        forced: Any = "required"
        for _ in range(len(call.setup.start_policy.connect_reads) + 1):
            completion = await client.chat.completions.create(
                model=self.llm_model, messages=messages, tools=tools, tool_choice=forced
            )
            message = completion.choices[0].message
            tool_calls = message.tool_calls or []
            turn = [
                {
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                        }
                        for tc in tool_calls
                    ],
                }
            ]
            for tc in tool_calls:
                result = await call.session.dispatch(
                    tc.function.name, json.loads(tc.function.arguments or "{}")
                )
                turn.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result, default=str)}
                )
            messages += turn
            context.add_messages(turn)
            missing = call.session.missing_reads
            if not missing:
                return
            forced = {"type": "function", "function": {"name": missing[0]}}
        call.start.warn_priming_abandoned(call.session.missing_reads)

    async def prime_within_deadline(self, call: CallRuntime, context: LLMContext) -> None:
        """Prime, but never past the call's priming deadline; then the caller is heard either way."""
        try:
            await asyncio.wait_for(self.prime(call, context), call.start.priming_timeout_s)
        except TimeoutError:
            call.start.warn_priming_abandoned(call.session.missing_reads)
        call.start.mark_primed()

    def opening_frames(self, setup: SessionSetup) -> list[Frame]:
        """What the pipeline does as soon as the caller is connected: open the call if the agent speaks first."""
        if not setup.start_policy.agent_opens:
            return []
        message = {"role": "system", "content": setup.start_policy.opening_instruction}
        return [LLMMessagesAppendFrame([message], run_llm=True)]

    # --- the call --------------------------------------------------------------------------

    async def run_call(self, call: CallRuntime) -> None:
        context = self.build_context(call.setup)
        await self.prime_within_deadline(call, context)
        worker = self.build_worker(call, context)
        await PipelineRunner(handle_sigint=False).run(worker)

    def build_worker(self, call: CallRuntime, context: LLMContext) -> PipelineWorker:
        turns = call.setup.turn_taking
        transport = FastAPIWebsocketTransport(
            websocket=call.line.websocket,
            params=FastAPIWebsocketParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                audio_in_sample_rate=CHANNEL_RATE_HZ,
                audio_out_sample_rate=CHANNEL_RATE_HZ,
                serializer=RawPCM16Serializer(),
            ),
        )
        llm = self.build_llm(call.setup)
        llm.register_function(None, _dispatcher(call.session))
        user, assistant = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(
                vad_analyzer=self._vad or SileroVADAnalyzer(params=vad_params(turns)),
                user_turn_strategies=UserTurnStrategies(
                    # A user turn starts on a final transcript, not on raw VAD: background noise must
                    # not interrupt the agent or cancel a tool call.
                    start=[TranscriptionUserTurnStartStrategy(use_interim=False)],
                    stop=[
                        SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=user_speech_timeout_s(turns))
                    ],
                ),
            ),
        )
        pipeline = Pipeline(
            [
                transport.input(),
                STT_FACTORIES[self.config.stt](self.config),
                user,
                llm,
                TTS_FACTORIES[self.config.tts](self.config),
                transport.output(),
                assistant,
            ]
        )
        # No RTVI: the bench channel carries audio only.
        worker = PipelineWorker(
            pipeline, params=PipelineParams(enable_metrics=True), app_resources=call, enable_rtvi=False
        )

        opening = self.opening_frames(call.setup)

        @transport.event_handler("on_client_connected")
        async def _connected(_transport: Any, _client: Any) -> None:
            if opening:
                await worker.queue_frames(opening)

        @transport.event_handler("on_client_disconnected")
        async def _disconnected(_transport: Any, _client: Any) -> None:
            await worker.cancel()

        return worker


def _dispatcher(session: CallSession) -> Callable[[FunctionCallParams], Any]:
    async def handle(params: FunctionCallParams) -> None:
        # Shielded: a barge-in may cancel the handler, but a payment or a database write
        # that has started must finish, or the call and the world would disagree.
        result = await asyncio.shield(session.dispatch(params.function_name, dict(params.arguments)))
        if session.ended:
            await params.result_callback(result, properties=FunctionCallResultProperties(run_llm=False))
            await params.pipeline_worker.queue_frame(EndFrame())  # flushes the last words, then hangs up
            return
        await params.result_callback(result)

    return handle


PROVIDER = PipecatProvider
