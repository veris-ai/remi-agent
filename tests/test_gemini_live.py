"""Gemini Live: tool calls the vendor cancels, against what the provider sends and logs.

The parity matrix (``test_parity.py``) covers the call as a whole; this pins
``toolCallCancellation``: Gemini sends it when caller speech interrupts a turn
whose tool calls are pending, by which time they have run and been answered.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from .fakes import REMI, FakeLine, FakeVendorSocket, ScriptedSession, runtime
from .test_parity import GeminiScript, gemini_call

CARD = "4242424242424242"


class RefusingSession(ScriptedSession):
    """``verify_caller`` comes back as a refusal, everything else as in ``ScriptedSession``."""

    async def dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        result = await super().dispatch(name, args)
        return {"error": "not verified"} if name == "verify_caller" else result


def tool_responses(sock: FakeVendorSocket) -> list[dict[str, Any]]:
    return [r for f in sock.sent if "toolResponse" in f for r in f["toolResponse"]["functionResponses"]]


async def test_a_cancelled_call_that_already_ran_is_logged_and_its_true_result_stands(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The caller says "ok" while take_payment runs: the charge is made and answered, then Gemini
    cancels it. Nothing is undone, skipped or re-sent; the log says which calls ran, never their args."""
    caplog.set_level(logging.WARNING, logger="vamos_voice.providers.gemini_live")
    sock = FakeVendorSocket()
    script = GeminiScript(sock)
    line, session = FakeLine(), RefusingSession()
    task = asyncio.create_task(gemini_call(sock)["provider"].run_call(runtime(REMI, line, session)))
    await sock.wait_for(GeminiScript.is_setup)
    script.ready()

    calls = [
        {"id": "fc_pay", "name": "take_payment", "args": {"card_number": CARD}},
        {"id": "fc_verify", "name": "verify_caller", "args": {}},
    ]
    sock.push({"toolCall": {"functionCalls": calls}})
    await sock.wait_for(lambda f: "toolResponse" in f)
    sock.push({"serverContent": {"interrupted": True}})
    sock.push({"toolCallCancellation": {"ids": ["fc_pay", "fc_verify", "fc_unseen"]}})
    script.tool_calls("end_call")  # the call goes on after the cancellation
    await asyncio.wait_for(task, 3)

    assert [c for c, _ in session.calls] == ["take_payment", "verify_caller", "end_call"]
    assert tool_responses(sock)[:2] == [
        {"id": "fc_pay", "name": "take_payment", "response": {"ok": "take_payment"}},
        {"id": "fc_verify", "name": "verify_caller", "response": {"error": "not verified"}},
    ], "each call's true result, sent once"
    assert [r["id"] for r in tool_responses(sock)] == ["fc_pay", "fc_verify", "fc_0_0"]
    assert line.closed

    (warning,) = [r for r in caplog.records if "toolCallCancellation" in r.getMessage()]
    assert warning.levelno == logging.WARNING
    message = warning.getMessage()
    assert "fc_pay (take_payment: already run and answered with a result)" in message
    assert "fc_verify (verify_caller: already run and answered with an error)" in message
    assert "fc_unseen (not run by this call)" in message
    assert CARD not in caplog.text and "not verified" not in caplog.text, "no arguments or results"
