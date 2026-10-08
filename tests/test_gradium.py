"""Gradium (gradbot): config, tool calls answered once, the hang-up after the last words, offline.

The parity matrix (``test_parity.py``) covers the session gradbot is started
with and both call starts; these pin the bridge's own rules.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("gradbot")

from vamos_voice.providers.gradium import GradiumConfig, GradiumProvider

from .cascade_fakes import wait_until
from .fakes import FAKE_INBOUND, REMI, FakeLine, ScriptedSession, runtime
from .gradbot_fakes import FakeGradbot


def test_config_comes_from_the_environment() -> None:
    assert GradiumProvider.required_env == ("GRADIUM_API_KEY", "OPENAI_API_KEY")
    assert GradiumProvider.trace_adapter == "gradbot"
    config = GradiumProvider.from_env({"GRADIUM_API_KEY": "g", "OPENAI_API_KEY": "o"}).config
    assert (config.llm_model, config.voice_id) == ("gpt-4.1-mini", "4SZHfMpw-p46Ywgs")
    assert "g'" not in repr(config) and "o'" not in repr(config)
    custom = GradiumConfig.from_env(
        {
            "GRADIUM_API_KEY": "g",
            "OPENAI_API_KEY": "o",
            "VAMOS_GRADIUM_LLM_MODEL": "m",
            "VAMOS_GRADIUM_VOICE_ID": "v",
        }
    )
    assert (custom.llm_model, custom.voice_id) == ("m", "v")


async def start(monkeypatch: pytest.MonkeyPatch, case=FAKE_INBOUND):
    gradbot = FakeGradbot().install(monkeypatch)
    line, session = FakeLine(), ScriptedSession()
    call = runtime(case, line, session)
    task = asyncio.create_task(GradiumProvider(GradiumConfig("g", "o")).run_call(call))
    await gradbot.started.wait()
    return gradbot, call, line, session, task


async def test_gradbot_is_started_with_keys_model_and_pcm_both_ways(monkeypatch: pytest.MonkeyPatch) -> None:
    import gradbot as real

    gradbot, _, _, _, task = await start(monkeypatch, REMI)
    kwargs = dict(gradbot.kwargs)
    kwargs.pop("session_config")
    assert kwargs == {
        "gradium_api_key": "g",
        "llm_api_key": "o",
        "llm_model_name": "gpt-4.1-mini",
        "input_format": real.AudioFormat.Pcm,
        "output_format": real.AudioFormat.Pcm,
    }
    assert gradbot.config.voice_id == "4SZHfMpw-p46Ywgs"
    gradbot.end()
    await asyncio.wait_for(task, 3)
    assert gradbot.closed


async def test_a_reissued_call_is_answered_with_the_earlier_result_and_runs_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gradbot, _, _, session, task = await start(monkeypatch)
    args = json.dumps({"account_number": "ACME-1", "postal_code": "10001"})
    gradbot.event("push_to_llm")
    gradbot.event("llm_started")
    gradbot.tool_call("verify_caller", args, call_id="c1")
    gradbot.event("push_to_llm")  # gradbot re-prompts while the call is pending...
    gradbot.tool_call("verify_caller", args, call_id="c2")  # ...and the model asks again
    await wait_until(lambda: len(gradbot.answers) == 2)
    assert [c for c, _ in session.calls] == ["verify_caller"]
    assert {call_id for call_id, _ in gradbot.answers} == {"c1", "c2"}
    assert len({answer for _, answer in gradbot.answers}) == 1

    gradbot.event("push_to_llm")  # a prompt built after the result went back...
    gradbot.event("llm_started")
    gradbot.tool_call("verify_caller", args, call_id="c3")  # ...may repeat the call on purpose
    await wait_until(lambda: len(gradbot.answers) == 3)
    assert [c for c, _ in session.calls] == ["verify_caller", "verify_caller"]
    gradbot.end()
    await asyncio.wait_for(task, 3)


async def test_arguments_that_are_not_json_are_answered_without_running_the_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gradbot, _, _, session, task = await start(monkeypatch)
    gradbot.tool_call("verify_caller", '{"account', call_id="c1")
    await wait_until(lambda: gradbot.answers)
    assert session.calls == [] and "error" in json.loads(gradbot.answers[0][1])
    gradbot.end()
    await asyncio.wait_for(task, 3)


async def test_ending_the_call_waits_for_the_last_words_then_hangs_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gradbot, call, line, _, task = await start(monkeypatch)
    gradbot.tool_call("end_call", call_id="c1")
    await wait_until(lambda: gradbot.answers)
    for _ in range(3):  # the goodbye is still playing
        gradbot.audio()
        await asyncio.sleep(0.05)
    assert not line.closed
    gradbot.event("end_tts_audio")
    await asyncio.wait_for(task, 3)
    assert line.closed and call.hung_up and gradbot.closed


async def test_ending_the_call_with_nothing_left_to_say_hangs_up_after_a_quiet_second(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gradbot, _, line, _, task = await start(monkeypatch)
    gradbot.tool_call("end_call", call_id="c1")
    await asyncio.wait_for(task, 3)
    assert line.closed


async def test_the_caller_hanging_up_ends_the_session(monkeypatch: pytest.MonkeyPatch) -> None:
    gradbot, _, line, _, task = await start(monkeypatch)
    await line.incoming.put(None)
    await asyncio.wait_for(task, 3)
    assert gradbot.closed
