"""TTS synthesis orchestration independent from HTTP request handling."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from urllib.request import ProxyHandler, Request, build_opener

from .errors import (
    InvalidSpeechRequest,
    SpeechSynthesisFailed,
    SpeechUnavailable,
    SpeechUpstreamRejected,
)
from .providers import (
    EdgeTtsProvider,
    ElevenLabsTtsProvider,
    OpenAITtsProvider,
    TtsProvider,
)
from .transport import (
    NoRedirectTtsHandler,
    PinnedHTTPSHandler,
    TTS_PROXY_MAX_BYTES,
    buffer_tts_audio_response,
    tts_open,
)
from .validation import TtsRequest
from .voices import is_edge_voice_allowed


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SpeechAudio:
    content: bytes
    content_type: str = "audio/mpeg"


def _synthesize_elevenlabs(
    request: TtsRequest,
    provider: ElevenLabsTtsProvider,
    *,
    open_fn,
    max_bytes: int,
    proxy_handler_factory,
    no_redirect_handler_class,
) -> SpeechAudio:
    url = (
        "https://api.elevenlabs.io/v1/text-to-speech/"
        f"{provider.voice_id}/stream?output_format=mp3_44100_128"
    )
    body = json.dumps(
        {
            "text": request.text,
            "model_id": provider.model_id,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        }
    ).encode("utf-8")
    upstream_request = Request(
        url,
        data=body,
        headers={
            "xi-api-key": provider.api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
    )
    try:
        opener_factory = lambda: build_opener(  # noqa: E731 - lazy security chain
            proxy_handler_factory({}),
            no_redirect_handler_class(),
        )
        with open_fn(upstream_request, timeout=30, opener_factory=opener_factory) as response:
            content = buffer_tts_audio_response(response, max_bytes=max_bytes)
    except ValueError as exc:
        logger.warning("ElevenLabs TTS rejected an invalid upstream response", exc_info=True)
        raise SpeechUpstreamRejected("ElevenLabs TTS generation failed") from exc
    except Exception as exc:
        logger.exception("ElevenLabs TTS generation failed")
        raise SpeechSynthesisFailed("ElevenLabs TTS generation failed") from exc
    return SpeechAudio(content)


def _synthesize_openai(
    request: TtsRequest,
    provider: OpenAITtsProvider,
    *,
    open_fn,
    max_bytes: int,
    proxy_handler_factory,
    no_redirect_handler_class,
    pinned_https_handler_class,
) -> SpeechAudio:
    body = json.dumps(
        {
            "model": provider.model,
            "input": request.text,
            "voice": provider.voice,
        }
    ).encode("utf-8")
    upstream_request = Request(
        f"{provider.base_url}/audio/speech",
        data=body,
        headers={
            "Authorization": f"Bearer {provider.api_key}",
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
    )
    try:
        opener_factory = lambda: build_opener(  # noqa: E731 - lazy security chain
            proxy_handler_factory({}),
            no_redirect_handler_class(),
            pinned_https_handler_class(),
        )
        with open_fn(upstream_request, timeout=30, opener_factory=opener_factory) as response:
            content = buffer_tts_audio_response(response, max_bytes=max_bytes)
    except ValueError as exc:
        logger.warning("OpenAI TTS rejected an invalid upstream response", exc_info=True)
        raise SpeechUpstreamRejected("OpenAI TTS generation failed") from exc
    except Exception as exc:
        logger.exception("OpenAI TTS generation failed")
        raise SpeechSynthesisFailed("OpenAI TTS generation failed") from exc
    return SpeechAudio(content)


def _synthesize_edge(request: TtsRequest) -> SpeechAudio:
    if not is_edge_voice_allowed(request.voice):
        raise InvalidSpeechRequest("invalid voice")
    try:
        import edge_tts
    except ImportError as exc:
        raise SpeechUnavailable(
            "Edge TTS engine not installed on the server. Install it with: pip install edge-tts"
        ) from exc

    kwargs = {}
    if request.rate:
        kwargs["rate"] = request.rate
    if request.pitch:
        kwargs["pitch"] = request.pitch
    try:
        communicator = edge_tts.Communicate(
            request.text,
            request.voice,
            **kwargs,
        )
        content = bytearray()
        for chunk in communicator.stream_sync():
            if chunk.get("type") == "audio" and chunk.get("data"):
                content.extend(chunk["data"])
    except BrokenPipeError:
        raise
    except Exception as exc:
        logger.exception("Edge TTS generation failed")
        raise SpeechSynthesisFailed("TTS generation failed") from exc
    if not content:
        raise SpeechSynthesisFailed("TTS produced no audio")
    return SpeechAudio(bytes(content))


def synthesize_tts(
    request: TtsRequest,
    provider: TtsProvider,
    *,
    open_fn=tts_open,
    max_bytes: int = TTS_PROXY_MAX_BYTES,
    proxy_handler_factory=ProxyHandler,
    no_redirect_handler_class=NoRedirectTtsHandler,
    pinned_https_handler_class=PinnedHTTPSHandler,
) -> SpeechAudio:
    if isinstance(provider, ElevenLabsTtsProvider):
        return _synthesize_elevenlabs(
            request,
            provider,
            open_fn=open_fn,
            max_bytes=max_bytes,
            proxy_handler_factory=proxy_handler_factory,
            no_redirect_handler_class=no_redirect_handler_class,
        )
    if isinstance(provider, OpenAITtsProvider):
        return _synthesize_openai(
            request,
            provider,
            open_fn=open_fn,
            max_bytes=max_bytes,
            proxy_handler_factory=proxy_handler_factory,
            no_redirect_handler_class=no_redirect_handler_class,
            pinned_https_handler_class=pinned_https_handler_class,
        )
    if not isinstance(provider, EdgeTtsProvider):
        raise SpeechSynthesisFailed("TTS generation failed")
    return _synthesize_edge(request)
