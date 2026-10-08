"""A stand-in for ``gradbot.run``: records the session it was asked for, replays scripted messages.

``FakeGradbot.install(monkeypatch)`` replaces ``gradbot.run`` (the attribute the
provider calls and the bench's trace adapter wraps). The test pushes
``MsgOut``-shaped messages with ``audio``/``stt``/``event``/``tool_call`` and
reads back the caller audio gradbot received and the tool answers it got.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

AUDIO_48K = b"\x01\x00" * 4800  # 100 ms at gradbot's 48 kHz output


class FakeHandle:
    def __init__(self, call_id: str, answers: list[tuple[str, str]]) -> None:
        self.call_id, self.answers = call_id, answers

    async def send(self, result_json: str) -> None:
        self.answers.append((self.call_id, result_json))

    async def send_error(self, error_message: str) -> None:
        self.answers.append((self.call_id, f"error: {error_message}"))


class FakeGradbot:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}
        self.caller_audio: list[bytes] = []
        self.closed = False
        self.answers: list[tuple[str, str]] = []
        self.outbox: asyncio.Queue[Any] = asyncio.Queue()
        self.started = asyncio.Event()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeGradbot:
        import gradbot

        monkeypatch.setattr(gradbot, "run", self.run)
        return self

    async def run(self, **kwargs: Any) -> tuple[Any, Any]:
        self.kwargs = kwargs
        self.started.set()
        return SimpleNamespace(send_audio=self._send_audio, close=self._close), SimpleNamespace(
            receive=self.outbox.get
        )

    async def _send_audio(self, data: bytes) -> None:
        self.caller_audio.append(data)

    async def _close(self) -> None:
        self.closed = True

    @property
    def config(self) -> Any:
        return self.kwargs["session_config"]

    def audio(self) -> None:
        self.outbox.put_nowait(SimpleNamespace(msg_type="audio", data=AUDIO_48K))

    def stt(self, text: str) -> None:
        self.outbox.put_nowait(SimpleNamespace(msg_type="stt_text", text=text, data=None))

    def event(self, kind: str) -> None:
        self.outbox.put_nowait(
            SimpleNamespace(msg_type="event", data=None, event=SimpleNamespace(event_type=kind))
        )

    def tool_call(self, name: str, args_json: str = "{}", call_id: str | None = None) -> None:
        call_id = call_id or f"call_{name}_{self.outbox.qsize()}"
        self.outbox.put_nowait(
            SimpleNamespace(
                msg_type="tool_call",
                data=None,
                tool_call=SimpleNamespace(call_id=call_id, tool_name=name, args_json=args_json),
                tool_call_handle=FakeHandle(call_id, self.answers),
            )
        )

    def end(self) -> None:
        self.outbox.put_nowait(None)
