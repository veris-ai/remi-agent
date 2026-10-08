"""Hugging Face: utterance buffering, the Whisper request, the fal-ai Kokoro route and synthesis, offline.

The parity matrix (``test_parity.py``) covers the call through the shared
cascade; these pin the HTTP legs against an ``httpx.MockTransport``.
"""

from __future__ import annotations

import base64
import io
import json
import wave

import httpx
import numpy as np
import pytest

pytest.importorskip("openai")

import vamos_voice.providers.huggingface as hf
from vamos_voice.providers.huggingface import (
    HuggingFaceConfig,
    HuggingFaceProvider,
    KokoroTTS,
    WhisperSTT,
    pcm_to_wav,
    post_with_retry,
    wav_to_pcm,
)

FRAME = 960  # 20 ms at 24 kHz
VOICED = b"\x10\x00" * 480
QUIET = b"\x00\x00" * 480


def wav_info(data: bytes) -> tuple[int, int, int, bytes]:
    with wave.open(io.BytesIO(data), "rb") as w:
        return w.getframerate(), w.getnchannels(), w.getsampwidth(), w.readframes(w.getnframes())


def test_config_defaults_are_the_energy_candidates() -> None:
    assert HuggingFaceProvider.required_env == ("HF_TOKEN",)
    config = HuggingFaceConfig.from_env({"HF_TOKEN": "hf_x"})
    assert (config.llm_model, config.stt_model, config.tts_model, config.tts_voice) == (
        "openai/gpt-oss-120b:groq",
        "openai/whisper-large-v3",
        "hexgrad/Kokoro-82M",
        "af_heart",
    )
    assert "hf_x" not in repr(config)
    provider = HuggingFaceProvider(config)
    assert str(provider.llm.client.base_url) == "https://router.huggingface.co/v1/"
    assert provider.llm.model == "openai/gpt-oss-120b:groq"
    assert provider.stt_url == "https://router.huggingface.co/hf-inference/models/openai/whisper-large-v3"


async def test_an_utterance_keeps_its_onset_and_ends_after_the_silence() -> None:
    stt = WhisperSTT(httpx.AsyncClient(), "https://stt", silence_ms=800)
    for _ in range(30):  # 600 ms of quiet before: only the last 240 ms are kept as preroll
        await stt.feed(QUIET, speaking=False)
    for _ in range(10):
        await stt.feed(VOICED, speaking=True)
    for _ in range(40):
        await stt.feed(QUIET, speaking=False)
    utterance = stt.utterances.get_nowait()
    assert len(utterance) == (12 + 10 + 40) * FRAME
    assert utterance[12 * FRAME : 13 * FRAME] == VOICED
    assert stt.utterances.empty()


async def test_whisper_is_asked_for_english_and_a_timeout_drops_only_the_turn() -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 2:
            raise httpx.ReadTimeout("hung", request=request)
        return httpx.Response(200, json={"text": " Yes, this is Marcus. "})

    stt = WhisperSTT(httpx.AsyncClient(transport=httpx.MockTransport(handler)), "https://stt", 800)
    assert await stt.transcribe(VOICED * 50) == " Yes, this is Marcus. "
    assert await stt.transcribe(VOICED * 50) == ""
    body = requests[0]
    assert body["parameters"] == {"generate_kwargs": {"language": "en", "task": "transcribe"}}
    rate, channels, width, frames = wav_info(base64.b64decode(body["inputs"]))
    assert (rate, channels, width) == (16000, 1, 2)
    assert len(frames) == pytest.approx(len(VOICED * 50) * 2 / 3, abs=4)


async def test_429_and_503_are_waited_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hf, "RETRY_FIRST_S", 0.001)
    statuses = iter([503, 429, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(statuses), json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert (await post_with_retry(client, "https://x", "stt", json={})).status_code == 200


async def test_the_tts_route_is_resolved_from_the_hubs_provider_mapping() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/models/hexgrad/Kokoro-82M"
        assert request.url.params["expand"] == "inferenceProviderMapping"
        mapping = {
            "fal-ai": {
                "task": "text-to-speech",
                "status": "live",
                "providerId": "fal-ai/kokoro/american-english",
            },
            "replicate": {"task": "text-to-speech", "status": "staging", "providerId": "x"},
        }
        return httpx.Response(200, json={"inferenceProviderMapping": mapping})

    provider = HuggingFaceProvider(
        HuggingFaceConfig(token="hf_x"), http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    assert (
        await provider.resolve_tts_route()
        == "https://router.huggingface.co/fal-ai/fal-ai/kokoro/american-english"
    )


async def test_a_model_without_a_live_fal_route_fails_the_boot() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"inferenceProviderMapping": {}})

    provider = HuggingFaceProvider(
        HuggingFaceConfig(token="hf_x"), http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(RuntimeError, match="fal-ai"):
        await provider.resolve_tts_route()


async def test_kokoro_synthesizes_the_whole_round_and_the_audio_is_fetched_without_the_token() -> None:
    tone = (np.sin(np.arange(22050) / 10) * 8000).astype("<i2").tobytes()  # 1 s at 22.05 kHz
    posts: list[tuple[str, dict]] = []
    downloads: list[httpx.Request] = []

    def api(request: httpx.Request) -> httpx.Response:
        posts.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, json={"audio": {"url": "https://fal.media/files/a.wav"}})

    def files(request: httpx.Request) -> httpx.Response:
        downloads.append(request)
        return httpx.Response(200, content=pcm_to_wav(tone, 22050))

    tts = KokoroTTS(
        httpx.AsyncClient(transport=httpx.MockTransport(api), headers={"Authorization": "Bearer hf_x"}),
        httpx.AsyncClient(transport=httpx.MockTransport(files)),
        "https://router.huggingface.co/fal-ai/fal-ai/kokoro/american-english",
        "af_heart",
    )

    async def text():
        for delta in ("Hello ", "Marcus."):
            yield delta

    (audio,) = [pcm async for pcm in tts.speak(text())]
    assert len(audio) == pytest.approx(48_000, abs=8), "resampled to the caller's 24 kHz"
    assert posts == [
        (
            "https://router.huggingface.co/fal-ai/fal-ai/kokoro/american-english",
            {"text": "Hello Marcus.", "voice": "af_heart"},
        )
    ]
    assert "authorization" not in downloads[0].headers


def test_stereo_wav_is_mixed_down() -> None:
    stereo = np.array([[100, 300]] * 2400, dtype="<i2").tobytes()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(stereo)
    assert wav_to_pcm(buffer.getvalue()) == np.full(2400, 200, dtype="<i2").tobytes()
