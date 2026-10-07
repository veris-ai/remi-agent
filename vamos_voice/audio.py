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


#: Analysis frame of ``CallerSpeech``: 20 ms of 24 kHz PCM16.
FRAME_MS = 20
_FRAME_BYTES = CHANNEL_RATE_HZ * BYTES_PER_SAMPLE * FRAME_MS // 1000
#: How long ``CallerSpeech`` remembers foreground frames (far longer than any caller turn).
_HORIZON_MS = 120_000


class CallerSpeech:
    """Tells the caller's own voice from quieter background audio on their line (a TV, a radio).

    Server VAD answers "is someone talking?"; it cannot tell the person on the phone from
    a television behind them, so a broadcast at -40 dBFS becomes caller turns that
    interrupt every reply. On a phone line the caller's own voice is the loudest thing by
    a wide margin (bench recordings: caller peaks -7 to -20 dBFS, background speech -36 to
    -46), so a 20 ms frame is *foreground* when it is within ``margin_db`` of the loudest
    level heard so far on the call and above ``floor_dbfs``. Until the caller's voice has
    set that peak, everything above the floor counts (the vendor's VAD alone decides, as
    before), and a caller no louder than their background is treated as background.

    Frames are timed on the input timeline: milliseconds of audio fed so far, the clock
    the Realtime VAD events use (``audio_start_ms`` / ``audio_end_ms``).
    """

    def __init__(self, margin_db: float = 20.0, floor_dbfs: float = -55.0, min_frames: int = 5) -> None:
        self.margin_db, self.floor_dbfs, self.min_frames = margin_db, floor_dbfs, min_frames
        self.position_ms = 0
        self.peak_dbfs = -np.inf
        # The last 3 frame levels: the peak follows their median, so a lone click never sets it.
        self._recent: list[float] = [-np.inf, -np.inf]
        self._foreground: list[int] = []  # start ms of each foreground frame, ascending
        self._partial = b""

    def feed(self, pcm: bytes) -> None:
        """Analyse caller audio in the order it is sent to the vendor."""
        data = self._partial + pcm
        whole = len(data) - len(data) % _FRAME_BYTES
        self._partial = data[whole:]
        if not whole:
            return
        frames = np.frombuffer(data[:whole], dtype="<i2").astype(np.float64).reshape(-1, _FRAME_BYTES // 2)
        levels = 20 * np.log10(np.maximum(np.sqrt(np.mean(frames**2, axis=1)), 1e-9) / 32768)
        for level in levels.tolist():
            self._recent = [*self._recent[-2:], level]
            self.peak_dbfs = max(self.peak_dbfs, float(np.median(self._recent)))
            if level >= max(self.floor_dbfs, self.peak_dbfs - self.margin_db):
                self._foreground.append(self.position_ms)
            self.position_ms += FRAME_MS
        if self._foreground and self._foreground[0] < self.position_ms - _HORIZON_MS:
            self._foreground = [t for t in self._foreground if t >= self.position_ms - _HORIZON_MS]

    def foreground_ms(self, start_ms: int, end_ms: int | None = None) -> int:
        """Milliseconds of foreground (the caller's own voice) in ``[start_ms, end_ms)``."""
        end = self.position_ms if end_ms is None else end_ms
        return FRAME_MS * sum(1 for t in self._foreground if start_ms <= t < end)

    def caller_spoke(self, start_ms: int, end_ms: int | None = None) -> bool:
        """Did the caller's own voice (not only background) fill ``min_frames`` frames in the span?"""
        return self.foreground_ms(start_ms, end_ms) >= self.min_frames * FRAME_MS
