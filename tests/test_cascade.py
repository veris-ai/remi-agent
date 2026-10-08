"""The shared in-pod cascade (``vamos_voice.providers.cascade``) with scripted legs.

What every cascade vendor (Mistral, Hugging Face, Grok STT + TTS) inherits:
the model runs only on a caller turn (callee first) or opens at once (agent
first); each round is spoken, then its tools run through the session; a tool
that ends the call hangs up after the last words; a barge-in silences the turn
but lets the round's tool calls run, and trims the history to what was heard;
playout runs at most a second ahead of real time. Plus Silero and the local
silence endpoint the legs without vendor endpointing use.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from vamos_voice.providers.cascade import (
    BAD_ARGUMENTS,
    INTERRUPTED_MARKER,
    PLAYBACK_LEAD_MS,
    PLAYBACK_SLICE_MS,
    UTTERANCE_GAP_MS,
    BargeInDetector,
    BargeInRule,
    SilenceEndpoint,
)

from .cascade_fakes import SILENCE, FakeLLM, FakeSTT, FakeTTS, ScriptedCascade, tool, wait_until
from .fakes import FAKE_INBOUND, HELLO, REMI, FakeLine, ScriptedSession, runtime

SPEECH = Path(__file__).parent / "fixtures" / "caller_speech_24k.pcm"


async def start(provider: ScriptedCascade, case=REMI):
    line, session = FakeLine(), ScriptedSession()
    call = runtime(case, line, session)
    task = asyncio.create_task(provider.run_call(call))
    return call, line, session, task


async def stop(task: asyncio.Task) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_callee_first_the_model_waits_for_the_callees_first_turn() -> None:
    llm = FakeLLM(["Hi, this is Remi. May I speak with Marcus?"])
    provider = ScriptedCascade(llm)
    call, line, _, task = await start(provider)

    await line.incoming.put(HELLO)  # the callee's "Hello?" goes to STT at once
    await wait_until(lambda: provider.stt.fed)
    await asyncio.sleep(0.05)
    assert llm.requests == [] and not call.start.callee_spoke

    await provider.stt.turns.put("Hello?")
    await wait_until(lambda: line.sent)
    assert call.start.callee_spoke
    ((messages, tools),) = llm.requests
    assert messages == [
        {"role": "system", "content": REMI.setup().instructions},
        {"role": "user", "content": "Hello?"},
    ]
    assert {t["function"]["name"] for t in tools} == set(REMI.expected_tools())
    assert provider.tts.spoken == ["Hi, this is Remi. May I speak with Marcus?"]
    await stop(task)


async def test_agent_first_the_opening_round_runs_before_the_caller_says_anything() -> None:
    llm = FakeLLM(["Thanks for calling Acme Energy."])
    provider = ScriptedCascade(llm)
    _, line, _, task = await start(provider, FAKE_INBOUND)
    await wait_until(lambda: line.sent)
    ((messages, _),) = llm.requests
    assert messages == [
        {"role": "system", "content": FAKE_INBOUND.setup().instructions},
        {"role": "system", "content": FAKE_INBOUND.policy.opening_instruction},
    ]
    await stop(task)


async def test_a_round_is_spoken_then_its_tools_run_and_the_next_round_sees_the_results() -> None:
    llm = FakeLLM(["Let me check.", tool("get_account")], ["You owe nothing."])
    provider = ScriptedCascade(llm)
    _, _, session, task = await start(provider, FAKE_INBOUND)
    await wait_until(lambda: len(llm.requests) == 2)
    await wait_until(lambda: len(provider.tts.spoken) == 2)
    assert session.calls == [("get_account", {})]
    assert provider.tts.spoken == ["Let me check.", "You owe nothing."]
    second_request = llm.requests[1][0]
    assert second_request[-2] == {
        "role": "assistant",
        "content": "Let me check.",
        "tool_calls": [
            {
                "id": "call_get_account",
                "type": "function",
                "function": {"name": "get_account", "arguments": "{}"},
            }
        ],
    }
    assert second_request[-1] == {
        "role": "tool",
        "tool_call_id": "call_get_account",
        "name": "get_account",
        "content": json.dumps({"ok": "get_account"}),
    }
    await stop(task)


async def test_arguments_that_are_not_json_are_answered_without_running_the_tool() -> None:
    llm = FakeLLM([tool("verify_caller", '{"account_number": "AC')], ["Sorry?"])
    provider = ScriptedCascade(llm)
    _, _, session, task = await start(provider, FAKE_INBOUND)
    await wait_until(lambda: len(llm.requests) == 2)
    assert session.calls == []
    assert llm.requests[1][0][-1]["content"] == BAD_ARGUMENTS
    await stop(task)


async def test_a_tool_that_ends_the_call_hangs_up_after_the_last_words_and_asks_nothing_more() -> None:
    llm = FakeLLM(["Goodbye.", tool("end_call")])
    provider = ScriptedCascade(llm)
    call, line, session, task = await start(provider)
    await provider.stt.turns.put("I have to go.")
    await asyncio.wait_for(task, 3)
    assert session.calls == [("end_call", {})]
    assert line.closed and call.hung_up
    assert len(llm.requests) == 1
    assert provider.tts.spoken == ["Goodbye."]


async def test_turns_that_finish_while_the_agent_answers_are_answered_together() -> None:
    gate = asyncio.Event()

    class SlowLLM(FakeLLM):
        async def reply(self, messages, tools):
            self.requests.append(([dict(m) for m in messages], tools))
            if len(self.requests) == 1:
                await gate.wait()
            yield "Sure."

    llm = SlowLLM()
    provider = ScriptedCascade(llm)
    _, _, _, task = await start(provider)
    await provider.stt.turns.put("Hello?")
    await wait_until(lambda: llm.requests)
    await provider.stt.turns.put("Who is this?")
    await provider.stt.turns.put("Hello??")
    await asyncio.sleep(0.05)
    gate.set()
    await wait_until(lambda: len(llm.requests) == 2)
    assert llm.requests[1][0][-1] == {"role": "user", "content": "Who is this? Hello??"}
    await stop(task)


async def test_barge_in_silences_the_turn_runs_its_tools_and_keeps_only_what_was_heard() -> None:
    reply = " ".join(f"word{i}" for i in range(40))  # 4 s of audio at the fake's 100 ms a word
    llm = FakeLLM([reply, tool("get_account")], ["Never asked for."])
    provider = ScriptedCascade(llm, tts=FakeTTS(chunk_delay_s=0.01))
    call, line, session, task = await start(provider, FAKE_INBOUND)
    await wait_until(lambda: line.sent)
    await asyncio.sleep(0.3)  # the caller hears ~0.3 s of the reply...
    for _ in range(3):  # ...and talks over it for 0.6 s
        await line.incoming.put(HELLO)
    await wait_until(lambda: session.calls)
    await asyncio.sleep(0.1)

    assert session.calls == [("get_account", {})], "the round's tool call still runs"
    assert len(llm.requests) == 1, "the turn ends: no round after the barge-in"
    assert provider.tts.clears == 1
    assert call.outbound.sent_ms("reply-1") < 4000, "the rest of the reply never went out"
    await provider.stt.turns.put("Wait, what?")
    await wait_until(lambda: len(llm.requests) == 2)
    assistant = llm.requests[1][0][-3]
    assert (
        assistant["role"] == "assistant" and assistant["tool_calls"][0]["function"]["name"] == "get_account"
    )
    assert assistant["content"].endswith(INTERRUPTED_MARKER)
    kept = assistant["content"].removesuffix(INTERRUPTED_MARKER).split()
    assert 1 <= len(kept) < 15, kept
    await stop(task)


async def test_a_blip_of_caller_speech_does_not_interrupt_the_reply() -> None:
    reply = " ".join(f"word{i}" for i in range(10))
    llm = FakeLLM([reply])
    provider = ScriptedCascade(llm, tts=FakeTTS(chunk_delay_s=0.01))
    call, line, _, task = await start(provider, FAKE_INBOUND)
    await wait_until(lambda: line.sent)
    await line.incoming.put(HELLO)  # 0.2 s: a cough, a noise burst
    await line.incoming.put(SILENCE * 20)  # 0.4 s gap: a new utterance next
    await line.incoming.put(HELLO)
    await wait_until(lambda: call.outbound.sent_ms("reply-1") >= 1000)
    assert provider.tts.clears == 0
    await stop(task)


async def test_with_interim_text_barge_in_also_needs_a_transcribed_word() -> None:
    reply = " ".join(f"word{i}" for i in range(40))
    llm = FakeLLM([reply])
    provider = ScriptedCascade(llm, stt=FakeSTT(interim_text=True), tts=FakeTTS(chunk_delay_s=0.01))
    call, line, _, task = await start(provider, FAKE_INBOUND)
    await wait_until(lambda: line.sent)
    for _ in range(4):  # 0.8 s of speech the STT hears no words in (background noise)
        await line.incoming.put(HELLO)
    await wait_until(lambda: len(provider.stt.fed) >= 4)
    await asyncio.sleep(0.05)
    assert provider.tts.clears == 0
    provider.stt.on_interim("wait")
    await wait_until(lambda: provider.tts.clears == 1)
    assert call.outbound.sent_ms("reply-1") < 4000
    await stop(task)


def test_barge_in_rule_defaults_to_half_a_second_and_one_word() -> None:
    assert BargeInRule() == BargeInRule(min_speech_ms=500, min_words=1)
    assert ScriptedCascade.barge_in_rule == BargeInRule()


def test_barge_in_detector_counts_one_utterance_from_the_reply_start() -> None:
    words = BargeInDetector(BargeInRule(), needs_words=True)
    assert not words.audio(480, speaking=True)
    assert not words.audio(UTTERANCE_GAP_MS - 20, speaking=False)  # a pause between words
    assert not words.audio(20, speaking=True), "500 ms of speech, but no word yet"
    assert words.interim("hold on")
    words.reset()  # the next reply
    assert not words.interim("hold") and words.speech_ms == 0

    duration = BargeInDetector(BargeInRule(), needs_words=False)
    assert not duration.audio(400, speaking=True)
    assert not duration.audio(UTTERANCE_GAP_MS, speaking=False)  # the utterance ended
    assert not duration.audio(400, speaking=True), "two short blips are not 0.5 s of speech"
    assert duration.audio(100, speaking=True)


async def test_playout_runs_at_most_a_second_ahead_of_real_time() -> None:
    llm = FakeLLM([" ".join(["word"] * 50)])  # 5 s of audio, synthesized at once
    provider = ScriptedCascade(llm)
    call, line, _, task = await start(provider, FAKE_INBOUND)
    await wait_until(lambda: line.sent)
    await asyncio.sleep(0.4)
    ahead = call.outbound.sent_ms("reply-1") - call.outbound.played_ms("reply-1")
    assert ahead <= PLAYBACK_LEAD_MS + PLAYBACK_SLICE_MS
    assert call.outbound.sent_ms("reply-1") < 5000
    await stop(task)


async def test_the_caller_hanging_up_ends_the_call() -> None:
    provider = ScriptedCascade(FakeLLM())
    _, line, _, task = await start(provider)
    await line.incoming.put(None)
    await asyncio.wait_for(task, 3)


def test_silence_endpoint_counts_audio_time_after_speech() -> None:
    endpoint = SilenceEndpoint(800)
    frame = len(SILENCE)  # 20 ms
    assert not endpoint.push(frame, speaking=False), "silence before any speech is no turn"
    assert not endpoint.push(frame, speaking=True)
    ends = [endpoint.push(frame, speaking=False) for _ in range(40)]
    assert ends == [False] * 39 + [True]
    assert not endpoint.push(frame, speaking=False) and not endpoint.in_turn


def test_silero_hears_speech_and_not_silence() -> None:
    pytest.importorskip("onnxruntime")
    from vamos_voice.providers.silero import SileroVad

    vad = SileroVad()
    assert not any(vad.push(SILENCE) for _ in range(50))
    speech = SPEECH.read_bytes()
    verdicts = [vad.push(speech[i : i + 960]) for i in range(0, len(speech), 960)]
    assert sum(verdicts) > len(verdicts) // 3
    after = [vad.push(SILENCE) for _ in range(50)]
    assert not after[-1], "speech ends once the caller stops"
