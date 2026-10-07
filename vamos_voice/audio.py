"""PCM16 mono audio helpers shared by every provider.

The bench's ``voice_ws`` channel is raw PCM16, 24 kHz, mono, both ways. A
vendor at another rate (Gemini Live takes 16 kHz in) gets a streaming
``Resampler``; one at 24 kHz gets none.
"""

from __future__ import annotations

import numpy as np

CHANNEL_RATE_HZ = 24_000
BYTES_PER_SAMPLE = 2


def pcm_ms(n_bytes: int, rate_hz: int = CHANNEL_RATE_HZ) -> float:
    """Milliseconds of audio in ``n_bytes`` of PCM16 mono."""
    return n_bytes / BYTES_PER_SAMPLE / rate_hz * 1000


class Resampler:
    """Streaming linear-interpolation resampler for PCM16 mono.

    Keeps the last input sample and the fractional read position between
    chunks, so chunk boundaries add no clicks and no drift. Speech-band
    quality is all a voice model needs; no anti-alias filter is applied
    beyond what the 24 kHz → 16 kHz ratio tolerates for speech.
    """

    def __init__(self, src_hz: int, dst_hz: int) -> None:
        self.src_hz, self.dst_hz = src_hz, dst_hz
        self._step = src_hz / dst_hz
        self._pos = 0.0  # next output sample, in input-sample units relative to the carried sample
        self._carry: np.ndarray = np.zeros(0, dtype=np.float32)

    def process(self, chunk: bytes) -> bytes:
        if self.src_hz == self.dst_hz:
            return chunk
        samples = np.frombuffer(chunk, dtype="<i2").astype(np.float32)
        data = np.concatenate([self._carry, samples])
        if len(data) < 2:
            self._carry = data
            return b""
        positions = np.arange(self._pos, len(data) - 1, self._step)
        out = np.interp(positions, np.arange(len(data)), data)
        next_pos = positions[-1] + self._step if len(positions) else self._pos
        keep_from = len(data) - 1
        self._carry = data[keep_from:]
        self._pos = next_pos - keep_from
        return np.clip(np.round(out), -32768, 32767).astype("<i2").tobytes()
