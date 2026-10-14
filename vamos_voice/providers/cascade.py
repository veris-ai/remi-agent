"""In-pod cascades: the vendor's speech-to-text, a chat model with the agent's tools, the vendor's text-to-speech.

Mistral (Voxtral STT + Mistral chat + Voxtral TTS), Hugging Face (Whisper +
an open LLM + Kokoro on HF inference) and Grok STT + TTS sell speech in and
speech out as separate services, with no session holding the conversation.
So the pipeline runs here, written once in ``CascadeProvider`` and shared by
those vendors; a vendor supplies only its three legs:

- ``SpeechToText``: caller audio in, the caller's finished turns out. The leg
  decides where a turn ends: by the vendor's own endpointing where it has one
  (Grok STT's Smart Turn), otherwise by ``SilenceEndpoint`` over Silero's
  verdict (Mistral, Hugging Face).
- ``ChatModel``: one LLM round over the conversation and the tools, streamed as
  text deltas and then the round's tool calls.
- ``TextToSpeech``: the round's text in, PCM16 24 kHz out.

What the cascade itself owns, the same for every vendor:

- **Instructions and tools.** The system message is ``setup.instructions``
  (the screen pop already appended); the tools are the registry's specs in the
  OpenAI function-calling shape (every cascade's chat API takes it).
- **Call start.** The model runs only on a caller turn, so a callee-first
  agent never speaks before the callee: the first finished turn is the
  callee-spoke signal. An agent-first agent gets its opening round at once,
  with ``opening_instruction`` appended as a system message (as on Pipecat).
- **Rounds.** A caller turn is answered by up to ``MAX_TOOL_ROUNDS`` LLM
  rounds: each round's text is spoken as it streams, then its tool calls run
  one at a time, in order, through ``CallSession.dispatch``; the next round
  sees the results. Turns that finish while the agent is answering are
  answered together, in one user message, next.
- **Barge-in.** None of these vendors detects the caller talking over the
  agent, so the cascade must: Silero VAD on the caller's audio (``silero.py``,
  the detector the VAmoS Energy candidates of these vendors use), under
  ``BargeInRule``: LiveKit Agents' default interruption handling (livekit-agents
  1.8, ``AgentSession`` in VAD mode, its self-hosted default), all of it, the
  same for every cascade. Caller utterances are segmented as LiveKit's Silero
  stream does (``CallerUtterances``). An utterance that has lasted
  ``min_speech_ms`` (0.5 s; ``min_words`` 0) while a turn is being answered
  *pauses* the reply's audio; so does any utterance that starts, or is under
  way, before the reply is audible. When the utterance ends, the pause becomes
  a false interruption after ``false_interruption_timeout_s`` (2 s; 0 s for a
  pause that never reached 0.5 s) and the reply resumes where it stopped
  (``resume_false_interruption``). A finished caller turn (the STT's final
  transcript, never empty) arriving while a turn is being answered interrupts
  it: the audio stops, the vendor's TTS utterance is cleared, and the rest of
  that turn is silenced. No other noise filter: noise is a benchmark
  condition. The LLM round it lands in
  still completes and its tool calls still run (a payment that was decided is
  made and recorded); then the turn ends and the caller's words are the next
  turn. The interrupted reply is kept in the history as the words the caller
  can have heard (``OutboundAudio.played_ms``), marked ``INTERRUPTED_MARKER``.
- **Playout.** TTS audio goes out in ``PLAYBACK_SLICE_MS`` slices, at most
  ``PLAYBACK_LEAD_MS`` ahead of real time: the vendors stream faster than
  speech plays, and audio already at the caller cannot be cut.
- **Hang-up.** When a tool ends the call (``session.ended``), no further round
  is asked for; the last words play and ``call.hang_up()`` closes the line.

Trace: no ``trace_adapter``. Each LLM round is one SDK call the bench's OTel
bundle instruments (OpenInference ``OpenAIInstrumentor`` / ``MistralAIInstrumentor``):
the instructions, tools, tool calls and the results in the next request.
"""

from __future__ import annotations

