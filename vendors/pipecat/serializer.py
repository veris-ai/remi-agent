"""The bench's ``voice_ws`` framing for Pipecat: raw PCM16 24 kHz mono binary frames."""

from __future__ import annotations

from pipecat.frames.frames import Frame, InputAudioRawFrame, OutputAudioRawFrame
from pipecat.serializers.base_serializer import FrameSerializer

from remi_core.audio import CHANNEL_RATE_HZ


class RawPCM16Serializer(FrameSerializer):
    """Bytes in become input audio frames; output audio frames go out as bytes; nothing else crosses."""

    async def serialize(self, frame: Frame) -> bytes | None:
        return frame.audio if isinstance(frame, OutputAudioRawFrame) else None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        if isinstance(data, bytes):
            return InputAudioRawFrame(audio=data, sample_rate=CHANNEL_RATE_HZ, num_channels=1)
        return None
