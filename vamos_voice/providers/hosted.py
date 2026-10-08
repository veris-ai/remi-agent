"""What the hosted orchestrators (Vapi, Cartesia) share: the vendor runs STT, LLM and TTS on its side.

- ``public_base_url``: the pod's public address, for a vendor that calls tools back over
  HTTPS. Veris Bench sets ``PUBLIC_BASE_URL`` on an image candidate whose ``image`` block has
  ``"public_endpoint": true`` (``https://<voice host>/hooks/<attempt id>``; the gateway strips
  that prefix and forwards every path to the pod's port). The platform adds no auth, so a
  provider's webhook route authenticates the vendor itself with a per-call secret.
- ``hang_up_after_last_words``: the vendor's model may still be speaking (or about to) when
  the agent's hang-up tool returns; wait for its audio to stop, let it play, then hang up.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Mapping

from vamos_voice.providers.base import CallRuntime

logger = logging.getLogger(__name__)

PUBLIC_BASE_URL_ENV = "PUBLIC_BASE_URL"
#: No agent audio for this long after the hang-up tool means the last words are out.
LAST_WORDS_QUIET_S = 1.0
#: Longest the hang-up waits for the vendor to stop sending audio.
LAST_WORDS_MAX_WAIT_S = 15.0


def public_base_url(env: Mapping[str, str]) -> str:
    """``PUBLIC_BASE_URL`` without a trailing slash; ValueError unless it is an https:// URL."""
    base = (env.get(PUBLIC_BASE_URL_ENV) or "").strip().rstrip("/")
    if not base.startswith("https://"):
        raise ValueError(
            f"{PUBLIC_BASE_URL_ENV} must be the pod's public https:// URL "
            '(set by Veris Bench when the candidate\'s image has "public_endpoint": true)'
        )
    return base


def secret_matches(presented: str | None, expected: str) -> bool:
    """Constant-time comparison of a presented per-call secret."""
    return presented is not None and hmac.compare_digest(presented.encode(), expected.encode())


async def hang_up_after_last_words(call: CallRuntime) -> None:
    """The agent ended the call: wait until the vendor stops sending audio, let it play, hang up."""
    await call.outbound.wait_until_quiet(LAST_WORDS_QUIET_S, LAST_WORDS_MAX_WAIT_S)
    await call.hang_up()
