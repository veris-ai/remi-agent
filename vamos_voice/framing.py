"""How audio and hang-ups travel on the bench's ``voice_ws`` socket: the two framings.

The bench actor (veris-sandbox ``actor-service/app/actor/channels/voice_ws.py``)
speaks one of two framings, set per candidate by the channel's ``protocol``:

- ``binary``: each WebSocket message is raw PCM16 24 kHz mono. There is no
  in-band control, so the close handshake is the only hang-up signal, and the
  actor classifies *any* close by the callee as ``voice_ws_disconnect``, a
  failed attempt, even when the agent ended the call on purpose.
- ``json``: text messages ``{"type": "audio", "audio": "<base64 PCM16>"}`` both
  ways, and ``{"type": "end"}`` from either side is a clean hang-up. When the
  agent sends ``end`` before closing, the actor records ``client_end``, a
  normal end reason. The actor also sends ``end`` (then closes) when it hangs
  up, and ignores message types it does not know.

The candidate's channel ``protocol`` and this server's ``VAMOS_VOICE_FRAMING``
must match. ``binary`` stays the default (today's images); bench candidates
use ``json`` so an agent that ends the call is graded, not failed.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from .agent import ConfigError

logger = logging.getLogger(__name__)

FRAMING_ENV = "VAMOS_VOICE_FRAMING"
#: The WebSocket close code every deliberate hang-up uses (RFC 6455 normal closure).
NORMAL_CLOSURE = 1000
END_MESSAGE = json.dumps({"type": "end"})


@dataclass(frozen=True)
class CallerMessage:
    """One decoded caller message: audio, or the caller hanging up."""

    audio: bytes = b""
    end: bool = False


CALLER_END = CallerMessage(end=True)


class Framing(StrEnum):
    """The ``voice_ws`` framing: encodes agent audio and the hang-up, decodes caller messages."""

    BINARY = "binary"
    JSON = "json"

    def encode_audio(self, pcm: bytes) -> bytes | str:
        """Agent audio as one WebSocket message (bytes: a binary frame; str: a text frame)."""
        if self is Framing.BINARY:
            return pcm
        return json.dumps({"type": "audio", "audio": base64.b64encode(pcm).decode()})

    def end_message(self) -> str | None:
        """What to send before closing when the agent hangs up; None when the close says it all."""
        return END_MESSAGE if self is Framing.JSON else None

    def decode(self, data: bytes | str) -> CallerMessage | None:
        """One caller message; None for anything that is neither audio nor a hang-up (ignored)."""
        if self is Framing.BINARY:
            if isinstance(data, str):
                logger.warning("caller sent a text frame on a binary audio channel; ignored")
                return None
            return CallerMessage(audio=data) if data else None
        return _decode_json(data)


def _decode_json(data: bytes | str) -> CallerMessage | None:
    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
    try:
        event = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("caller sent a non-JSON frame on a json audio channel; ignored")
        return None
    if not isinstance(event, dict):
        logger.warning("caller sent a JSON frame that is not an object; ignored")
        return None
    kind = event.get("type")
    if kind == "end":
        return CALLER_END
    if kind != "audio":
        logger.debug("caller sent a %r frame; ignored", kind)
        return None
    try:
        pcm = base64.b64decode(event.get("audio") or "", validate=True)
    except (binascii.Error, ValueError):
        logger.warning("caller sent an audio frame with bad base64; ignored")
        return None
    return CallerMessage(audio=pcm) if pcm else None


def framing_from_env(env: Mapping[str, str]) -> Framing:
    """``VAMOS_VOICE_FRAMING`` (``binary`` or ``json``), or ``binary`` when unset."""
    raw = env.get(FRAMING_ENV) or Framing.BINARY.value
    try:
        return Framing(raw)
    except ValueError:
        choices = ", ".join(f.value for f in Framing)
        raise ConfigError(f"{FRAMING_ENV} must be one of {choices}, got {raw!r}") from None
