"""Validation and normalization for speech requests."""

from __future__ import annotations

from dataclasses import dataclass
import re

from .errors import InvalidSpeechRequest


MAX_TTS_TEXT_CHARS = 5000
DEFAULT_EDGE_VOICE = "zh-CN-XiaoxiaoNeural"


@dataclass(frozen=True)
class TtsRequest:
    text: str
    engine: str
    voice: str
    rate: str
    pitch: str


def normalize_tts_prosody(value, *, unit: str) -> str | None:
    if not value:
        return ""
    normalized = str(value).strip()
    if not re.fullmatch(r"[+-]?\d{1,3}" + re.escape(unit), normalized):
        return None
    amount = int(normalized[: -len(unit)])
    if -100 <= amount <= 100:
        return normalized
    return None


def parse_tts_request(data) -> TtsRequest:
    if not isinstance(data, dict):
        raise InvalidSpeechRequest("invalid request body")

    text = str(data.get("text") or "").strip()
    voice = str(data.get("voice") or DEFAULT_EDGE_VOICE)
    engine = str(data.get("engine") or "edge").strip().lower()
    rate = normalize_tts_prosody(data.get("rate"), unit="%")
    pitch = normalize_tts_prosody(data.get("pitch"), unit="Hz")

    if rate is None:
        raise InvalidSpeechRequest("invalid rate")
    if pitch is None:
        raise InvalidSpeechRequest("invalid pitch")
    if not text:
        raise InvalidSpeechRequest("text is required")
    if len(text) > MAX_TTS_TEXT_CHARS:
        raise InvalidSpeechRequest(
            f"text too long (max {MAX_TTS_TEXT_CHARS} characters)"
        )
    return TtsRequest(
        text=text,
        engine=engine,
        voice=voice,
        rate=rate,
        pitch=pitch,
    )


def validate_voice_id(voice_id) -> str:
    normalized = str(voice_id or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", normalized):
        raise InvalidSpeechRequest("invalid voice_id in config")
    return normalized
