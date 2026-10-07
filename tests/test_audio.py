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
