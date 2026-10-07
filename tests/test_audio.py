"""The streaming resampler Gemini's input uses."""

from __future__ import annotations

import numpy as np

from vamos_voice.audio import Resampler, pcm_ms


def test_24k_to_16k_keeps_duration_and_shape_across_chunks() -> None:
    t = np.arange(24_000) / 24_000
    tone = (np.sin(2 * np.pi * 440 * t) * 10_000).astype("<i2").tobytes()
    resampler = Resampler(24_000, 16_000)
    out = b"".join(resampler.process(tone[i : i + 960]) for i in range(0, len(tone), 960))
    assert abs(pcm_ms(len(out), 16_000) - 1000) < 1
    samples = np.frombuffer(out, dtype="<i2").astype(float)
    expected = np.sin(2 * np.pi * 440 * np.arange(len(samples)) / 16_000) * 10_000
    assert np.max(np.abs(samples - expected)) < 400  # no clicks at chunk boundaries


def test_same_rate_is_a_pass_through() -> None:
    assert Resampler(24_000, 24_000).process(b"\x01\x02") == b"\x01\x02"


def test_caller_speech_tells_the_callers_voice_from_a_quieter_background() -> None:
    from vamos_voice.audio import CallerSpeech

    from .fakes import BACKGROUND, HELLO, tone

    meter = CallerSpeech()
    meter.feed(BACKGROUND)  # before the caller has spoken, the gate is open: VAD alone decides
    assert meter.caller_spoke(0, 1000)
    meter.feed(HELLO)
    assert meter.caller_spoke(1000, 1200)
    meter.feed(BACKGROUND)  # the TV again, now 30 dB under the voice heard
    assert meter.foreground_ms(1200) == 0 and not meter.caller_spoke(1200)
    meter.feed(tone(300, -25))  # a quieter caller is still the caller
    assert meter.caller_spoke(2200)
    assert meter.position_ms == 2500


def test_caller_speech_reads_frames_across_chunk_boundaries_and_ignores_a_click() -> None:
    from vamos_voice.audio import CallerSpeech

    from .fakes import HELLO, tone

    chunked = CallerSpeech()
    for i in range(0, len(HELLO), 1234):  # chunks that do not align with 20 ms frames
        chunked.feed(HELLO[i : i + 1234])
    assert chunked.position_ms == 200 and chunked.caller_spoke(0)

    clicked = CallerSpeech()
    clicked.feed(tone(20, -3) + tone(300, -28))  # one 20 ms click does not set the bar
    assert clicked.caller_spoke(20)
