"""``OutboundAudio``: the agent's playback buffer, the same for every vendor that writes the line.

The bench's ``voice_ws`` caller plays every byte of agent audio it receives and
has no message that clears its buffer, so what is written is what is heard.
Vendors stream faster than speech plays; these tests pin that ``OutboundAudio``
plays at speaking speed: at most the lead ahead of real time, in order, never
blocking the vendor's event loop. The agent's memory must hold exactly what the
caller heard, so ``interrupted()`` drops nothing unless asked
(``drop_unsent=True``, for a provider that then cuts the vendor's context to
``heard_ms``).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from vamos_voice.call_start import CallStart, callee_first
from vamos_voice.providers import base
from vamos_voice.providers.base import PLAYBACK_LEAD_S, CallRuntime, OutboundAudio

from .fakes import FakeLine

PIECE_MS = 100
PUMP = "agent audio -> caller"


def piece(n: int = 0, ms: int = PIECE_MS) -> bytes:
    """``ms`` of PCM16 at 24 kHz, tagged by its byte so the order on the line can be read back."""
    return bytes([n % 250 + 1, 0]) * 24 * ms


class Clock:
    """Monotonic time the test controls; ``sleep`` advances it instead of waiting."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += max(0.0, seconds)
        await asyncio.sleep(0)


class TimedLine(FakeLine):
    """Records when (on the test's clock) each piece reached the caller."""

    def __init__(self, clock: Clock) -> None:
        super().__init__()
        self.clock, self.at = clock, []

    async def send(self, pcm: bytes) -> None:
        await super().send(pcm)
        self.at.append(self.clock.t)


def outbound(clock: Clock | None = None) -> tuple[OutboundAudio, TimedLine, Clock]:
    clock = clock or Clock()
    line = TimedLine(clock)
    start = CallStart(callee_first())
    start.mark_callee_spoke()
    return OutboundAudio(line, start, monotonic=clock, sleep=clock.sleep), line, clock  # type: ignore[arg-type]


def pumps() -> list[asyncio.Task]:
    return [t for t in asyncio.all_tasks() if t.get_name() == PUMP and not t.done()]


async def test_with_nothing_queued_and_the_line_within_the_lead_audio_goes_out_at_once() -> None:
    out, line, _ = outbound()
    assert await out.send(piece(1), "a") is True
    assert line.sent == [piece(1)] and not pumps()
    assert out.sent_ms("a") == out.heard_ms("a") == PIECE_MS
    assert out.played_ms("a") == 0, "written, not played yet"


async def test_the_line_held_for_the_callee_drops_audio() -> None:
    clock = Clock()
    line = TimedLine(clock)
    out = OutboundAudio(line, CallStart(callee_first()), monotonic=clock, sleep=clock.sleep)  # type: ignore[arg-type]
    assert await out.send(piece(1), "a") is False
    assert line.sent == [] and out.sent_ms("a") == 0 and out.dropped_bytes == len(piece(1))


async def test_audio_streamed_faster_than_real_time_leaves_at_speaking_pace_in_order() -> None:
    out, line, clock = outbound()
    sent = [(piece(i), "a" if i < 20 else "b") for i in range(30)]  # 3 s, two items, all at once
    for pcm, item in sent:
        assert await out.send(pcm, item) is True  # accepted at once: send never waits for playout
    assert clock.t == 0.0
    assert PLAYBACK_LEAD_S * 1000 <= sum(map(len, line.sent)) / 48 <= PLAYBACK_LEAD_S * 1000 + PIECE_MS
    assert out.sent_ms("a") == 2000 and out.heard_ms("a") < 2000, "the rest waits here, not at the caller"

    await out.drain()
    assert line.sent == [pcm for pcm, _ in sent], "every piece, once, in the order it came"
    for i, at in enumerate(line.at):
        ahead_s = i * PIECE_MS / 1000 - at  # audio already on the line minus time gone by
        assert ahead_s <= PLAYBACK_LEAD_S + 1e-9, f"piece {i} went out {ahead_s:.2f} s ahead"
    assert line.at[-1] == pytest.approx(3.0 - PIECE_MS / 1000 - PLAYBACK_LEAD_S)
    assert clock.t == pytest.approx(3.0), "drain waits for the queue to be written, then to play"
    clock.t += 0.001
    assert (out.heard_ms("a"), out.heard_ms("b"), out.played_ms("b")) == (2000, 1000, 1000)