import abc
import asyncio
import enum
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, aclosing
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from ..audio import pcm_ms
from ..tools import ToolSpec
from .base import MAX_TOOL_ROUNDS, CallRuntime, SessionSetup, VoiceProvider
from .silero import SileroVad, inference_session
from .wire import Connect, connect_websocket, dispatch_json_call, run_until_first_done

logger = logging.getLogger(__name__)

PLAYBACK_SLICE_MS = 500
PLAYBACK_LEAD_MS = 1000
#: 24 kHz PCM16: 48 bytes per ms.
BYTES_PER_MS = 48
#: Ends an interrupted reply in the history, in place of the words never played.
INTERRUPTED_MARKER = "… [interrupted by the caller]"
#: The tool result when the model's arguments are not JSON (the call is not run).
BAD_ARGUMENTS = json.dumps({"error": "The arguments were not valid JSON. Call the tool again."})

Message = dict[str, Any]
TurnSink = Callable[[str], Awaitable[None]]
#: LiveKit's Silero stream defaults (livekit-plugins-silero 1.8): this much speech starts a caller
#: utterance, and this much non-speech ends it (shorter pauses between words do not).
MIN_SPEECH_MS = 50
MIN_SILENCE_MS = 550


@dataclass(frozen=True)
class ToolCall:
    """One complete tool call from an LLM round."""

    id: str
    name: str
    arguments: str

    def message(self) -> dict[str, Any]:
        """The call as it is kept on the assistant message."""
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


class ChatToolFormat:
    """Chat-completions function tools: ``{"type": "function", "function": {name, description, parameters}}``."""

    def convert(self, tools: list[ToolSpec]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
            }
            for t in tools
        ]


class VoiceActivity(Protocol):
    """Is the caller speaking? Fed every caller frame in order (``silero.SileroVad``)."""

    def push(self, frame: bytes) -> bool: ...


class SpeechToText(abc.ABC):
    """One call's STT leg."""

    @abc.abstractmethod
    async def feed(self, pcm: bytes, speaking: bool) -> None:
        """Caller audio (PCM16 24 kHz) with Silero's verdict on it."""

    @abc.abstractmethod
    async def run(self, on_turn: TurnSink) -> None:
        """Hand each finished caller turn to ``on_turn`` until the vendor closes; raise on a vendor error."""


class TextToSpeech(abc.ABC):
    """One call's TTS leg."""

    #: How long the voice takes per word: turns the audio played before a barge-in into words heard
    #: when the leg cannot say how long the whole reply is.
    ms_per_word: ClassVar[int] = 400

    @abc.abstractmethod
    def speak(self, text: AsyncIterator[str]) -> AsyncIterator[bytes]:
        """One utterance: text deltas in (until exhausted), PCM16 24 kHz out (even byte counts)."""

    async def clear(self) -> None:  # noqa: B027 - optional hook
        """The caller barged in: abandon the utterance in flight (vendors with a streaming socket)."""


class ChatModel(abc.ABC):
    """The LLM leg."""

    @abc.abstractmethod
    def reply(self, messages: list[Message], tools: list[dict[str, Any]]) -> AsyncIterator[str | ToolCall]:
        """One round: text deltas as they arrive, then the round's complete tool calls."""


class SilenceEndpoint:
    """End of turn after ``silence_ms`` of non-speech following speech, counted in audio time.

    Audio time, not the wall clock: frames arrive in bursts when anything
    stalls, and a wall-clock timer would read a burst as a long silence and end
    the turn mid-sentence. Used by the legs whose vendor has no endpointing.
    """

    def __init__(self, silence_ms: int) -> None:
        self.silence_ms = silence_ms
        self.in_turn = False
        self._silent_ms = 0.0

    def push(self, n_bytes: int, speaking: bool) -> bool:
        """Account for one frame; True when it ends the caller's turn."""
        if speaking:
            self.in_turn, self._silent_ms = True, 0.0
            return False
        if not self.in_turn:
            return False
        self._silent_ms += pcm_ms(n_bytes)
        if self._silent_ms < self.silence_ms:
            return False
        self.in_turn, self._silent_ms = False, 0.0
        return True


