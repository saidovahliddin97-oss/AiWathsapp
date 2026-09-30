"""Core message pipeline: webhook event -> relative -> context -> Claude ->
validation -> WhatsApp -> memory.

Kept independent from FastAPI so it can be unit-tested directly.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Protocol

from app.claude import GenerationError, ReplyGenerator
from app.config import Settings
from app.media import NullTranscriber, Transcriber
from app.memory import Store, detect_fact_candidates, normalize_phone
from app.models import IncomingMessage, Mode, ProcessResult, Relative
from app.prompts import build_context
from app.validation import explain_problems, validate_reply
from app.whatsapp import WhatsAppError

log = logging.getLogger("assistant")

IMPORTANT_CATEGORIES = {"health", "loss"}
MAX_GENERATION_ATTEMPTS = 3


class MessageSender(Protocol):
    async def send_text(self, to: str, text: str, quote_id: str | None = None) -> str | None: ...


class MediaSource(Protocol):
    async def download_media(self, media_id: str) -> tuple[bytes, str]: ...


class DryRunSender:
    """Records messages instead of sending them (DRY_RUN / demo / tests)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, to: str, text: str, quote_id: str | None = None) -> str | None:
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
        self._new_contact: Relative | None = None
        self._migrate_flags()

    # ----------------------------------------------------------------- helpers
    @property
    def generator_label(self) -> str:
        from app.claude import generator_name

        return generator_name(self.generator)

    @property
    def _status_ok(self) -> str:
        return "dry_run" if self.settings.dry_run or isinstance(self.sender, DryRunSender) else "sent"

    def _log(self, level: int, msg: str, **fields) -> None:
        log.log(level, "%s %s", msg, _fmt(**fields))

    def reply_all(self) -> bool:
        return self.store.get_flag("reply_all", self.settings.reply_to_everyone)

    def blocked(self, phone: str) -> bool:
        return self.store.get_flag(f"block:{normalize_phone(phone)}", False)

    def _resolve_relative(self, msg: IncomingMessage) -> Relative | None:
        rel = self.store.find_relative_by_phone(msg.phone)
        if rel or not (self.reply_all() or not self.settings.ignore_unknown_senders):
            return rel
        phone = normalize_phone(msg.phone)
        if len(phone) < 6:
            return None
        rel = Relative(
            id=f"c_{phone}",
            phone=phone,
            name=msg.profile_name or f"+{phone}",
            relation="contact",
            mode=Mode.FULL_CHAT,
            notes=["Not in the User's family list: relationship unknown."],
        )
        self.store.upsert_relative(rel)
        self._new_contact = rel
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
        """Generate + validate, retrying with validator feedback."""
        feedback = None
        self._last_draft = ""
        for _ in range(MAX_GENERATION_ATTEMPTS):
            draft = await self.generator.generate(context, feedback)  # may raise GenerationError
            result = validate_reply(draft, mode, facts, incoming=context.get("incoming_message"))
            if result.ok:
                return result.text, None
            self._last_draft = result.text
            feedback = result.feedback
        return None, feedback

    async def _deliver(
        self,
        rel: Relative,
        text: str,
        kind: str,
        event_id: str | None,
        target: str | None = None,
        quote_id: str | None = None,
    ) -> ProcessResult | None:
        """Send and store. Returns a failure ProcessResult or None on success."""
        target = target or rel.phone
        try:
            wa_id = await self.sender.send_text(target, text, quote_id=quote_id)
        except WhatsAppError as e:
            outbox_id = self.store.enqueue_outbox(rel.id, target, text, kind, str(e))
            self._log(logging.ERROR, "send failed, queued", event_id=event_id, relative=rel.id, outbox=outbox_id, error=e)
            return ProcessResult(status="send_failed_queued", relative_id=rel.id, reply=text, detail=str(e))
        self.store.add_message(rel.id, "assistant", text, kind=kind, wa_message_id=wa_id)
        return None

    # ------------------------------------------------------------ owner & flags
    def _owner_target(self) -> str | None:
        """Where notifications for the owner go ("me" = own self-chat in personal-number mode)."""
        if not self.settings.notify_owner:
            return None
        if self.settings.personal_number:
            return "me"
        return self.settings.owner_phone or None

    def _is_owner(self, msg: IncomingMessage) -> bool:
        if msg.from_me and msg.self_chat:
            return True
        owner = normalize_phone(self.settings.owner_phone)
        return bool(owner) and not msg.is_group and normalize_phone(msg.phone) == owner

    async def notify_owner(self, text: str) -> None:
        target = self._owner_target()
        if not target:
            return
        try:
            await self.sender.send_text(target, text)
        except WhatsAppError as e:
            self._log(logging.WARNING, "owner notification failed", error=e)

    def autopilot_on(self) -> bool:
        """True if at least one relative gets full replies."""
        return any(r.mode is Mode.FULL_CHAT for r in self.store.list_relatives() if r.relation != "family_group")

    def default_mode(self) -> Mode:
        return Mode(self.store.get_kv("default_mode", Mode.FULL_CHAT.value))

    def _migrate_flags(self) -> None:
        # Older versions had a global "autopilot" switch; now it is a bulk per-person mode.
        if self.store.get_kv("autopilot") == "0":
            self.store.set_all_modes(Mode.GREETING_ONLY)
            self.store.set_kv("default_mode", Mode.GREETING_ONLY.value)
        self.store.set_kv("autopilot", "1")
        # "Reply to everyone" (owner's request): switch everybody to full chat once.
        if self.reply_all() and self.store.get_kv("reply_all_migrated") is None:
            self.store.set_all_modes(Mode.FULL_CHAT)
            self.store.set_kv("default_mode", Mode.FULL_CHAT.value)
            self.store.set_kv("reply_all_migrated", "1")

    def group_enabled(self, chat_id: str | None) -> bool:
        return bool(chat_id) and self.store.get_flag(f"group_on:{chat_id}", False)

    def paused(self, relative_id: str) -> bool:
        now = time.time()
        if float(self.store.get_kv("pause_all_until", "0")) > now:
            return True
        last_manual = float(self.store.get_kv(f"manual:{relative_id}", "0"))
        return now - last_manual < self.settings.manual_pause_minutes * 60

    def _group_entity(self, msg: IncomingMessage) -> Relative:
        rel_id = "grp_" + normalize_phone(msg.chat_id or "")
        rel = self.store.get_relative(rel_id)
        if rel is None:
            rel = Relative(
                id=rel_id,
                phone=normalize_phone(msg.chat_id or ""),
                name=msg.group_name or "Оилавӣ гурӯҳ",
                relation="family_group",
                age_group="peer",
                mode=self.default_mode(),
                notes=["This is a family group chat with several relatives; the User is one of the members."],
            )
            self.store.upsert_relative(rel)
        return rel

    def _journal(self, msg: IncomingMessage, result: ProcessResult) -> None:
        """Keep the last few decisions so the owner can ask /почему."""
        if result.status in ("owner_command", "duplicate") or (msg.is_group and result.detail == "group disabled"):
            return
        rel = self.store.get_relative(result.relative_id) if result.relative_id else None
        who = (rel.address or rel.name) if rel else (msg.profile_name or f"+{normalize_phone(msg.phone)}")
        entry = {"t": time.time(), "who": who, "rel": result.relative_id, "status": result.status,
                 "detail": (result.detail or "")[:200], "reply": (result.reply or "")[:120], "text": msg.text[:60]}
        try:
            items = json.loads(self.store.get_kv("journal", "[]"))
        except ValueError:
            items = []
        self.store.set_kv("journal", json.dumps((items + [entry])[-10:], ensure_ascii=False))

    def _addresses_owner(self, text: str) -> bool:
        low = text.lower()
        return any(re.search(rf"(?<!\w){re.escape(n.lower())}", low) for n in self.settings.owner_name_list)

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
        self._journal(msg, result)
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

        # 1. Owner: commands and manual replies
        if self._is_owner(msg):
            self.store.finish_event(event_id, "done")
            from app.commands import handle_command

            answer = await handle_command(self, msg.text)
            if answer:
                await self.notify_owner(answer) if msg.self_chat else await self._send_raw(msg.phone, answer)
            return ProcessResult(status="owner_command", reply=answer)
        # Groups are ignored completely unless the owner enabled them (/группа вкл <название>)
        if msg.is_group and not self.group_enabled(msg.chat_id):
            if msg.chat_id and msg.group_name:
                self.store.set_kv(f"group_seen:{msg.chat_id}", msg.group_name)
            self.store.finish_event(event_id, "ignored")
            return ProcessResult(status="group_not_addressed", detail="group disabled")
        if msg.from_me:
            if msg.is_group:
                rel = self._group_entity(msg)
            elif self.blocked(msg.phone):
                rel = None
            else:  # you wrote to someone yourself: remember it and let the bot keep quiet there
                rel = self._resolve_relative(msg)
                self._new_contact = None
            self.store.finish_event(event_id, "done")
            if rel is None or not msg.text.strip():
                return ProcessResult(status="owner_message_recorded")
            self.store.add_message(rel.id, "assistant", msg.text.strip(), kind="manual", wa_message_id=event_id)
            self.store.set_kv(f"manual:{rel.id}", str(time.time()))
            return ProcessResult(status="owner_message_recorded", relative_id=rel.id)

        # 2. Who is talking
        if not msg.is_group and self.blocked(msg.phone):
            self.store.finish_event(event_id, "ignored")
            return ProcessResult(status="ignored_blocked", detail=f"+{normalize_phone(msg.phone)}")
        speaker: Relative | None = self.store.find_relative_by_phone(msg.phone)
        if not msg.is_group and speaker is None and msg.is_business and self.settings.skip_business:
            self.store.finish_event(event_id, "ignored")
            return ProcessResult(status="ignored_business", detail=f"+{normalize_phone(msg.phone)} {msg.profile_name or ''}".strip())
        self._new_contact = None
        if msg.is_group:
            rel = self._group_entity(msg)
        else:
            rel = self._resolve_relative(msg)
            if self._new_contact is not None:
                phone = normalize_phone(msg.phone)
                await self.notify_owner(
                    f"🆕 Бот начал отвечать новому собеседнику: {self._new_contact.name} (+{phone}).\n"
                    f"Если ему не нужно отвечать: /блок {phone}"
                )
            if rel is None:
                self.store.finish_event(event_id, "ignored")
                await self._notify_unknown(msg)
                return ProcessResult(
                    status="ignored_unknown_sender",
                    detail=f"+{normalize_phone(msg.phone)} {msg.profile_name or ''}".strip(),
                )

        text, media_note = await self._media_context(msg)
        if not text and not media_note:
            self.store.finish_event(event_id, "ignored")
            return ProcessResult(status="ignored_empty", relative_id=rel.id)

        if self.settings.log_message_text:
            self._log(logging.INFO, "incoming", event_id=event_id, relative=rel.id, text=repr(text[:200]))

        history = self.store.recent_messages(rel.id, limit=self.settings.history_limit)
        stored_text = text or f"[{msg.kind}]"
        if msg.is_group:
            who = (speaker.address or speaker.name) if speaker else (msg.profile_name or msg.phone)
            stored_text = f"{who}: {stored_text}"
        self.store.add_message(rel.id, "relative", stored_text, kind=msg.kind, wa_message_id=event_id)

        fact_owner = speaker.id if (msg.is_group and speaker) else rel.id
        categories = detect_fact_candidates(text)
        for category in categories:
            self.store.add_fact_candidate(fact_owner, text, category)
        who_name = (speaker.address or speaker.name) if speaker else (msg.profile_name or msg.phone)
        if IMPORTANT_CATEGORIES.intersection(categories):
            await self.notify_owner(f"⚠️ Муҳим / Важно — {who_name}: {text[:500]}")

        # 3. Should we answer at all?
        mode = rel.mode
        skip: str | None = None
        if msg.is_group and not (msg.addressed_to_bot or self._addresses_owner(text)):
            skip = "group_not_addressed"
        elif mode is Mode.GREETING_ONLY:
            skip = "skipped_greeting_only"
        elif self.paused(rel.id):
            skip = "skipped_manual_pause"
        if skip:
            self.store.finish_event(event_id, "done")
            if skip in ("skipped_autopilot_off", "skipped_greeting_only") and self.settings.forward_unanswered:
                await self.notify_owner(f"✉️ {who_name}: {text[:500] or '[' + msg.kind + ']'}")
            return ProcessResult(status=skip, relative_id=rel.id, mode=mode)

        if self.generator is None:
            self.store.finish_event(event_id, "failed")
            return ProcessResult(status="generation_failed", relative_id=rel.id, mode=mode, detail="no generator configured")

        # 4. Generate, validate, send
        facts = self.store.get_facts(rel.id)
        if msg.is_group and speaker:
            facts = facts + [f for f in self.store.get_facts(speaker.id) if f.relative_id]
        context = build_context(
            mode,
            rel,
            facts,
            history,
            incoming_text=text,
            media_note=media_note,
            speaker=speaker if msg.is_group else None,
            speaker_name=who_name if msg.is_group else None,
        )
        try:
            reply, problems = await self._generate_valid(context, mode, facts)
        except GenerationError as e:
            self.store.finish_event(event_id, "retryable" if e.retryable else "failed")
            self._log(logging.ERROR, "generation failed", event_id=event_id, relative=rel.id, error=e)
            await self.notify_owner(f"❗ Не смог ответить ({who_name}): {text[:300]}\nПричина: {e}\nОтветьте сами.")
            return ProcessResult(status="generation_failed", relative_id=rel.id, mode=mode, detail=str(e))
        if reply is None:
            self.store.finish_event(event_id, "failed")
            self._log(logging.WARNING, "validation failed", event_id=event_id, relative=rel.id, problems=problems)
            await self.notify_owner(
                f"❗ Не смог ответить ({who_name}): {text[:300]}\n"
                f"Черновик бота: «{self._last_draft[:300]}»\n"
                f"Не отправлен, потому что: {explain_problems(problems)}\nОтветьте сами."
            )
            return ProcessResult(status="validation_failed", relative_id=rel.id, mode=mode, detail=problems)

        target = msg.chat_id if msg.is_group and msg.chat_id else rel.phone
        quote = event_id if msg.is_group else None
        failure = await self._deliver(rel, reply, "text", event_id, target=target, quote_id=quote)
        self.store.finish_event(event_id, "done")
        if failure:
            failure.mode = mode
            return failure
        return ProcessResult(status=self._status_ok, relative_id=rel.id, mode=mode, reply=reply)

    async def _send_raw(self, to: str, text: str) -> None:
        try:
            await self.sender.send_text(to, text)
        except WhatsAppError as e:
            self._log(logging.WARNING, "send failed", error=e)

    async def _notify_unknown(self, msg: IncomingMessage) -> None:
        key = f"unknown_notified:{normalize_phone(msg.phone)}"
        if time.time() - float(self.store.get_kv(key, "0")) < 24 * 3600:
            return
        self.store.set_kv(key, str(time.time()))
        name = msg.profile_name or ""
        phone = normalize_phone(msg.phone)
        await self.notify_owner(
            f"👤 Пишет номер, которого нет в списке: +{phone} {name}\n«{(msg.text or '')[:300]}»\n"
            f"Чтобы бот ему отвечал: /добавить {phone} друг {name or 'Имя'}"
        )

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
