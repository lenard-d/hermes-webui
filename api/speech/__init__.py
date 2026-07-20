"""Public interface for speech synthesis, transcription, and discovery."""

from .discovery import (
    discover_transcription_provider,
    transcription_provider_capability_from_module,
)
from .errors import (
    InvalidSpeechRequest,
    SpeechError,
    SpeechSynthesisFailed,
    SpeechUnavailable,
    SpeechUpstreamRejected,
    TranscriptionFailed,
)
from .providers import resolve_tts_provider
from .synthesis import SpeechAudio, synthesize_tts
from .transcription import Transcript, temporary_audio_file, transcribe_audio_upload
from .validation import TtsRequest, normalize_tts_prosody, parse_tts_request
from .voices import EDGE_TTS_VOICES, discover_edge_voices, is_edge_voice_allowed

__all__ = (
    "EDGE_TTS_VOICES",
    "InvalidSpeechRequest",
    "SpeechAudio",
    "SpeechError",
    "SpeechSynthesisFailed",
    "SpeechUnavailable",
    "SpeechUpstreamRejected",
    "Transcript",
    "TranscriptionFailed",
    "TtsRequest",
    "discover_edge_voices",
    "discover_transcription_provider",
    "is_edge_voice_allowed",
    "normalize_tts_prosody",
    "parse_tts_request",
    "resolve_tts_provider",
    "synthesize_tts",
    "temporary_audio_file",
    "transcribe_audio_upload",
    "transcription_provider_capability_from_module",
)
