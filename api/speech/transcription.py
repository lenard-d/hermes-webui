"""Speech-to-text execution and temporary audio-file lifecycle."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import logging
from pathlib import Path
import tempfile

from .errors import SpeechUnavailable, TranscriptionFailed


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Transcript:
    text: str


@contextmanager
def temporary_audio_file(filename: str, content: bytes):
    suffix = Path(filename).suffix or ".webm"
    path = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="webui-stt-",
            suffix=suffix,
            delete=False,
        ) as temporary:
            path = Path(temporary.name)
            temporary.write(content)
        yield path
    finally:
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass


def transcribe_audio_upload(filename: str, content: bytes) -> Transcript:
    try:
        from tools.transcription_tools import transcribe_audio
    except ImportError as exc:
        raise SpeechUnavailable(
            "Speech-to-text is unavailable on this server"
        ) from exc

    with temporary_audio_file(filename, content) as audio_path:
        try:
            result = transcribe_audio(str(audio_path))
        except Exception as exc:
            logger.exception("Speech transcription failed")
            raise TranscriptionFailed("Transcription failed", status=500) from exc
    if not result.get("success"):
        message = str(result.get("error") or "Transcription failed")
        status = 503 if "unavailable" in message.lower() or "not configured" in message.lower() else 400
        raise TranscriptionFailed(message, status=status)
    return Transcript(str(result.get("transcript") or "").strip())
