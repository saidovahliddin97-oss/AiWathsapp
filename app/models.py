"""Domain models shared across modules."""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Mode(str, Enum):
    GREETING_ONLY = "GREETING_ONLY"
    FULL_CHAT = "FULL_CHAT"


class AgeGroup(str, Enum):
    ELDER = "elder"
    PEER = "peer"
    YOUNGER = "younger"


class Relative(BaseModel):
    id: str
    phone: str
    name: str
    relation: str = "relative"
    age_group: AgeGroup = AgeGroup.PEER
    # How the user normally addresses this person, e.g. "Модарҷон", "Карим тағо"
    address: str | None = None
    mode: Mode = Mode.FULL_CHAT
    notes: list[str] = Field(default_factory=list)


class Fact(BaseModel):
    fact: str
    source: Literal["user", "config", "conversation"] = "user"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    relative_id: str | None = None  # None = global fact about the user


class StoredMessage(BaseModel):
    sender: Literal["relative", "assistant"]
    text: str
    kind: str = "text"


class IncomingMessage(BaseModel):
    """A normalised inbound WhatsApp message."""

    message_id: str
    phone: str
    kind: Literal["text", "audio", "image", "unsupported"] = "text"
    text: str = ""
    media_id: str | None = None
    caption: str | None = None
    timestamp: int | None = None
    profile_name: str | None = None


class ProcessResult(BaseModel):
    status: Literal[
        "sent",
        "dry_run",
        "duplicate",
        "ignored_unknown_sender",
        "ignored_empty",
        "skipped_greeting_only",
        "generation_failed",
        "validation_failed",
        "send_failed_queued",
    ]
    relative_id: str | None = None
    mode: Mode | None = None
    reply: str | None = None
    detail: str | None = None
