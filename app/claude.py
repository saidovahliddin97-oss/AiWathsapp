"""Claude API client wrapper.

Takes a system prompt and a structured context, returns plain reply text.
Transport-level retries/timeouts are handled by the SDK (``max_retries`` /
``timeout``); everything that still fails surfaces as :class:`GenerationError`.
Secrets are never logged.
"""
from __future__ import annotations

import base64
import logging
import random
from typing import Protocol

import anthropic

from app.config import Settings
from app.models import Mode
from app.prompts import SYSTEM_PROMPT, render_user_message

log = logging.getLogger(__name__)


class GenerationError(Exception):
    def __init__(self, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class ReplyGenerator(Protocol):
    async def generate(self, context: dict, feedback: str | None = None) -> str: ...

    async def describe_image(self, image: bytes, mime_type: str) -> str | None: ...


class ClaudeClient:
    """Real generator backed by the Claude Messages API."""

    FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(self, settings: Settings, client: anthropic.AsyncAnthropic | None = None) -> None:
        self.settings = settings
        self._client = client or anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )

    async def _create(self, messages: list[dict], system: str) -> str:
        kwargs: dict = dict(
            model=self.settings.llm_model,
            max_tokens=self.settings.llm_max_tokens,
            system=system,
            messages=messages,
            output_config={"effort": self.settings.llm_effort},
        )
        try:
            if self.settings.llm_use_fallbacks:
                response = await self._client.beta.messages.create(
                    betas=[self.FALLBACK_BETA], fallbacks="default", **kwargs
                )
            else:
                response = await self._client.messages.create(**kwargs)
        except anthropic.APITimeoutError as e:
            raise GenerationError("Claude API timeout", retryable=True) from e
        except anthropic.RateLimitError as e:
            raise GenerationError("Claude API rate limited", retryable=True) from e
        except anthropic.AuthenticationError as e:
            raise GenerationError("Claude API authentication failed - check ANTHROPIC_API_KEY") from e
        except anthropic.BadRequestError as e:
            raise GenerationError(f"Claude API rejected the request ({e.status_code})") from e
        except anthropic.APIStatusError as e:
            raise GenerationError(f"Claude API error {e.status_code}", retryable=e.status_code >= 500) from e
        except anthropic.APIConnectionError as e:
            raise GenerationError("Claude API unreachable", retryable=True) from e

        if response.stop_reason == "refusal":
            raise GenerationError("Claude declined to answer (refusal)")
        if response.stop_reason == "max_tokens":
            raise GenerationError("Claude reply was truncated (max_tokens)")
        text = "".join(block.text for block in response.content if block.type == "text").strip()
        if not text:
            raise GenerationError("Claude returned an empty reply")
        return text

    async def generate(self, context: dict, feedback: str | None = None) -> str:
        user = render_user_message(context, feedback)
        return await self._create([{"role": "user", "content": user}], SYSTEM_PROMPT)

    async def describe_image(self, image: bytes, mime_type: str) -> str | None:
        """Short neutral description of an image a relative sent (vision)."""
        content = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": mime_type, "data": base64.b64encode(image).decode()},
            },
            {
                "type": "text",
                "text": "Describe this photo in 1-2 short neutral sentences in Russian so someone can reply to "
                "it in a family chat. Mention people, occasion and mood only if clearly visible. No speculation.",
            },
        ]
        try:
            return await self._create([{"role": "user", "content": content}], "You describe images briefly.")
        except GenerationError:
            log.exception("image description failed")
            return None


class OfflineGenerator:
    """Template-based generator used ONLY for local demos when no API key is set.

    It is deliberately simple and never states facts about the user.
    """

    GREETINGS = [
        "Ассалому алейкум, {a}! Аҳволатон хуб аст? Саломат бошед.",
        "Салом, {a}! Чӣ хел ҳастед? Дилам барои Шумо танг шуд.",
        "Ассалому алейкум, {a}! Хонаву дар ҳама саломатанд?",
        "Салом, {a}! Умедворам, ки ҳамааш хуб аст. Худо нигаҳдор бошад.",
    ]
    GREETINGS_INFORMAL = ["Салом, {a}! Чӣ гапҳо?", "Салом, {a}! Корҳо чӣ хел?", "Салом, {a}! Хуб ҳастӣ?"]

    async def generate(self, context: dict, feedback: str | None = None) -> str:
        rel = context["relative"]
        elder = rel["age_group"] == "elder"
        a = rel.get("address") or rel["name"]
        if context["mode"] == Mode.GREETING_ONLY.value:
            pool = self.GREETINGS if elder else self.GREETINGS_INFORMAL
            used = set(context.get("recent_greetings") or [])
            options = [g.format(a=a) for g in pool if g.format(a=a) not in used] or [pool[0].format(a=a)]
            return random.choice(options)

        text = (context.get("incoming_message") or "").lower()
        media = context.get("incoming_media")
        if media and "voice" in media:
            return f"{a}, овозатонро гирифтам, ташаккур. Баъдтар бо диққат гӯш мекунам." if elder else "Овозатро гирифтам, баъдтар гӯш мекунам 🙂"
        if media and "image" in media:
            return f"Чӣ акси зебо, {a}! Ташаккур, ки фиристодед." if elder else "Акси зебо! Раҳмат 🙂"
        if any(w in text for w in ("бемор", "касал", "вафот", "мурд", "бад шуд", "плохо")):
            return f"Вой, {a}, ғам нахӯред. Худо шифо диҳад, ҳамааш хуб мешавад." if elder else "Вой, ғам нахӯр, ҳамааш хуб мешавад."
        if any(w in text for w in ("табрик", "хурсанд", "муборак", "тӯй", "туй", "қабул шуд", "дохил шуд")):
            return f"Табрик, {a}! Хеле хурсанд шудам, муборак бошад!" if elder else "Табрик! Хеле хурсанд шудам 🎉"
        if any(w in text for w in ("куҷо", "кай", "меоӣ", "меоед", "омадӣ", "расидӣ", "где", "когда")):
            return f"{a}, ҳоло аниқ гуфта наметавонам, баъдтар ба Шумо хабар медиҳам." if elder else "Ҳоло аниқ намедонам, баъдтар мегӯям."
        if any(w in text for w in ("салом", "ассалом", "чӣ хел", "чи хел", "хубед", "хубӣ")):
            return f"Ваалейкум ассалом, {a}! Ташаккур, ман хуб. Шумо чӣ хел, саломатиатон хуб аст?" if elder else "Салом! Хуб, раҳмат. Худат чӣ хел?"
        return f"Ташаккур, {a}! Хуб, фаҳмидам." if elder else "Хуб, фаҳмидам 🙂"

    async def describe_image(self, image: bytes, mime_type: str) -> str | None:
        return None


def build_generator(settings: Settings) -> ReplyGenerator | None:
    if settings.claude_enabled:
        return ClaudeClient(settings)
    if settings.allow_offline_generator:
        log.warning("ANTHROPIC_API_KEY is not set - using the OFFLINE demo generator")
        return OfflineGenerator()
    return None
