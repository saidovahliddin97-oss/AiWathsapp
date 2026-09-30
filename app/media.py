"""Media pipeline hooks: speech-to-text and text-to-speech.

The MVP ships without an STT/TTS vendor. Plug one in by implementing the
protocols below and passing it to :class:`app.webhook.Assistant`.
"""
from __future__ import annotations

from typing import Protocol


class Transcriber(Protocol):
    async def transcribe(self, audio: bytes, mime_type: str) -> str | None: ...


class Synthesizer(Protocol):
    """Future: Claude text -> TTS -> WhatsApp voice message."""

    async def synthesize(self, text: str) -> bytes: ...


class NullTranscriber:
    """Default: voice messages are acknowledged but not transcribed."""

    async def transcribe(self, audio: bytes, mime_type: str) -> str | None:
        return None
