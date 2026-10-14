"""The band-limited streaming resampler every off-rate vendor uses (Gemini, Mistral, Fluxions,
Silero VAD and Hugging Face in; Gradium and Hugging Face TTS out)."""

from __future__ import annotations

import numpy as np
import pytest

from vamos_voice.audio import Resampler, pcm_ms, resample

# (source, destination) pairs the providers use, plus a TTS rate that does not divide 24 kHz.
RATES = [(24_000, 16_000), (48_000, 24_000), (16_000, 24_000), (22_050, 24_000)]


def _tone(hz: float, rate_hz: int, seconds: float = 1.0, amplitude: float = 10_000) -> bytes:
    t = np.arange(int(rate_hz * seconds)) / rate_hz
    return (np.sin(2 * np.pi * hz * t) * amplitude).astype("<i2").tobytes()


def _samples(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype="<i2").astype(float)


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x**2)))


def _chunked(pcm: bytes, src: int, dst: int, chunk_ms: int = 20) -> bytes:
    resampler = Resampler(src, dst)
    step = src * chunk_ms // 1000 * 2
    return (
        b"".join(resampler.process(pcm[i : i + step]) for i in range(0, len(pcm), step)) + resampler.flush()
    )


@pytest.mark.parametrize(("src", "dst"), RATES)
def test_output_length_is_the_input_duration_at_the_new_rate(src: int, dst: int) -> None:
    pcm = _tone(440, src, seconds=1.37)
    expected = -(-(len(pcm) // 2) * dst // src)
    assert len(resample(pcm, src, dst)) // 2 == expected
    assert len(_chunked(pcm, src, dst)) // 2 == expected


@pytest.mark.parametrize(("src", "dst"), RATES)
def test_chunked_equals_one_shot(src: int, dst: int) -> None:
    pcm = _tone(1_000, src) + _tone(3_000, src, 0.5)
    one_shot = _samples(resample(pcm, src, dst))
    for chunk_ms in (7, 20, 100):
        assert np.max(np.abs(_samples(_chunked(pcm, src, dst, chunk_ms)) - one_shot)) <= 1  # rounding only


@pytest.mark.parametrize(("src", "dst"), RATES)
def test_a_passband_tone_survives_aligned(src: int, dst: int) -> None:
    out = _samples(_chunked(_tone(1_000, src), src, dst))
    expected = np.sin(2 * np.pi * 1_000 * np.arange(len(out)) / dst) * 10_000
    edge = dst // 100  # the first and last 10 ms see the stream's zero padding
    assert np.max(np.abs(out[edge:-edge] - expected[edge:-edge])) < 50  # gain ~1, no delay, no clicks


@pytest.mark.parametrize(("src", "dst"), [(24_000, 16_000), (48_000, 24_000)])
def test_a_tone_above_the_new_nyquist_is_removed_not_folded(src: int, dst: int) -> None:
    hz = 0.5 * dst + 0.25 * (0.5 * src - 0.5 * dst)  # e.g. 10 kHz into 16 kHz, which would alias to 6 kHz
    out = _samples(_chunked(_tone(hz, src), src, dst))
    attenuation_db = 20 * np.log10(max(_rms(out[200:-200]), 1e-3) / (10_000 / np.sqrt(2)))
    assert attenuation_db < -60


def test_a_short_chunk_returns_nothing_until_the_filter_has_enough() -> None:
    resampler = Resampler(24_000, 16_000)
    assert resampler.process(b"\x01\x00") == b""
    assert len(resampler.process(b"\x00" * 960)) > 0


def test_same_rate_is_a_pass_through() -> None:
    assert Resampler(24_000, 24_000).process(b"\x01\x02") == b"\x01\x02"
    assert resample(b"\x01\x02", 24_000, 24_000) == b"\x01\x02"


def test_pcm_ms() -> None:
    assert pcm_ms(48_000) == 1000