@dataclass(frozen=True)
class BargeInRule:
    """When the caller talking over the agent pauses it, and when a pause was a false interruption
    (every cascade, the same): LiveKit Agents' ``InterruptionOptions`` defaults (livekit-agents 1.8).

    ``min_speech_ms`` is ``min_duration`` (``min_words`` is 0: no word count);
    ``false_interruption_timeout_s`` is ``false_interruption_timeout``, with
    ``resume_false_interruption`` on.
    """

    min_speech_ms: int = 500
    false_interruption_timeout_s: float = 2.0


class Utterance(enum.Enum):
    STARTED = "started"
    ENDED = "ended"


class CallerUtterances:
    """The caller's utterances as LiveKit's Silero stream segments them, over Silero's verdicts:
    one starts after ``MIN_SPEECH_MS`` of speech and ends after ``MIN_SILENCE_MS`` of non-speech;
    its ``duration_ms`` runs from its start, pauses included."""

    def __init__(self) -> None:
        self.speaking = False
        self.duration_ms = 0.0
        self._speech_ms = 0.0
        self._silence_ms = 0.0

    def audio(self, ms: float, speech: bool) -> Utterance | None:
        """Account for ``ms`` of caller audio; the utterance boundary it crosses, if any."""
        if self.speaking:
            self.duration_ms += ms
        if speech:
            self._silence_ms = 0.0
            self._speech_ms += ms
            if not self.speaking and self._speech_ms >= MIN_SPEECH_MS:
                self.speaking, self.duration_ms = True, self._speech_ms
                return Utterance.STARTED
            return None
        self._speech_ms = 0.0
        if not self.speaking:
            return None
        self._silence_ms += ms
        if self._silence_ms < MIN_SILENCE_MS:
            return None
        self.speaking, self.duration_ms, self._silence_ms = False, 0.0, 0.0
        return Utterance.ENDED


class CascadeProvider(VoiceProvider, abc.ABC):
    """A vendor whose STT, LLM and TTS are run as a cascade in this process."""

    trace_adapter = None
    tool_format = ChatToolFormat()
    #: How a leg opens its vendor socket; tests replace it to capture frames offline.
    connect: Connect = staticmethod(connect_websocket)
    llm: ChatModel
    #: When caller speech interrupts the agent (documented in the module docstring).
    barge_in_rule: ClassVar[BargeInRule] = BargeInRule()

    @abc.abstractmethod
    def open_legs(self, call: CallRuntime) -> AbstractAsyncContextManager[tuple[SpeechToText, TextToSpeech]]:
        """The call's STT and TTS legs, open for the length of the call."""

    def voice_activity(self, setup: SessionSetup) -> VoiceActivity:
        """The call's speech detector (barge-in, and the end of turn for legs without vendor endpointing)."""
        return SileroVad(setup.turn_taking.vad_threshold)

    def system_messages(self, setup: SessionSetup) -> list[Message]:
        """The conversation's start: the instructions, verbatim."""
        return [{"role": "system", "content": setup.instructions}]

    async def warm_up(self) -> None:
        await asyncio.to_thread(inference_session)  # Silero's model, before /health says ready

    async def run_call(self, call: CallRuntime) -> None:
        async with self.open_legs(call) as (stt, tts):
            await _CascadeCall(self, call, stt, tts).run()


