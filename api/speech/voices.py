"""Voice discovery and allowlisting owned by the speech domain."""

from __future__ import annotations


EDGE_TTS_VOICES = (
    "zh-CN-XiaoxiaoNeural",
    "zh-CN-XiaoyiNeural",
    "zh-CN-YunxiNeural",
    "zh-CN-YunjianNeural",
    "zh-CN-YunyangNeural",
    "en-US-AriaNeural",
    "en-US-GuyNeural",
    "fr-CA-AntoineNeural",
    "fr-CA-JeanNeural",
    "fr-CA-SylvieNeural",
    "fr-CA-ThierryNeural",
    "fr-FR-DeniseNeural",
    "fr-FR-EloiseNeural",
    "fr-FR-HenriNeural",
    "id-ID-GadisNeural",
)

_EDGE_TTS_VOICE_SET = frozenset(EDGE_TTS_VOICES)


def discover_edge_voices() -> tuple[str, ...]:
    return EDGE_TTS_VOICES


def is_edge_voice_allowed(voice: str) -> bool:
    return voice in _EDGE_TTS_VOICE_SET
