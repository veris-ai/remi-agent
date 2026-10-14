"""GPT-Live: session.start, the delegated backend, and frame translation.

These pin that the whole configuration (with the agent's screen pop in the
instructions of both the live model and its backend) goes out in the one
``session.start``, that the backend's ``tool_choice`` stays ``auto`` (no read
is ever forced), and that tool runs are answered and continued.
"""

from __future__ import annotations

import asyncio

import pytest

from vamos_voice.providers.gpt_live import BACKEND_PREAMBLE, LIVE_PREAMBLE, GptLiveProvider, live_instructions
from vamos_voice.providers.wire import dispatch_json_call

from .fakes import (
    FAKE_INBOUND,
    HELLO,
    REMI,
    FakeLine,
    FakeVendorSocket,
    ScriptedSession,
    remi_screen_pop_runtime,
    runtime,
)
from .test_parity import GptLiveScript


def provider_on(sock: FakeVendorSocket) -> GptLiveProvider:
    provider = GptLiveProvider("sk-test", "gpt-live-1", "gpt-5.6-terra", "marin")
    provider.connect = sock.connector()
    return provider


def test_from_env_defaults_and_overrides() -> None:
    provider = GptLiveProvider.from_env({"OPENAI_API_KEY": "sk"})
    assert (provider.model, provider.backend_model, provider.voice) == (
        "gpt-live-1",
        "gpt-5.6-terra",
        "marin",
    )
    custom = GptLiveProvider.from_env(
        {
            "OPENAI_API_KEY": "sk",
            "VAMOS_GPT_LIVE_MODEL": "m",
            "VAMOS_GPT_LIVE_BACKEND_MODEL": "b",
            "VAMOS_GPT_LIVE_VOICE": "v",
        }
    )
    assert (custom.model, custom.backend_model, custom.voice) == ("m", "b", "v")


def test_session_start_puts_tools_on_the_backend() -> None:
    setup = REMI.setup()
    start = GptLiveProvider("sk", "gpt-live-1", "gpt-5.6-terra", "marin").session_start(setup)
    session = start["session"]
    assert start["type"] == "session.start"
    assert session["model"] == "gpt-live-1"
    assert session["instructions"] == live_instructions(setup)
    assert session["audio"] == {"format": {"type": "audio/pcm", "rate": 24_000}, "output": {"voice": "marin"}}
    assert "tools" not in session, "the live model runs no tools; they belong to the backend"
    backend = session["delegation"]["responses"]
    assert session["delegation"]["type"] == "responses"
    assert backend["model"] == "gpt-5.6-terra"
    assert backend["instructions"] == BACKEND_PREAMBLE + setup.instructions
    assert [t["name"] for t in backend["tools"]] == [t.name for t in setup.tools]
    assert backend["tool_choice"] == "auto"
    assert backend["parallel_tool_calls"] is False


def test_the_live_model_is_told_which_actions_the_backend_carries_out() -> None:
    """smoke trial_5jpr50dil1axg9b1unegn: given no list of the backend's tools, GPT-Live never delegated
    end_call (0 of 117 calls) and made no tool call at all in 16. Its guide: list them in the live prompt."""
    setup = REMI.setup()
    live = live_instructions(setup)
    assert live.startswith(LIVE_PREAMBLE) and live.endswith(setup.instructions)
    actions = live[len(LIVE_PREAMBLE) : live.index(setup.instructions)].strip().splitlines()
    assert [line.split(":")[0] for line in actions] == [f"- {t.name}" for t in setup.tools]
    assert all(line.count(". ") == 0 for line in actions), "one sentence per action, not its whole contract"
    assert "end_call" in live[: live.index(setup.instructions)]


async def test_nothing_but_session_start_goes_out_before_session_started() -> None:
    sock = FakeVendorSocket()
    line = FakeLine()
    task = asyncio.create_task(provider_on(sock).run_call(runtime(REMI, line, ScriptedSession())))
    await sock.wait_for(GptLiveScript.is_setup)
    await line.incoming.put(HELLO)
    await asyncio.sleep(0.05)
    assert [f["type"] for f in sock.sent] == ["session.start"]
    task.cancel()


async def test_an_error_before_session_started_fails_the_call() -> None:
    sock = FakeVendorSocket()
    sock.push({"type": "error", "error": {"message": "Voice session access denied"}})
    with pytest.raises(RuntimeError, match="access denied"):
        await provider_on(sock).run_call(runtime(REMI))


async def test_the_screen_pop_reaches_the_live_model_and_the_backend_and_nothing_is_forced() -> None:
    """trial_5sy9iim6ancmrxjd9pp0b: priming never got the reads to run on GPT-Live, so the model had no
    account and asked for the agency's callback contact. The screen pop is in session.start itself."""
    sock = FakeVendorSocket()
    script = GptLiveScript(sock)
    line = FakeLine()
    call = await remi_screen_pop_runtime([], line)
    task = asyncio.create_task(provider_on(sock).run_call(call))
    start = await sock.wait_for(script.is_setup)
    session = start["session"]
    assert "# Account on screen" in session["instructions"]
    assert "You are calling Marcus Sokolov" in session["instructions"]
    assert session["instructions"].startswith(LIVE_PREAMBLE)
    backend = session["delegation"]["responses"]["instructions"]
    assert backend.startswith(BACKEND_PREAMBLE) and session["instructions"].endswith(
        backend[len(BACKEND_PREAMBLE) :]
    )
    assert session["delegation"]["responses"]["tool_choice"] == "auto"

    script.ready()
    await line.incoming.put(HELLO)
    await sock.wait_for(script.is_caller_audio)
    assert [f["type"] for f in sock.sent] == ["session.start", "session.input_audio.append"], (
        "no instruction append, no tool_choice update: nothing primes the reads"
    )
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_a_tool_run_is_answered_and_continued() -> None:
    sock = FakeVendorSocket()
    script = GptLiveScript(sock)
    session = ScriptedSession()
    task = asyncio.create_task(provider_on(sock).run_call(runtime(FAKE_INBOUND, FakeLine(), session)))
    await sock.wait_for(script.is_setup)
    script.ready()
    await sock.wait_for(script.is_request)  # the opening turn
    script.tool_calls("verify_caller")
    await asyncio.sleep(0.05)
    assert sock.sent[-2] == {
        "type": "response.item.create",
        "item": {"type": "function_call_output", "call_id": "call_1_0", "output": '{"ok": "verify_caller"}'},
    }
    assert sock.sent[-1] == {"type": "response.create"}
    assert not any(f["type"] == "session.update" for f in sock.sent), "tool_choice is never touched"
    task.cancel()


async def test_end_call_closes_the_line_and_the_session() -> None:
    sock = FakeVendorSocket()
    script = GptLiveScript(sock)
    line = FakeLine()
    task = asyncio.create_task(provider_on(sock).run_call(runtime(FAKE_INBOUND, line, ScriptedSession())))
    await sock.wait_for(script.is_setup)
    script.ready()
    script.tool_calls("end_call")
    await asyncio.wait_for(task, 3)
    assert line.closed
    assert sock.sent[-1] == {"type": "session.close"}


async def test_cut_off_arguments_are_never_dispatched() -> None:
    session = ScriptedSession()
    assert await dispatch_json_call(session, "make_payment", '{"amount": 12') is None  # type: ignore[arg-type]
    assert session.calls == []
    assert await dispatch_json_call(session, "noop", "") == '{"ok": "noop"}'  # type: ignore[arg-type]