class _CascadeCall:
    """One call: the caller-audio pump, the STT reader and the conversation, against shared state."""

    def __init__(self, provider: CascadeProvider, call: CallRuntime, stt: SpeechToText, tts: TextToSpeech):
        self.provider, self.call, self.stt, self.tts = provider, call, stt, tts
        self.messages = provider.system_messages(call.setup)
        self.tools = provider.tool_format.convert(call.setup.tools)
        self.vad = provider.voice_activity(call.setup)
        self.rule = provider.barge_in_rule
        self.utterances = CallerUtterances()
        self.turns: asyncio.Queue[str] = asyncio.Queue()
        self.replying = False  # a turn is being answered
        self.cut = False  # ...and the caller interrupted it
        #: Set while the reply's audio may play; cleared while caller speech pauses it.
        self.playing = asyncio.Event()
        self.playing.set()
        #: While paused: how long after the caller's utterance ends the pause is a false interruption.
        self.resume_after_s: float | None = None
        self.resume_timer: asyncio.TimerHandle | None = None
        self.item: str | None = None  # the round being spoken
        self.player: asyncio.Task[None] | None = None
        self.n_items = 0
        self.received_ms: dict[str, float] = {}  # TTS audio received per item
        self.complete: set[str] = set()  # items whose TTS audio all arrived

    async def run(self) -> None:
        self.call.inbound.bind(self.hear)
        await run_until_first_done(
            caller_to_stt=self.call.inbound.run(),
            stt_to_turns=self.stt.run(self.on_turn),
            turns=self.converse(),
        )

    # --- the caller ---------------------------------------------------------------------------

    async def hear(self, pcm: bytes) -> None:
        speaking = self.vad.push(pcm)
        boundary = self.utterances.audio(pcm_ms(len(pcm)), speaking)
        if boundary is Utterance.STARTED:
            self.cancel_resume()
            if not self.audible():
                self.hold()
        if self.utterances.speaking and self.utterances.duration_ms >= self.rule.min_speech_ms:
            self.cancel_resume()
            self.pause(self.rule.false_interruption_timeout_s)
        if boundary is Utterance.ENDED and self.resume_after_s is not None:
            self.resume_timer = asyncio.get_running_loop().call_later(self.resume_after_s, self.resume)
        await self.stt.feed(pcm, speaking)

    def audible(self) -> bool:
        """The reply's audio is playing at the caller (LiveKit's agent state "speaking")."""
        outbound = self.call.outbound
        return self.item is not None and outbound.played_ms(self.item) < outbound.sent_ms(self.item)

    def hold(self) -> None:
        """Caller speech while the reply is not audible (yet): LiveKit pauses it, to resume as soon
        as that utterance ends unless it becomes an interruption or a turn."""
        if self.resume_after_s is None:
            self.pause(0.0)

    def pause(self, resume_after_s: float) -> None:
        """Hold the reply's audio (what already went out still plays) until a false interruption
        resumes it ``resume_after_s`` after the caller's utterance ends, or a turn interrupts it."""
        if not self.replying or self.cut:
            return
        if self.resume_after_s is None:
            logger.info("caller speech over the agent: its reply is paused")
            self.playing.clear()
        self.resume_after_s = resume_after_s

    def resume(self) -> None:
        """The caller's utterance ended with no turn: a false interruption, the reply goes on."""
        self.resume_timer = None
        if self.resume_after_s is None or not self.replying or self.cut:
            return
        logger.info("false interruption: the reply resumes")
        self.unpause()

    def unpause(self) -> None:
        self.cancel_resume()
        self.resume_after_s = None
        self.playing.set()

    def cancel_resume(self) -> None:
        if self.resume_timer is not None:
            self.resume_timer.cancel()
            self.resume_timer = None

    def barge_in(self) -> None:
        if not self.replying or self.cut:
            return
        logger.info("barge-in: the caller spoke a turn over the agent; the rest of this turn is silenced")
        self.cut = True
        self.unpause()
        if self.player is not None:
            self.player.cancel()
        self.call.outbound.interrupted()

    async def on_turn(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        logger.info("caller turn (%d chars)", len(text))  # never the words: a caller may read out a card
        self.call.start.mark_callee_spoke()
        self.barge_in()  # LiveKit: a final transcript interrupts the reply under way
        self.turns.put_nowait(text)

    # --- the conversation ---------------------------------------------------------------------

    async def converse(self) -> None:
        policy = self.call.setup.start_policy
        if policy.agent_opens:
            self.messages.append({"role": "system", "content": policy.opening_instruction})
            if await self.respond():
                return
        while True:
            texts = [await self.turns.get()]
            while not self.turns.empty():
                texts.append(self.turns.get_nowait())
            self.messages.append({"role": "user", "content": " ".join(texts)})
            if await self.respond():
                return

    async def respond(self) -> bool:
        """Answer the conversation so far; True when the agent has hung up."""
        self.replying, self.cut = True, False
        if self.utterances.speaking:  # the caller is mid-utterance: the reply starts paused
            self.hold()
        try:
            await self.rounds()
        finally:
            self.replying, self.item = False, None
            self.unpause()
        if self.call.session.ended:
            await self.call.hang_up()
            return True
        return False

    async def rounds(self) -> None:
        for _ in range(MAX_TOOL_ROUNDS):
            self.n_items += 1
            item = self.item = f"reply-{self.n_items}"
            calls: list[ToolCall] = []
            said = await self.say(self.round_text(calls), item)
            message: Message = {"role": "assistant"}
            content = self.heard(said, item) if self.cut else said
            if content:
                message["content"] = content
            if calls:
                message["tool_calls"] = [c.message() for c in calls]
            if len(message) > 1:
                self.messages.append(message)
            for tool_call in calls:
                self.messages.append(await self.run_tool(tool_call))
            if self.call.session.ended or self.cut or not calls:
                return
        logger.warning("the model still wanted tools after %d rounds; the turn ends", MAX_TOOL_ROUNDS)

    async def round_text(self, calls: list[ToolCall]) -> AsyncIterator[str]:
        async for part in self.provider.llm.reply(self.messages, self.tools):
            if isinstance(part, ToolCall):
                calls.append(part)
            elif part:
                yield part

    async def run_tool(self, tool_call: ToolCall) -> Message:
        # Shielded: a hang-up may cancel the turn, but a payment or a write that has started must finish.
        output = await asyncio.shield(
            dispatch_json_call(self.call.session, tool_call.name, tool_call.arguments)
        )
        return {
            "role": "tool",
            "tool_call_id": tool_call.id,
            "name": tool_call.name,
            "content": output if output is not None else BAD_ARGUMENTS,
        }

    # --- speaking -----------------------------------------------------------------------------

    async def say(self, deltas: AsyncIterator[str], item: str) -> str:
        """Speak a round as it streams; return its whole text. ``deltas`` is always read to the end,
        so a barge-in never cuts a round short of its tool calls."""
        said = ""
        text: asyncio.Queue[str | None] = asyncio.Queue()
        async for delta in deltas:
            said += delta
            if self.cut:
                continue
            if self.player is None:
                self.player = asyncio.create_task(self.play(text, item), name="tts->caller")
            text.put_nowait(delta)
        if self.player is None:
            return said
        text.put_nowait(None)
        try:
            await self.player
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise  # the call is ending, not a barge-in
        finally:
            if not self.player.done():
                self.player.cancel()
                await asyncio.gather(self.player, return_exceptions=True)
            self.player = None
        if self.cut:
            await self.tts.clear()
        return said

    async def play(self, text: asyncio.Queue[str | None], item: str) -> None:
        async def deltas() -> AsyncIterator[str]:
            while (delta := await text.get()) is not None:
                yield delta

        outbound = self.call.outbound
        slice_bytes = PLAYBACK_SLICE_MS * BYTES_PER_MS
        self.received_ms[item] = 0.0
        # aclosing: a barge-in's cancel closes the leg's utterance at once, not whenever it is collected.
        async with aclosing(self.tts.speak(deltas())) as audio:
            async for pcm in audio:
                self.received_ms[item] += pcm_ms(len(pcm))
                for i in range(0, len(pcm), slice_bytes):
                    ahead = outbound.sent_ms(item) - outbound.played_ms(item)
                    if ahead > PLAYBACK_LEAD_MS:
                        await asyncio.sleep((ahead - PLAYBACK_LEAD_MS) / 1000)
                    await self.playing.wait()  # held while caller speech pauses the reply
                    await outbound.send(pcm[i : i + slice_bytes], item)
        self.complete.add(item)

    def heard(self, said: str, item: str) -> str | None:
        """The part of an interrupted reply the caller can have heard, marked; None if nothing played."""
        played = self.call.outbound.played_ms(item)
        if played <= 0:
            return None
        words = said.split()
        total = self.received_ms.get(item, 0.0)
        if item in self.complete and total > 0:
            n = int(len(words) * min(1.0, played / total))  # the whole reply's length is known
        else:
            n = int(played // self.tts.ms_per_word)
        if n >= len(words):
            return said
        return " ".join(words[:n]) + INTERRUPTED_MARKER
