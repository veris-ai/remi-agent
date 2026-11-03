"""Pipecat: a tool call the caller talks over still runs once, and the model sees what it really did.

Pipecat 1.12 broadcasts an interruption at every user turn start, even while a
tool runs, and by default cancels the call and writes ``CANCELLED`` as its
result. Our tool calls are shielded and always finish (a payment that started is
made), so the model must get their true result: in its context before the next
inference when Pipecat's flow allows, and never with a reply forced for it.

These drive the real pieces: the provider's LLM service with its tool handler
(registered as ``build_worker`` registers it), Pipecat's context aggregators and
a user turn through Pipecat's own turn start (the interruption) and stop. Only
OpenAI is scripted: ``chat.completions.create`` returns a canned stream and
records what each inference sent.
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest

from .fakes import REMI

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("pipecat") is None, reason="pipecat extra not installed"
)

ARGS = {"method_id": "pm_1", "amount": 50.0}
RESULT = {"status": "approved", "payment_id": "pay_1", "posted": True}
IN_PROGRESS = "IN_PROGRESS"  # Pipecat's placeholder while a tool runs
SETTLE_S = 0.5  # long enough for an inference the result would trigger to reach the model


class HeldSession:
    """A session whose one tool call runs until the test lets it return."""

    def __init__(self, *, ends_call: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.ended = False
        self.ends_call = ends_call
        self.running = asyncio.Event()
        self.release = asyncio.Event()

    async def dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, args))
        self.running.set()
        await self.release.wait()
        self.ended = self.ends_call
        return RESULT


class PipecatCall:
    """The provider's LLM and tool handler between Pipecat's own context aggregators."""

    def __init__(self, session: HeldSession, tool: str) -> None:
        from pipecat.frames.frames import InterruptionFrame
        from pipecat.observers.base_observer import BaseObserver, FramePushed
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.pipeline.worker import PipelineWorker
        from pipecat.processors.aggregators.llm_response_universal import (
            LLMContextAggregatorPair,
            LLMUserAggregatorParams,
        )
        from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies

        from vamos_voice.providers.pipecat import PipecatProvider, _dispatcher
        from vamos_voice.providers.pipecat.services import ServiceConfig

        provider = PipecatProvider(
            ServiceConfig(openai_api_key="sk-test", stt="deepgram", tts="openai"), "gpt-test"
        )
        setup = REMI.setup()
        self.context = provider.build_context(setup)
        self.llm = provider.build_llm(setup)
        self.llm.register_function(None, _dispatcher(session))  # exactly as build_worker does
        self.requests: list[list[dict[str, Any]]] = []
        self.llm._client.chat.completions.create = self._create  # type: ignore[method-assign]
        self.tool = tool
        self.interruptions = 0
        call = self

        class CountInterruptions(BaseObserver):
            async def on_push_frame(self, data: FramePushed) -> None:
                if isinstance(data.frame, InterruptionFrame) and data.destination is call.llm:
                    call.interruptions += 1

        # Turns come from the test (Pipecat's external strategies) instead of VAD and Smart Turn;
        # a turn start still goes through the aggregator's own interruption broadcast.
        user, assistant = LLMContextAggregatorPair(
            self.context,
            user_params=LLMUserAggregatorParams(user_turn_strategies=ExternalUserTurnStrategies()),
        )
        self.worker = PipelineWorker(
            Pipeline([user, self.llm, assistant]),
            enable_rtvi=False,
            cancel_on_idle_timeout=False,
            observers=[CountInterruptions()],
        )

    async def _create(self, **params: Any) -> AsyncIterator[Any]:
        from openai.types.chat import ChatCompletionChunk
        from openai.types.chat.chat_completion_chunk import (
            Choice,
            ChoiceDelta,
            ChoiceDeltaToolCall,
            ChoiceDeltaToolCallFunction,
        )

        self.requests.append(copy.deepcopy(params["messages"]))
        if len(self.requests) == 1:  # the model asks for the tool
            call = ChoiceDeltaToolCall(
                index=0,
                id="call_1",
                type="function",
                function=ChoiceDeltaToolCallFunction(name=self.tool, arguments=json.dumps(ARGS)),
            )
            delta = ChoiceDelta(tool_calls=[call])
        else:
            delta = ChoiceDelta(content="One moment.")
        chunk = ChatCompletionChunk(
            id="c",
            object="chat.completion.chunk",
            created=0,
            model="gpt-test",
            choices=[Choice(index=0, delta=delta, finish_reason=None)],
        )

        async def stream() -> AsyncIterator[ChatCompletionChunk]:
            yield chunk

        return stream()

    def tool_message(self) -> str | None:
        return next(
            (m["content"] for m in self.context.get_messages() if m.get("tool_call_id") == "call_1"),
            None,
        )

    @asynccontextmanager
    async def running(self) -> AsyncIterator[asyncio.Task[None]]:
        """The pipeline, running; the caller has asked for a payment. Always torn down."""
        from pipecat.frames.frames import LLMMessagesAppendFrame
        from pipecat.workers.runner import WorkerRunner

        started = asyncio.Event()

        @self.worker.event_handler("on_pipeline_started")
        async def _started(_worker: Any, _frame: Any) -> None:
            started.set()

        workers = WorkerRunner(handle_sigint=False)
        await workers.add_workers(self.worker)
        runner = asyncio.create_task(workers.run())
        try:
            await asyncio.wait_for(started.wait(), 5)
            ask = {"role": "user", "content": "Take fifty dollars from my card."}
            await self.worker.queue_frame(LLMMessagesAppendFrame([ask], run_llm=True))
            yield runner
        finally:
            if not runner.done():
                await self.worker.cancel()
                try:
                    await asyncio.wait_for(runner, 5)
                except TimeoutError:
                    runner.cancel()

    async def caller_starts_talking(self) -> None:
        from pipecat.frames.frames import ProposedUserStartedSpeakingFrame

        await self.worker.queue_frame(ProposedUserStartedSpeakingFrame())
        await until(lambda: self.interruptions > 0)

    async def caller_says(self, text: str) -> None:
        """Ends the caller's turn with ``text``: Pipecat writes it to the context and runs the LLM."""
        from pipecat.frames.frames import ProposedUserStoppedSpeakingFrame, TranscriptionFrame

        await self.worker.queue_frame(TranscriptionFrame(text, "caller", "2027-02-10T09:00:00-05:00"))
        await self.worker.queue_frame(ProposedUserStoppedSpeakingFrame())


