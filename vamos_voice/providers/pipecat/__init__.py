"""The agent on Pipecat: a cascaded STT -> LLM -> TTS pipeline.

Defaults (each overridable by env, see AGENTS.md "Config contract"):

- STT: Deepgram ``nova-3`` streaming (``VAMOS_PIPECAT_STT=deepgram``), or OpenAI (``openai``).
- LLM: OpenAI chat completions, ``gpt-4.1-mini`` (``VAMOS_PIPECAT_LLM_MODEL``), the text model of
  every pipeline vendor.
- TTS: OpenAI ``gpt-4o-mini-tts`` voice ``marin`` (``VAMOS_PIPECAT_TTS=openai``), or
  Cartesia (``cartesia``, needs ``CARTESIA_API_KEY`` and ``VAMOS_CARTESIA_VOICE_ID``).

Trace: no ``trace_adapter``. The LLM runs in this process through the
``openai`` SDK, which the bench's OTel bundle instruments (OpenInference
``OpenAIInstrumentor``), so every chat completion, with its tool calls and the
tool results in the next request, becomes an LLM span in the agent trace.

Call start: the system instruction carries the agent's screen pop, so the
pipeline starts with an empty context and the LLM runs only on a user turn:
the callee speaks first. For an agent that speaks first, the pipeline's first
frames (queued when the caller's socket connects) append the policy's opening
instruction and run the LLM.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from typing import Any, Self

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
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
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from vamos_voice.audio import CHANNEL_RATE_HZ
from vamos_voice.providers.base import OPENAI_SDK_MAX_RETRIES, CallRuntime, SessionSetup, VoiceProvider
from vamos_voice.session import CallSession
from vamos_voice.tools import ToolSpec

from .serializer import LineSerializer
from .services import STT_FACTORIES, TTS_FACTORIES, ServiceConfig

logger = logging.getLogger(__name__)

DEFAULT_LLM_MODEL = "gpt-4.1-mini"


class RetryingOpenAILLM(OpenAILLMService):
    """Pipecat's OpenAI LLM with the shared retry budget (``RETRY_BUDGET_S``); Pipecat leaves the SDK's 2."""

    def create_client(self, *args: Any, **kwargs: Any) -> Any:
        return super().create_client(*args, **kwargs).with_options(max_retries=OPENAI_SDK_MAX_RETRIES)


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

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        config = ServiceConfig.from_env(env)
        return cls(config, env.get("VAMOS_PIPECAT_LLM_MODEL") or DEFAULT_LLM_MODEL)

    async def warm_up(self) -> None:
        # Silero's ONNX model takes seconds to load: do it before /health says ready, not on the call.
        self._vad = await asyncio.to_thread(SileroVADAnalyzer)

    # --- what goes to the LLM ----------------------------------------------------------------

    def build_context(self, setup: SessionSetup) -> LLMContext:
        """The conversation context; the instructions go on the LLM service (``system_instruction``)."""
        return LLMContext([], self.tool_format.convert(setup.tools))

    def build_llm(self, setup: SessionSetup) -> OpenAILLMService:
        return RetryingOpenAILLM(
            api_key=self.config.openai_api_key,
            settings=OpenAILLMService.Settings(model=self.llm_model, system_instruction=setup.instructions),
        )

    def opening_frames(self, setup: SessionSetup) -> list[Frame]:
        """What the pipeline does as soon as the caller is connected: open the call if the agent speaks first."""
        if not setup.start_policy.agent_opens:
            return []
        message = {"role": "system", "content": setup.start_policy.opening_instruction}
        return [LLMMessagesAppendFrame([message], run_llm=True)]

    def user_params(self) -> LLMUserAggregatorParams:
        """Pipecat's own turn taking: Silero on its default parameters, the default start strategies
        (VAD, then transcription) and stop strategy (Smart Turn v3)."""
        return LLMUserAggregatorParams(
            vad_analyzer=self._vad or SileroVADAnalyzer(), user_turn_strategies=UserTurnStrategies()
        )

    # --- the call --------------------------------------------------------------------------

    async def run_call(self, call: CallRuntime) -> None:
        worker = self.build_worker(call, self.build_context(call.setup))
        await PipelineRunner(handle_sigint=False).run(worker)

    def build_worker(self, call: CallRuntime, context: LLMContext) -> PipelineWorker:
        transport = FastAPIWebsocketTransport(
            websocket=call.line.websocket,
            params=FastAPIWebsocketParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                audio_in_sample_rate=CHANNEL_RATE_HZ,
                audio_out_sample_rate=CHANNEL_RATE_HZ,
                serializer=LineSerializer(call.line.framing, on_caller_end=call.line.caller_gone),
            ),
        )
        llm = self.build_llm(call.setup)
        llm.register_function(None, _dispatcher(call.session))
        user, assistant = LLMContextAggregatorPair(
            context,
            user_params=self.user_params(),
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
            call.line.caller_gone()
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
