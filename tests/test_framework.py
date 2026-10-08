"""The framework on its own: the boundary, discovery, the context, the gate engine, dispatch, the screen pop."""

from __future__ import annotations

import asyncio
import enum
import importlib
import logging
import pkgutil
from typing import Any

import pytest
from pydantic import BaseModel

import vamos_voice
from vamos_voice.context import AgentContext, ContextError
from vamos_voice.registry import AGENTS_PACKAGE, UnknownAgent, agent_names, load_agent
from vamos_voice.server import ConfigError, build_app_from_env
from vamos_voice.session import CallSession, GateLadder
from vamos_voice.tools import ToolError, ToolRegistry

from .guards import assert_no_wall_clock, imported_modules


def framework_modules() -> list[Any]:
    """Every vamos_voice module whose dependencies are installed (vendor extras may be absent)."""
    modules = [vamos_voice]
    for info in pkgutil.walk_packages(vamos_voice.__path__, "vamos_voice."):
        try:
            modules.append(importlib.import_module(info.name))
        except ImportError:
            continue
    return modules


def test_the_framework_never_imports_an_agent() -> None:
    for module in framework_modules():
        offenders = {name for name in imported_modules(module) if name.split(".")[0] == AGENTS_PACKAGE}
        assert not offenders, f"{module.__name__} imports {offenders}: agents are found by name at runtime"


def test_the_framework_never_reads_a_wall_clock() -> None:
    for module in framework_modules():
        assert_no_wall_clock(module)


def test_agents_are_found_by_name() -> None:
    assert "remi" in agent_names()
    assert load_agent("remi").name == "remi"
    with pytest.raises(UnknownAgent, match="no agent 'rory'"):
        load_agent("rory")


def test_startup_names_the_missing_agent_and_vendor() -> None:
    with pytest.raises(ConfigError, match="VAMOS_AGENT, VAMOS_VENDOR"):
        build_app_from_env({})
    with pytest.raises(ConfigError, match="no agent 'nobody'"):
        build_app_from_env({"VAMOS_AGENT": "nobody", "VAMOS_VENDOR": "openai_realtime"})


# --- the context -------------------------------------------------------------------------------


def test_context_is_a_string_map_and_absent_is_empty() -> None:
    assert len(AgentContext.from_json(None)) == 0
    assert len(AgentContext.from_json("")) == 0
    ctx = AgentContext.from_json('{"a": "1", "b": "2"}')
    assert dict(ctx) == {"a": "1", "b": "2"}
    assert ctx.require("b", "a") == ("2", "1")
    with pytest.raises(ContextError, match=r"lacks \['c'\]"):
        ctx.require("a", "c")


@pytest.mark.parametrize("raw", ["not json", "[]", '{"a": 1}', '{"a": {"b": "c"}}'])
def test_context_rejects_anything_but_a_string_object(raw: str) -> None:
    with pytest.raises(ContextError):
        AgentContext.from_json(raw)


# --- gates and dispatch --------------------------------------------------------------------------


class Level(enum.IntEnum):
    OPEN = 0
    VERIFIED = 1


class State:
    def __init__(self) -> None:
        self.verified = False
        self.ended = False
        self.finalized = 0
        self.release = asyncio.Event()
        self.order: list[str] = []

    async def finalize(self) -> None:
        self.finalized += 1


class Secret(BaseModel):
    pin: str


class NoArgs(BaseModel):
    pass


def tiny_session() -> tuple[CallSession, State]:
    registry = ToolRegistry()

    @registry.tool(Secret, redact=("pin",))
    async def verify(state: State, args: Secret) -> dict[str, Any]:
        """Verify the caller with their PIN."""
        if args.pin != "1234":
            raise ToolError("Wrong PIN.")
        state.verified = True
        return {"verified": True}

    @registry.tool(NoArgs, requires=Level.VERIFIED)
    async def balance(state: State, _: NoArgs) -> dict[str, Any]:
        """Read the balance."""
        return {"balance": 10}

    @registry.tool(NoArgs, requires=Level.VERIFIED)
    async def broken(state: State, _: NoArgs) -> dict[str, Any]:
        """Always fails."""
        raise RuntimeError("database fell over")

    @registry.tool(NoArgs)
    async def slow_write(state: State, _: NoArgs) -> dict[str, Any]:
        """A write that takes a while (a charge, a database transaction)."""
        await state.release.wait()
        state.order.append("slow_write")
        return {"written": True}

    @registry.tool(NoArgs)
    async def end_call(state: State, _: NoArgs) -> dict[str, Any]:
        """Hang up."""
        state.ended = True
        return {"ended": True}

    gates = GateLadder[State](
        level=lambda s: Level.VERIFIED if s.verified else Level.OPEN,
        explain=lambda s, needed: "Verify the caller first.",
    )
    state = State()
    return CallSession(state, registry, gates), state