async def test_an_interruption_drops_nothing_unless_asked() -> None:
    """A vendor that keeps in its context everything it sent has all of it heard."""
    out, line, _clock = outbound()
    sent = [piece(i) for i in range(30)]
    for pcm in sent:
        await out.send(pcm, "a")
    assert out.heard_ms("a") < 3000, "some of it is still queued"
    assert out.interrupted() == {}
    assert out.sent_ms("a") == 3000, "nothing was dropped"
    await out.drain()
    assert line.sent == sent and out.heard_ms("a") == 3000, "all of it reached the caller"


async def test_drop_unsent_drops_only_what_was_not_written_and_the_written_audio_plays_on() -> None:
    out, line, clock = outbound()
    for i in range(30):
        await out.send(piece(i), "a")
    written = out.heard_ms("a")
    assert 1000 <= written < 3000

    assert out.interrupted(drop_unsent=True) == {"a": 3000 - written}
    assert out.sent_ms("a") == out.heard_ms("a") == written, "accepted falls back to written"
    assert out.interrupted(drop_unsent=True) == {}, "nothing left to drop"
    await asyncio.sleep(0.01)
    assert sum(map(len, line.sent)) / 48 == written, "the dropped audio never reached the caller"
    assert not pumps()

    clock.t = 0.05
    assert out.played_ms("a") == 50, "the playout clock keeps running"
    await out.send(piece(99), "b")  # the next reply waits behind what the caller is still hearing
    assert line.sent[-1] != piece(99)
    await out.drain()
    assert line.sent[-1] == piece(99) and line.at[-1] == pytest.approx(written / 1000 - PLAYBACK_LEAD_S)


def real_outbound() -> tuple[OutboundAudio, FakeLine]:
    """On the real clock: the pump really waits."""
    line = FakeLine()
    start = CallStart(callee_first())
    start.mark_callee_spoke()
    return OutboundAudio(line, start), line  # type: ignore[arg-type]


async def test_the_pump_stops_and_drops_the_queue_once_the_line_closes() -> None:
    out, line = real_outbound()
    for i in range(30):
        await out.send(piece(i), "a")
    await asyncio.sleep(0.15)  # the pump writes a piece every 100 ms
    assert pumps()
    await line.close()
    await asyncio.sleep(0.15)
    assert not pumps()
    written = out.heard_ms("a")
    assert 1100 < written < 1500
    assert sum(map(len, line.sent)) / 48 == written == out.sent_ms("a"), "the rest was dropped"


async def test_close_stops_a_waiting_pump_at_once() -> None:
    out, _ = real_outbound()
    for i in range(30):
        await out.send(piece(i), "a")
    await asyncio.sleep(0)
    assert pumps()
    await out.close()
    assert not pumps() and out.sent_ms("a") == out.heard_ms("a")


async def test_drain_never_waits_longer_than_max_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "MAX_DRAIN_S", 0.2)
    out, _ = real_outbound()
    for i in range(50):
        await out.send(piece(i), "a")  # 5 s
    loop = asyncio.get_running_loop()
    began = loop.time()
    await out.drain()
    assert loop.time() - began < 0.4
    await out.close()


async def test_call_hang_up_applies_the_agent_grace_after_playout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Audio:
        async def drain(self) -> None:
            events.append("drained")

    class Line:
        async def close(self) -> None:
            events.append("closed")

    async def sleep(seconds: float) -> None:
        events.append(f"grace {seconds}")

    monkeypatch.setattr(base.asyncio, "sleep", sleep)
    session = SimpleNamespace(state=SimpleNamespace(hang_up_grace_s=2.5))
    call = CallRuntime(Line(), session, None, None, None, Audio())  # type: ignore[arg-type]
    await call.hang_up()
    assert events == ["drained", "grace 2.5", "closed"]
