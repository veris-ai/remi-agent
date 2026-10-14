"""PCM16 mono audio helpers shared by every provider.

The bench's ``voice_ws`` channel is raw PCM16, 24 kHz, mono, both ways. A
vendor at another rate (Gemini Live takes 16 kHz in, Gradium speaks 48 kHz)
gets a streaming ``Resampler``; one at 24 kHz gets none.
"""

from __future__ import annotations

from math import gcd

import numpy as np

CHANNEL_RATE_HZ = 24_000
BYTES_PER_SAMPLE = 2

# The anti-alias / anti-image low-pass, a Kaiser-windowed sinc. Its stop band starts at the lower
# rate's Nyquist (so nothing folds back), its pass band is flat to ~0.8 of it (6.3 kHz at 16 kHz,
# 9.5 kHz at 24 kHz: all of speech), and it delays the audio by ~1.5 ms at most.
ZERO_CROSSINGS = 24  # filter half-length, in periods of the lower rate's Nyquist
STOPBAND_DB = 80.0
_KAISER_BETA = 0.1102 * (STOPBAND_DB - 8.7)


def pcm_ms(n_bytes: int, rate_hz: int = CHANNEL_RATE_HZ) -> float:
    """Milliseconds of audio in ``n_bytes`` of PCM16 mono."""
    return n_bytes / BYTES_PER_SAMPLE / rate_hz * 1000


def _polyphase_bank(up: int, down: int) -> np.ndarray:
    """The low-pass at ``up`` x the input rate, as rows of taps: row ``r`` serves outputs at phase ``r``."""
    widest = max(up, down)
    half = ZERO_CROSSINGS * widest
    n = np.arange(-half, half + 1)
    # Kaiser transition width (normalised to the upsampled rate); centre it so the stop band begins
    # exactly at the lower Nyquist, 1 / (2 * widest).
    transition = (STOPBAND_DB - 7.95) / (14.36 * (2 * half))
    cutoff = 1 / (2 * widest) - transition / 2
    taps = 2 * cutoff * np.sinc(2 * cutoff * n) * np.kaiser(len(n), _KAISER_BETA) * up
    per_phase = 2 * half // up + 1
    padded = np.zeros(up + per_phase * up)
    padded[: len(taps)] = taps
    rows = np.arange(up)[:, None] + up * np.arange(per_phase)[None, :]
    return padded[rows]


class Resampler:
    """Streaming band-limited (polyphase FIR) resampler for PCM16 mono.

    Carries the filter's input history and the output phase between chunks, so
    chunked output equals one-shot output: no clicks at chunk boundaries and no
    drift. Output is aligned with the input (the filter delay is compensated);
    each chunk returns every sample the input so far fully determines, which
    holds back ~1.5 ms. ``flush`` releases that tail at the end of a stream.
    """

    def __init__(self, src_hz: int, dst_hz: int) -> None:
        self.src_hz, self.dst_hz = src_hz, dst_hz
        g = gcd(src_hz, dst_hz)
        self._up, self._down = dst_hz // g, src_hz // g
        self._half = ZERO_CROSSINGS * max(self._up, self._down)
        self._bank = _polyphase_bank(self._up, self._down)
        width = self._bank.shape[1]
        self._buf = np.zeros(width, dtype=np.float64)  # input history; zeros before the stream
        self._buf_start = -width  # absolute index of _buf[0]
        self._n_in = 0  # input samples consumed
        self._n_out = 0  # output samples emitted

    def process(self, chunk: bytes) -> bytes:
        """Resample one chunk; returns the output samples it completes (possibly none)."""
        if self.src_hz == self.dst_hz:
            return chunk
        samples = np.frombuffer(chunk, dtype="<i2")
        self._n_in += len(samples)
        self._buf = np.concatenate([self._buf, samples.astype(np.float64)])
        last_in = self._buf_start + len(self._buf) - 1
        return self._emit(((last_in + 1) * self._up - self._half - 1) // self._down + 1)

    def flush(self) -> bytes:
        """End of stream: the held-back tail, so the total is ``ceil(n_in * dst / src)`` samples."""
        if self.src_hz == self.dst_hz:
            return b""
        total = -(-self._n_in * self._up // self._down)
        self._buf = np.concatenate([self._buf, np.zeros(self._bank.shape[1], dtype=np.float64)])
        return self._emit(total)

    def _emit(self, end: int) -> bytes:
        if end <= self._n_out:
            return b""
        k = np.arange(self._n_out, end)
        pos = k * self._down + self._half
        newest = pos // self._up  # the newest input each output reads
        phase = pos - newest * self._up
        width = self._bank.shape[1]
        idx = (newest - self._buf_start)[:, None] - np.arange(width)[None, :]
        out = np.einsum("ij,ij->i", self._bank[phase], self._buf[idx])
        self._n_out = end
        keep_from = (end * self._down + self._half) // self._up - width + 1
        drop = max(0, keep_from - self._buf_start)
        self._buf, self._buf_start = self._buf[drop:], self._buf_start + drop
        return np.clip(np.round(out), -32768, 32767).astype("<i2").tobytes()


def resample(pcm: bytes, src_hz: int, dst_hz: int) -> bytes:
    """One-shot: a whole PCM16 mono clip at ``dst_hz`` (``ceil(n * dst / src)`` samples)."""
    resampler = Resampler(src_hz, dst_hz)
    return resampler.process(pcm) + resampler.flush()
