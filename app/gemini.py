"""Google Gemini generator (free tier) via the REST API.

Same interface as :class:`app.claude.ClaudeClient`, so the pipeline does not
care which model writes the reply. Get a free key at https://aistudio.google.com
"""
from __future__ import annotations

import asyncio
import base64
import logging

import httpx

from app.claude import GenerationError
from app.config import Settings
from app.prompts import SYSTEM_PROMPT, render_user_message

log = logging.getLogger(__name__)

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
RETRYABLE = {408, 429, 500, 502, 503, 504}


class GeminiClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._http = http or httpx.AsyncClient(timeout=settings.llm_timeout_seconds)

    async def _call(self, parts: list[dict], system: str, max_tokens: int) -> str:
        url = API_URL.format(model=self.settings.gemini_model)
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0.8, "maxOutputTokens": max_tokens},
        }
        headers = {"x-goog-api-key": self.settings.gemini_api_key.get_secret_value()}
        attempts = self.settings.llm_max_retries + 1
        for attempt in range(attempts):
            try:
                resp = await self._http.post(url, json=body, headers=headers)
            except httpx.TimeoutException as e:
                err = GenerationError("Gemini API timeout", retryable=True)
                err.__cause__ = e
            except httpx.HTTPError as e:
                err = GenerationError("Gemini API unreachable", retryable=True)
                err.__cause__ = e
            else:
                if resp.status_code < 400:
                    return self._extract(resp.json())
                if resp.status_code in (401, 403):
                    raise GenerationError("Gemini API key rejected - check GEMINI_API_KEY")
                if resp.status_code == 404:
                    raise GenerationError(f"Gemini model {self.settings.gemini_model!r} not found - check GEMINI_MODEL")
                message = ""
                try:
                    message = str((resp.json().get("error") or {}).get("message", ""))
                except ValueError:
                    pass
                if "location is not supported" in message.lower():
                    raise GenerationError("Gemini недоступен в вашей стране - используйте OPENROUTER_API_KEY")
                if "api key not valid" in message.lower():
                    raise GenerationError("Gemini API key rejected - check GEMINI_API_KEY")
                err = GenerationError(
                    f"Gemini API error {resp.status_code} {message[:120]}".strip(),
                    retryable=resp.status_code in RETRYABLE,
                )
                if not err.retryable:
                    raise err
            if attempt < attempts - 1:
                await asyncio.sleep(2**attempt)
        raise err

    @staticmethod
    def _extract(data: dict) -> str:
        block = (data.get("promptFeedback") or {}).get("blockReason")
        if block:
            raise GenerationError(f"Gemini blocked the request ({block})")
        candidates = data.get("candidates") or []
        if not candidates:
            raise GenerationError("Gemini returned no candidates")
        cand = candidates[0]
        if cand.get("finishReason") in ("SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST"):
            raise GenerationError(f"Gemini declined ({cand['finishReason']})")
        if cand.get("finishReason") == "MAX_TOKENS":
            raise GenerationError("Gemini reply was truncated (MAX_TOKENS)")
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
        if not text:
            raise GenerationError("Gemini returned an empty reply")
        return text

    async def generate(self, context: dict, feedback: str | None = None) -> str:
        user = render_user_message(context, feedback)
        return await self._call([{"text": user}], SYSTEM_PROMPT, self.settings.llm_max_tokens)

    async def describe_image(self, image: bytes, mime_type: str) -> str | None:
        parts = [
            {"inlineData": {"mimeType": mime_type, "data": base64.b64encode(image).decode()}},
            {
                "text": "Describe this photo in 1-2 short neutral sentences in Russian so someone can reply to "
                "it in a family chat. Mention people, occasion and mood only if clearly visible. No speculation."
            },
        ]
        try:
            return await self._call(parts, "You describe images briefly.", 1024)
        except GenerationError:
            log.exception("image description failed")
            return None
