"""Stable error vocabulary for speech domain operations."""

from __future__ import annotations


class SpeechError(Exception):
    """A speech failure with an HTTP-compatible public error contract."""

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.public_message = message
        self.status = status


class InvalidSpeechRequest(SpeechError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status=400)


class SpeechUnavailable(SpeechError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status=503)


class SpeechUpstreamRejected(SpeechError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status=502)


class SpeechSynthesisFailed(SpeechError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status=500)


class TranscriptionFailed(SpeechError):
    """A transcription backend failure preserving its established status."""

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message, status=status)
