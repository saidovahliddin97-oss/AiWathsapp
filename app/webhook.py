"""Core message pipeline: webhook event -> relative -> context -> Claude ->
validation -> WhatsApp -> memory.

Kept independent from FastAPI so it can be unit-tested directly.
"""
from __future__ import annotations

import logging
import time
from typing import Protocol

from app.claude import GenerationError, ReplyGenerator
from app.config import Settings
from app.media import NullTranscriber, Transcriber
from app.memory import Store, detect_fact_candidates
from app.models import IncomingMessage, Mode, ProcessResult, Relative
from app.prompts import build_context
from app.validation import validate_reply
from app.whatsapp import WhatsAppError

log = logging.getLogger("assistant")

GREETING_ONLY_COOLDOWN_SECONDS = 12 * 3600
MAX_GENERATION_ATTEMPTS = 2


class MessageSender(Protocol):
    async def send_text(self, to: str, text: str) -> str | None: ...


class MediaSource(Protocol):
    async def download_media(self, media_id: str) -> tuple[bytes, str]: ...


class DryRunSender:
    """Records messages instead of sending them (DRY_RUN / demo / tests)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, to: str, text: str) -> str | None:
        self.sent.append((to, text))
        return f"dryrun-{len(self.sent)}"


def _fmt(**fields) -> str:
    return " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)


class Assistant:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        generator: ReplyGenerator | None,
        sender: MessageSender,
        media_source: MediaSource | None = None,
        transcriber: Transcriber | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.generator = generator
        self.sender = sender
        self.media_source = media_source
        self.transcriber = transcriber or NullTranscriber()

    # ----------------------------------------------------------------- helpers
    @property
    def _status_ok(self) -> str:
        return "dry_run" if self.settings.dry_run or isinstance(self.sender, DryRunSender) else "sent"

    def _log(self, level: int, msg: str, **fields) -> None:
        log.log(level, "%s %s", msg, _fmt(**fields))

    def _resolve_relative(self, msg: IncomingMessage) -> Relative | None:
        rel = self.store.find_relative_by_phone(msg.phone)
        if rel or self.settings.ignore_unknown_senders:
            return rel
        rel = Relative(id=f"wa_{msg.phone}", phone=msg.phone, name=msg.profile_name or "Дӯст", relation="friend")
        self.store.upsert_relative(rel)
        return rel

    async def _media_context(self, msg: IncomingMessage) -> tuple[str, str | None]:
        """Return (text for the model, media note) for a message."""
        if msg.kind == "text":
            return msg.text.strip(), None
        if msg.kind == "audio":
            transcript = None
            if self.media_source and msg.media_id:
                try:
                    audio, mime = await self.media_source.download_media(msg.media_id)
                    transcript = await self.transcriber.transcribe(audio, mime)
                except WhatsAppError as e:
                    self._log(logging.WARNING, "audio download failed", event_id=msg.message_id, error=e)
            if transcript:
                return transcript.strip(), "The relative sent a voice message; incoming_message is its transcript."
            return "", (
                "The relative sent a voice message that could not be transcribed. Its content is unknown: "
                "do not pretend to know what was said; reply briefly and warmly."
            )
        if msg.kind == "image":
            description = None
            if self.media_source and msg.media_id and self.generator:
                try:
                    image, mime = await self.media_source.download_media(msg.media_id)
                    if mime in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                        description = await self.generator.describe_image(image, mime)
                except WhatsAppError as e:
                    self._log(logging.WARNING, "image download failed", event_id=msg.message_id, error=e)
            note = (
                f"The relative sent an image. Description: {description}"
                if description
                else "The relative sent an image whose content is unknown: react warmly but do not describe it."
            )
            return (msg.caption or "").strip(), note
        return "", "The relative sent a sticker or other media that cannot be read. Content unknown."

    async def _generate_valid(self, context: dict, mode: Mode, facts) -> tuple[str | None, str | None]:
        """Generate + validate, retrying once with validator feedback."""
        feedback = None
        for _ in range(MAX_GENERATION_ATTEMPTS):
            draft = await self.generator.generate(context, feedback)  # may raise GenerationError
            result = validate_reply(draft, mode, facts)
            if result.ok:
                return result.text, None
            feedback = result.feedback
        return None, feedback

    async def _deliver(self, rel: Relative, text: str, kind: str, event_id: str | None) -> ProcessResult | None:
        """Send and store. Returns a failure ProcessResult or None on success."""
        try:
            wa_id = await self.sender.send_text(rel.phone, text)
        except WhatsAppError as e:
            outbox_id = self.store.enqueue_outbox(rel.id, rel.phone, text, kind, str(e))
            self._log(logging.ERROR, "send failed, queued", event_id=event_id, relative=rel.id, outbox=outbox_id, error=e)
            return ProcessResult(status="send_failed_queued", relative_id=rel.id, reply=text, detail=str(e))
        self.store.add_message(rel.id, "assistant", text, kind=kind, wa_message_id=wa_id)
        return None

    # --------------------------------------------------------------- pipeline
    async def process(self, msg: IncomingMessage) -> ProcessResult:
        started = time.monotonic()
        event_id = msg.message_id
        if not self.store.claim_event(event_id):
            self._log(logging.INFO, "duplicate event skipped", event_id=event_id)
            return ProcessResult(status="duplicate")

        try:
            result = await self._process_claimed(msg)
        except Exception:
            self.store.finish_event(event_id, "retryable")
            self._log(logging.ERROR, "unexpected error", event_id=event_id)
            log.exception("pipeline crashed")
            raise
        self._log(
            logging.INFO,
            "processed",
            event_id=event_id,
            relative=result.relative_id,
            mode=result.mode.value if result.mode else None,
            status=result.status,
            ms=int((time.monotonic() - started) * 1000),
        )
        return result

    async def _process_claimed(self, msg: IncomingMessage) -> ProcessResult:
        event_id = msg.message_id
        rel = self._resolve_relative(msg)
        if rel is None:
            self.store.finish_event(event_id, "ignored")
            return ProcessResult(status="ignored_unknown_sender")

        text, media_note = await self._media_context(msg)
        if not text and not media_note:
            self.store.finish_event(event_id, "ignored")
            return ProcessResult(status="ignored_empty", relative_id=rel.id)

        if self.settings.log_message_text:
            self._log(logging.INFO, "incoming", event_id=event_id, relative=rel.id, text=repr(text[:200]))

        history = self.store.recent_messages(rel.id, limit=self.settings.history_limit)
        stored_text = text or f"[{msg.kind}]"
        self.store.add_message(rel.id, "relative", stored_text, kind=msg.kind, wa_message_id=event_id)
        for category in detect_fact_candidates(text):
            self.store.add_fact_candidate(rel.id, text, category)

        mode = rel.mode
        if mode is Mode.GREETING_ONLY:
            last = self.store.last_assistant_message_at(rel.id)
            if last and time.time() - last < GREETING_ONLY_COOLDOWN_SECONDS:
                self.store.finish_event(event_id, "done")
                return ProcessResult(status="skipped_greeting_only", relative_id=rel.id, mode=mode)

        if self.generator is None:
            self.store.finish_event(event_id, "failed")
            return ProcessResult(status="generation_failed", relative_id=rel.id, mode=mode, detail="no generator configured")

        facts = self.store.get_facts(rel.id)
        context = build_context(
            mode,
            rel,
            facts,
            history,
            incoming_text=text,
            recent_greetings=self.store.recent_greetings(rel.id) if mode is Mode.GREETING_ONLY else None,
            media_note=media_note,
        )
        try:
            reply, problems = await self._generate_valid(context, mode, facts)
        except GenerationError as e:
            self.store.finish_event(event_id, "retryable" if e.retryable else "failed")
            self._log(logging.ERROR, "generation failed", event_id=event_id, relative=rel.id, error=e)
            return ProcessResult(status="generation_failed", relative_id=rel.id, mode=mode, detail=str(e))
        if reply is None:
            self.store.finish_event(event_id, "failed")
            self._log(logging.WARNING, "validation failed", event_id=event_id, relative=rel.id, problems=problems)
            return ProcessResult(status="validation_failed", relative_id=rel.id, mode=mode, detail=problems)

        failure = await self._deliver(rel, reply, "greeting" if mode is Mode.GREETING_ONLY else "text", event_id)
        self.store.finish_event(event_id, "done")
        if failure:
            failure.mode = mode
            return failure
        return ProcessResult(status=self._status_ok, relative_id=rel.id, mode=mode, reply=reply)

    async def greet(self, relative_id: str) -> ProcessResult:
        """Send one proactive GREETING_ONLY message (e.g. from a scheduler)."""
        rel = self.store.get_relative(relative_id)
        if rel is None:
            return ProcessResult(status="ignored_unknown_sender", detail="unknown relative")
        if self.generator is None:
            return ProcessResult(status="generation_failed", relative_id=rel.id, detail="no generator configured")
        mode = Mode.GREETING_ONLY
        facts = self.store.get_facts(rel.id)
        context = build_context(
            mode,
            rel,
            facts,
            self.store.recent_messages(rel.id, limit=6),
            recent_greetings=self.store.recent_greetings(rel.id),
        )
        try:
            reply, problems = await self._generate_valid(context, mode, facts)
        except GenerationError as e:
            return ProcessResult(status="generation_failed", relative_id=rel.id, mode=mode, detail=str(e))
        if reply is None:
            return ProcessResult(status="validation_failed", relative_id=rel.id, mode=mode, detail=problems)
        failure = await self._deliver(rel, reply, "greeting", None)
        if failure:
            failure.mode = mode
            return failure
        self._log(logging.INFO, "greeting sent", relative=rel.id, mode=mode.value)
        return ProcessResult(status=self._status_ok, relative_id=rel.id, mode=mode, reply=reply)

    async def retry_outbox(self) -> dict[str, int]:
        """Re-send replies that failed earlier. Bounded by OUTBOX_MAX_ATTEMPTS."""
        sent = failed = 0
        for item in self.store.pending_outbox(self.settings.outbox_max_attempts):
            try:
                wa_id = await self.sender.send_text(item["phone"], item["text"])
            except WhatsAppError as e:
                self.store.mark_outbox(item["id"], sent=False, error=str(e))
                failed += 1
                continue
            self.store.mark_outbox(item["id"], sent=True)
            self.store.add_message(item["relative_id"], "assistant", item["text"], kind=item["kind"], wa_message_id=wa_id)
            sent += 1
        if sent or failed:
            self._log(logging.INFO, "outbox retry", sent=sent, failed=failed)
        return {"sent": sent, "failed": failed}
