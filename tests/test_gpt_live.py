"""GPT-Live: session.start, the forced priming through the delegated backend, and frame translation.

GPT-Live has no forced tool choice on the live model but does on its
Responses backend, which ``session.update`` may change mid-session; these
tests pin that the provider uses it to force each connect read, then hands
the backend back to ``auto`` before the caller is heard.
"""

from __future__ import annotations

import asyncio

import pytest

from vamos_voice.providers.gpt_live import BACKEND_PREAMBLE, GptLiveProvider
from vamos_voice.providers.wire import dispatch_json_call

from .fakes import FAKE_INBOUND, HELLO, REMI, FakeLine, FakeVendorSocket, ScriptedSession, runtime
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


@pytest.mark.parametrize("force", [True, False])
def test_session_start_puts_tools_on_the_backend(force: bool) -> None:
    setup = REMI.setup()
    start = GptLiveProvider("sk", "gpt-live-1", "gpt-5.6-terra", "marin").session_start(
        setup, force_tools=force
    )
    session = start["session"]
    assert start["type"] == "session.start"
    assert session["model"] == "gpt-live-1"
    assert session["instructions"] == setup.instructions
    assert session["audio"] == {"format": {"type": "audio/pcm", "rate": 24_000}, "output": {"voice": "marin"}}
    assert "tools" not in session, "the live model runs no tools; they belong to the backend"
    backend = session["delegation"]["responses"]
    assert session["delegation"]["type"] == "responses"
    assert backend["model"] == "gpt-5.6-terra"
    assert backend["instructions"] == BACKEND_PREAMBLE + setup.instructions
    assert [t["name"] for t in backend["tools"]] == [t.name for t in setup.tools]
    assert backend["tool_choice"] == ("required" if force else "auto")
    assert backend["parallel_tool_calls"] is False


async def test_nothing_but_session_start_goes_out_before_session_started() -> None:
    sock = FakeVendorSocket()
    line = FakeLine()
    task = asyncio.create_task(
        provider_on(sock).run_call(runtime(REMI, line, ScriptedSession(REMI.policy.connect_reads)))
    )
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


async def test_priming_forces_each_missing_read_on_the_backend_then_returns_to_auto() -> None:
    reads = REMI.policy.connect_reads
    sock = FakeVendorSocket()
    script = GptLiveScript(sock)
    line, session = FakeLine(), ScriptedSession(reads)
    call = runtime(REMI, line, session)
    task = asyncio.create_task(provider_on(sock).run_call(call))
    await sock.wait_for(script.is_setup)
    script.ready()
    await line.incoming.put(HELLO)
    await sock.wait_for(script.is_prime)

    for read in reads:  # the backend runs one call per run (parallel_tool_calls off)
        script.tool_calls(read)
        await asyncio.sleep(0.02)
    await sock.wait_for(script.is_caller_audio)
    assert call.start.primed and [c for c, _ in session.calls] == list(reads)

    kinds = [f["type"] for f in sock.sent]
    assert kinds[:2] == ["session.start", "session.instructions.append"]
    choices = [
        f["session"]["delegation"]["responses"]["tool_choice"]
        for f in sock.sent
        if f["type"] == "session.update"
    ]
    assert choices == [{"type": "function", "name": reads[1]}, {"type": "function", "name": reads[2]}, "auto"]
    outputs = [f["item"]["call_id"] for f in sock.sent if f["type"] == "response.item.create"]
    assert outputs == ["call_1_0", "call_2_0", "call_3_0"]
    # Every answered run is continued, and the caller's audio follows the last read.
    assert kinds.count("response.create") == 3
    assert kinds.index("session.input_audio.append") > max(
        i for i, k in enumerate(kinds) if k == "session.update"
    )
    task.cancel()


async def test_a_run_without_calls_while_priming_asks_the_live_model_again() -> None:
    reads = REMI.policy.connect_reads
    sock = FakeVendorSocket()
    script = GptLiveScript(sock)
    task = asyncio.create_task(provider_on(sock).run_call(runtime(REMI, FakeLine(), ScriptedSession(reads))))
    await sock.wait_for(script.is_setup)
    script.ready()
    await sock.wait_for(script.is_prime)
    script.tool_calls()  # a backend run that called nothing
    await asyncio.sleep(0.05)
    appends = [f["content"] for f in sock.sent if f["type"] == "session.instructions.append"]
    assert appends == [REMI.policy.prime_instruction, REMI.policy.reprime_instruction(list(reads))]
    task.cancel()


async def test_after_priming_a_tool_run_is_answered_and_continued() -> None:
    sock = FakeVendorSocket()
    script = GptLiveScript(sock)
    session = ScriptedSession(())
    task = asyncio.create_task(provider_on(sock).run_call(runtime(FAKE_INBOUND, FakeLine(), session)))
    await sock.wait_for(script.is_setup)
    script.ready()
    await sock.wait_for(script.is_prime)
    script.tool_calls("verify_caller")
    await asyncio.sleep(0.05)
    assert sock.sent[-2] == {
        "type": "response.item.create",
        "item": {"type": "function_call_output", "call_id": "call_1_0", "output": '{"ok": "verify_caller"}'},
    }
    assert sock.sent[-1] == {"type": "response.create"}
    assert not any(f["type"] == "session.update" for f in sock.sent), (
        "no priming: tool_choice is never touched"
    )
    task.cancel()


async def test_end_call_closes_the_line_and_the_session() -> None:
    sock = FakeVendorSocket()
    script = GptLiveScript(sock)
    line = FakeLine()
    task = asyncio.create_task(provider_on(sock).run_call(runtime(FAKE_INBOUND, line, ScriptedSession(()))))
    await sock.wait_for(script.is_setup)
    script.ready()
    script.tool_calls("end_call")
    await asyncio.wait_for(task, 3)
    assert line.closed
    assert sock.sent[-1] == {"type": "session.close"}


async def test_cut_off_arguments_are_never_dispatched() -> None:
    session = ScriptedSession(())
    assert await dispatch_json_call(session, "make_payment", '{"amount": 12') is None  # type: ignore[arg-type]
    assert session.calls == []
    assert await dispatch_json_call(session, "noop", "") == '{"ok": "noop"}'  # type: ignore[arg-type]
