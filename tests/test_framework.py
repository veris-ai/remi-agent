"""The framework on its own: the boundary, discovery, the context, the gate engine, dispatch."""

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
    async def end_call(state: State, _: NoArgs) -> dict[str, Any]:
        """Hang up."""
        state.ended = True
        return {"ended": True}

    gates = GateLadder[State](
        level=lambda s: Level.VERIFIED if s.verified else Level.OPEN,
        explain=lambda s, needed: "Verify the caller first.",
    )
    state = State()
    return CallSession(state, registry, gates, connect_reads=("verify",)), state


async def test_dispatch_gates_redacts_and_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    session, state = tiny_session()
    assert session.missing_reads == ["verify"]
    assert await session.dispatch("balance", {}) == {"error": "Verify the caller first."}
    assert await session.dispatch("nope", {}) == {"error": "There is no tool named nope."}
    assert "invalid arguments" in (await session.dispatch("verify", {}))["error"]
    assert await session.dispatch("verify", {"pin": "9999"}) == {"error": "Wrong PIN."}
    assert session.missing_reads == ["verify"]
    assert await session.dispatch("verify", {"pin": "1234"}) == {"verified": True}
    assert session.missing_reads == []
    assert await session.dispatch("balance", {}) == {"balance": 10}
    assert "system problem" in (await session.dispatch("broken", {}))["error"]
    await session.dispatch("end_call", {})
    assert session.ended
    assert await session.dispatch("balance", {}) == {"error": "The call has ended."}
    await session.finalize()
    assert state.finalized == 1
    assert "1234" not in caplog.text and "9999" not in caplog.text and "[redacted]" in caplog.text
    assert session.tool_log[0] == "balance"


# --- the priming deadline ---------------------------------------------------------------


def test_priming_timeout_comes_from_the_environment() -> None:
    from vamos_voice.call_start import PRIMING_TIMEOUT_S
    from vamos_voice.server import priming_timeout_from_env

    assert priming_timeout_from_env({}) == PRIMING_TIMEOUT_S == 6.0
    assert priming_timeout_from_env({"VAMOS_PRIMING_TIMEOUT_S": "2.5"}) == 2.5
    for bad in ("0", "-1", "soon", "nan", "inf"):
        with pytest.raises(ConfigError, match="VAMOS_PRIMING_TIMEOUT_S"):
            priming_timeout_from_env({"VAMOS_PRIMING_TIMEOUT_S": bad})


async def test_a_finished_background_task_does_not_end_the_call() -> None:
    from vamos_voice.providers.wire import run_until_first_done

    order: list[str] = []

    async def background() -> None:
        order.append("background done")

    async def loop() -> None:
        await asyncio.sleep(0.05)
        order.append("loop done")

    await run_until_first_done(background(), main=loop())
    assert order == ["background done", "loop done"]


async def test_a_failing_background_task_fails_the_call() -> None:
    from vamos_voice.providers.wire import run_until_first_done

    async def background() -> None:
        raise RuntimeError("deadline handler broke")

    with pytest.raises(RuntimeError, match="deadline handler broke"):
        await run_until_first_done(background(), main=asyncio.sleep(5))


async def test_priming_deadline_abandons_only_when_priming_overruns() -> None:
    from vamos_voice.call_start import CallStart, callee_first

    policy = callee_first(("read",), "run the read")
    abandoned: list[bool] = []

    async def abandon() -> None:
        abandoned.append(True)

    on_time = CallStart(policy, priming_timeout_s=0.5)
    on_time.mark_primed()
    await on_time.priming_deadline(lambda: [], abandon)
    assert abandoned == []

    late = CallStart(policy, priming_timeout_s=0.05)
    await late.priming_deadline(lambda: ["read"], abandon)
    assert abandoned == [True]
