"""The bench's ``voice_ws`` socket for Pipecat, in the candidate's ``Framing`` (``vamos_voice.framing``).

Pipecat's FastAPI transport owns the socket, so this serializer is where the
framing applies: caller audio becomes input audio frames, agent audio goes out
encoded, and the hang-up matches ``CallerLine.close``. When the pipeline ends
(``EndFrame`` after the agent's hang-up tool, or ``CancelFrame``) the transport
writes that frame through here, then closes the socket with 1000: in ``json``
framing the frame becomes ``{"type": "end"}``, unless the caller hung up first.
A caller's ``end`` cancels the pipeline, as a dropped socket does, and is
reported at once (``on_caller_end``: the line's ``caller_gone``) so the call
finalizes without waiting for the pipeline to wind down.
"""

from __future__ import annotations

from collections.abc import Callable

from pipecat.frames.frames import (
    CancelFrame,
    CancelWorkerFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer

from vamos_voice.audio import CHANNEL_RATE_HZ
from vamos_voice.framing import Framing


class LineSerializer(FrameSerializer):
    """Audio in and out in the line's framing; the hang-up as the framing's end message; nothing else crosses."""

    def __init__(self, framing: Framing, on_caller_end: Callable[[], None] | None = None) -> None:
        super().__init__()
        self.framing = framing
        self.caller_ended = False
        self._on_caller_end = on_caller_end

    async def serialize(self, frame: Frame) -> bytes | str | None:
        if isinstance(frame, OutputAudioRawFrame):
            return self.framing.encode_audio(frame.audio)
        if isinstance(frame, EndFrame | CancelFrame) and not self.caller_ended:
            return self.framing.end_message()
        return None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        message = self.framing.decode(data)
        if message is None:
            return None
        if message.end:
            self.caller_ended = True
            if self._on_caller_end is not None:
                self._on_caller_end()
            return CancelWorkerFrame(reason="caller hung up")
        return InputAudioRawFrame(audio=message.audio, sample_rate=CHANNEL_RATE_HZ, num_channels=1)