async def until(condition: Callable[[], bool], within: float = 3.0) -> None:
    async def poll() -> None:
        while not condition():  # noqa: ASYNC110 - polls test state that has no event
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), within)


def tool_content(messages: list[dict[str, Any]]) -> Any:
    return next(m["content"] for m in messages if m.get("tool_call_id") == "call_1")


async def test_a_tool_nobody_talks_over_is_answered_as_pipecat_answers_it() -> None:
    session = HeldSession()
    call = PipecatCall(session, "take_payment")
    async with call.running():
        await asyncio.wait_for(session.running.wait(), 3)
        session.release.set()
        await until(lambda: len(call.requests) == 2)  # Pipecat runs the LLM on the result
        await asyncio.sleep(SETTLE_S)

    assert call.interruptions == 0
    assert tool_content(call.requests[1]) == json.dumps(RESULT)
    assert len(call.requests) == 2


@pytest.mark.parametrize("result_lands", ["while the caller talks", "after the model answered the caller"])
async def test_a_tool_the_caller_talks_over_reports_its_real_result_once(result_lands: str) -> None:
    session = HeldSession()
    call = PipecatCall(session, "take_payment")
    async with call.running():
        await asyncio.wait_for(session.running.wait(), 3)
        await call.caller_starts_talking()  # Pipecat interrupts: the payment is under way
        if result_lands == "while the caller talks":
            session.release.set()
            await until(lambda: call.tool_message() != IN_PROGRESS)
            await call.caller_says("Okay.")
            await until(lambda: len(call.requests) == 2)
            # the caller's turn is answered with the payment's real result already in the context
            assert tool_content(call.requests[1]) == json.dumps(RESULT)
        else:
            await call.caller_says("Okay.")
            await until(lambda: len(call.requests) == 2)
            assert tool_content(call.requests[1]) == IN_PROGRESS  # Pipecat answers the turn at once
            session.release.set()
            await until(lambda: call.tool_message() != IN_PROGRESS)
        await asyncio.sleep(SETTLE_S)

    assert session.calls == [("take_payment", ARGS)], "the tool ran exactly once"
    assert call.tool_message() == json.dumps(RESULT), "the context holds what the tool really did"
    assert "CANCELLED" not in json.dumps(call.context.get_messages())
    assert len(call.requests) == 2, "no inference was forced for the late result"
    assert call.requests[1][-1] == {"role": "user", "content": "Okay."}


async def test_end_call_the_caller_talks_over_still_hangs_up() -> None:
    session = HeldSession(ends_call=True)
    call = PipecatCall(session, "end_call")
    async with call.running() as runner:
        await asyncio.wait_for(session.running.wait(), 3)
        await call.caller_starts_talking()
        session.release.set()
        # the handler's EndFrame ends the pipeline: nobody else sends one
        await asyncio.wait({runner}, timeout=5)
        assert runner.done(), "the pipeline did not end after end_call"

    assert session.calls == [("end_call", ARGS)]
    assert call.tool_message() == json.dumps(RESULT)
    assert len(call.requests) == 1, "no inference after the call ended"
