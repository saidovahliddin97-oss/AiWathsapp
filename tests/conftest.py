from __future__ import annotations

import pytest

from app.claude import GenerationError
from app.config import Settings
from app.memory import Store
from app.models import AgeGroup, Fact, Mode, Relative
from app.webhook import Assistant, DryRunSender
from app.whatsapp import WhatsAppError


class FakeGenerator:
    """Scripted generator that records every context it receives."""

    def __init__(self, replies=None, error: Exception | None = None):
        self.replies = list(replies or ["Ваалейкум ассалом, Модарҷон! Ташаккур, хубам."])
        self.error = error
        self.calls: list[tuple[dict, str | None]] = []

    async def generate(self, context, feedback=None):
        self.calls.append((context, feedback))
        if self.error:
            raise self.error
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]

    async def describe_image(self, image, mime_type):
        return "На фото семья за праздничным столом."


class FailingSender:
    def __init__(self, fail_times=10):
        self.fail_times = fail_times
        self.sent = []

    async def send_text(self, to, text, quote_id=None):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise WhatsAppError("WhatsApp API error 503", 503)
        self.sent.append((to, text))
        return "wamid.ok"


class FakeMedia:
    def __init__(self, mime="image/jpeg"):
        self.mime = mime
        self.requested = []

    async def download_media(self, media_id):
        self.requested.append(media_id)
        return b"bytes", self.mime


class FakeTranscriber:
    def __init__(self, text):
        self.text = text

    async def transcribe(self, audio, mime_type):
        return self.text


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        anthropic_api_key="",
        dry_run=True,
        database_path=":memory:",
        allow_offline_generator=False,
        reply_to_everyone=False,  # most tests cover the "relatives list only" mode
    )


@pytest.fixture
def store():
    s = Store(":memory:")
    s.upsert_relative(Relative(id="mom", phone="992900000001", name="Модар", relation="mother",
                               age_group=AgeGroup.ELDER, address="Модарҷон"))
    s.upsert_relative(Relative(id="uncle", phone="992900000004", name="Карим", relation="uncle",
                               age_group=AgeGroup.ELDER, address="Карим тағо"))
    s.upsert_relative(Relative(id="sis", phone="992900000005", name="Нигина", relation="sister",
                               age_group=AgeGroup.YOUNGER))
    s.upsert_relative(Relative(id="aunt", phone="992900000003", name="Зарина", relation="aunt",
                               age_group=AgeGroup.ELDER, address="Зарина хола", mode=Mode.GREETING_ONLY))
    yield s
    s.close()


@pytest.fixture
def sender():
    return DryRunSender()


def make_assistant(settings, store, generator, sender=None, **kw):
    return Assistant(settings, store, generator, sender or DryRunSender(), **kw)


__all__ = ["FakeGenerator", "FailingSender", "FakeMedia", "FakeTranscriber", "make_assistant", "GenerationError", "Fact"]
