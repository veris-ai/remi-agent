"""Silero voice activity detection over the caller's PCM16 stream, for the in-pod cascades.

The cascades (``cascade.py``: Mistral, Hugging Face, Grok STT + TTS) buy STT
and TTS from the vendor, and nothing upstream tells them when the caller starts
talking over the agent; Mistral and Hugging Face do not tell them when the
caller's turn ends either. They use the same detector the VAmoS Energy
candidates of those vendors use (rory-agent ``rory_tools/vad.py``), so the
vendors are compared under the same local plumbing: Silero, a speech-vs-not
classifier (the model Pipecat and LiveKit run), not a frame-energy gate, which
hears the bench's cafe or television noise as a caller who never stops talking.

It scores 32 ms windows of 16 kHz audio; caller frames at 24 kHz are resampled
and accumulated into those windows, and the last window's verdict stands for
every frame until the next one completes. Speech starts at ``start_threshold``
and ends below ``STOP_THRESHOLD`` (the gap keeps a breath from flickering the
verdict mid-sentence).

The ONNX weights ship in ``data/silero_vad.onnx`` (MIT, ``data/SILERO_VAD_LICENSE``;
the same file Pipecat ships) and run on CPU through onnxruntime; one inference
session per process, the recurrent state per call.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import numpy as np
import onnxruntime

from ..audio import CHANNEL_RATE_HZ, Resampler

MODEL_PATH = Path(__file__).parent / "data" / "silero_vad.onnx"
MODEL_RATE_HZ = 16_000
WINDOW_SAMPLES = 512  # 32 ms at 16 kHz, the size the model is trained on
CONTEXT_SAMPLES = 64  # the model wants the previous window's tail in front
WINDOW_BYTES = WINDOW_SAMPLES * 2
#: Below this probability an utterance in progress ends (the start threshold is ``TurnTaking.vad_threshold``).
STOP_THRESHOLD = 0.35
#: The recurrent state is reset after this much continuous non-speech, so a long silence cannot drift it.
RESET_AFTER_S = 5.0


@cache
def inference_session() -> onnxruntime.InferenceSession:
    """The model, loaded once per process (``warm_up`` calls this before /health reports ready)."""
    options = onnxruntime.SessionOptions()
    options.inter_op_num_threads = 1
    options.intra_op_num_threads = 1
    return onnxruntime.InferenceSession(
        str(MODEL_PATH), providers=["CPUExecutionProvider"], sess_options=options
    )


class SileroVad:
    """One call's detector: push PCM16 mono frames, read back whether the caller is speaking."""

    def __init__(self, start_threshold: float = 0.5, rate_hz: int = CHANNEL_RATE_HZ) -> None:
        self.start_threshold = start_threshold
        self._resampler = Resampler(rate_hz, MODEL_RATE_HZ)
        self._pending = b""
        self._speaking = False
        self._quiet_s = 0.0
        self.probability = 0.0
        self._reset_state()

    def _reset_state(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, CONTEXT_SAMPLES), dtype=np.float32)

    def push(self, frame: bytes) -> bool:
        """Score ``frame``; return whether the caller is speaking now."""
        self._pending += self._resampler.process(frame)
        while len(self._pending) >= WINDOW_BYTES:
            window = np.frombuffer(self._pending[:WINDOW_BYTES], dtype="<i2")
            self._pending = self._pending[WINDOW_BYTES:]
            self._score(window.astype(np.float32) / 32768.0)
        return self._speaking

    def _score(self, window: np.ndarray) -> None:
        x = np.concatenate((self._context, window[np.newaxis, :]), axis=1)
        out, self._state = inference_session().run(
            None, {"input": x, "state": self._state, "sr": np.array(MODEL_RATE_HZ, dtype=np.int64)}
        )
        self._context = x[:, -CONTEXT_SAMPLES:]
        self.probability = float(out[0][0])
        threshold = STOP_THRESHOLD if self._speaking else self.start_threshold
        self._speaking = self.probability > threshold
        if self._speaking:
            self._quiet_s = 0.0
            return
        self._quiet_s += WINDOW_SAMPLES / MODEL_RATE_HZ
        if self._quiet_s >= RESET_AFTER_S:
            self._reset_state()
            self._quiet_s = 0.0
