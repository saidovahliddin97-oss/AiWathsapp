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
    # Linked-device bridge extras
    chat_id: str | None = None  # where to reply (group jid or private chat)
    is_group: bool = False
    group_name: str | None = None
    addressed_to_bot: bool = False  # @mention or reply to one of our messages
    from_me: bool = False  # written from the owner's own account (personal-number mode)
    self_chat: bool = False  # the owner's "message yourself" chat
    is_business: bool = False  # sender is a WhatsApp Business account
    media_b64: str | None = None
    media_mime: str | None = None


class ProcessResult(BaseModel):
    status: Literal[
        "sent",
        "dry_run",
        "duplicate",
        "ignored_unknown_sender",
        "ignored_blocked",
        "ignored_business",
        "ignored_empty",
        "skipped_greeting_only",
        "skipped_manual_pause",
        "skipped_autopilot_off",
        "group_not_addressed",
        "owner_command",
        "owner_message_recorded",
        "generation_failed",
        "validation_failed",
        "send_failed_queued",
    ]
    relative_id: str | None = None
    mode: Mode | None = None
    reply: str | None = None
    detail: str | None = None