async def test_dispatch_gates_redacts_and_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    session, state = tiny_session()
    assert await session.dispatch("balance", {}) == {"error": "Verify the caller first."}
    assert await session.dispatch("nope", {}) == {"error": "There is no tool named nope."}
    assert "invalid arguments" in (await session.dispatch("verify", {}))["error"]
    assert await session.dispatch("verify", {"pin": "9999"}) == {"error": "Wrong PIN."}
    assert await session.dispatch("verify", {"pin": "1234"}) == {"verified": True}
    assert await session.dispatch("balance", {}) == {"balance": 10}
    assert "system problem" in (await session.dispatch("broken", {}))["error"]
    await session.dispatch("end_call", {})
    assert session.ended
    assert await session.dispatch("balance", {}) == {"error": "The call has ended."}
    await session.finalize()
    assert state.finalized == 1
    assert "1234" not in caplog.text and "9999" not in caplog.text and "[redacted]" in caplog.text
    assert session.tool_log[0] == "balance"


async def test_a_caller_hang_up_finalizes_once_after_running_tools_and_refuses_new_ones() -> None:
    session, state = tiny_session()

    async def finalize() -> None:
        state.order.append("finalize")
        state.finalized += 1

    state.finalize = finalize  # type: ignore[method-assign]
    running = asyncio.create_task(session.dispatch("slow_write", {}))
    await asyncio.sleep(0)
    session.caller_gone()
    session.caller_gone()
    refused = await session.dispatch("verify", {"pin": "1234"})
    assert refused == {"error": "The caller has hung up; the call is over."}
    await asyncio.sleep(0.01)
    assert state.order == [], "finalize waits for the tool call already running"
    state.release.set()
    assert await running == {"written": True}
    await session.finalize()
    await session.finalize()
    assert state.order == ["slow_write", "finalize"] and state.finalized == 1


async def test_a_failed_finalize_is_logged_even_if_nobody_awaits_it(caplog: pytest.LogCaptureFixture) -> None:
    session, state = tiny_session()

    async def broken() -> None:
        raise RuntimeError("world unreachable")

    state.finalize = broken  # type: ignore[method-assign]
    session.caller_gone()
    await asyncio.sleep(0.01)
    assert "could not be finalized" in caplog.text


# --- the screen pop ------------------------------------------------------------------------------


class PoppingAgent:
    """Just enough of an ``AgentSpec`` for the screen pop: it reads, records the read, renders it."""

    name = "popper"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def screen_pop(self, call: State) -> Any:
        from vamos_voice.screen_pop import ScreenPop

        if self.fail:
            raise RuntimeError("world unreachable")
        call.verified = True  # the state records the reads, so the gates see them done
        return ScreenPop("Caller on screen", "Account ACME-1, verified by caller ID.")


async def test_the_screen_pop_is_appended_to_the_instructions_and_marks_the_state() -> None:
    from vamos_voice.screen_pop import run_screen_pop, with_screen_pop

    session, state = tiny_session()
    pop = await run_screen_pop(PoppingAgent(), state)  # type: ignore[arg-type]
    assert state.verified and await session.dispatch("balance", {}) == {"balance": 10}
    assert with_screen_pop("Be brief.\n", pop) == (
        "Be brief.\n\n# Caller on screen\n\nAccount ACME-1, verified by caller ID.\n"
    )
    assert with_screen_pop("Be brief.", None) == "Be brief."


async def test_a_failed_screen_pop_is_logged_and_the_call_goes_on(caplog: pytest.LogCaptureFixture) -> None:
    from vamos_voice.screen_pop import run_screen_pop

    session, state = tiny_session()
    assert await run_screen_pop(PoppingAgent(fail=True), state) is None  # type: ignore[arg-type]
    assert "screen pop failed for popper" in caplog.text and "world unreachable" in caplog.text
    assert await session.dispatch("balance", {}) == {"error": "Verify the caller first."}


async def test_an_agent_without_a_screen_pop_shows_nothing() -> None:
    from vamos_voice.context import CallClock

    from .fakes import FAKE_INBOUND

    agent = FAKE_INBOUND.agent()
    assert await agent.screen_pop(object()) is None  # type: ignore[arg-type]
    assert FAKE_INBOUND.setup().instructions == agent.instructions(CallClock(agent.call_time()))
