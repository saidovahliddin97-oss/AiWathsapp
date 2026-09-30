"""OpenRouter generator: one free key, many models (https://openrouter.ai/keys).

With OPENROUTER_MODEL=auto the client picks currently free text models from
OpenRouter's public model list (preferring strong multilingual ones) and falls
back to the next one when a model is rate-limited or unavailable.
"""
from __future__ import annotations

import base64
import logging
import re
import time

import httpx

from app.claude import GenerationError
from app.config import Settings
from app.prompts import SYSTEM_PROMPT, render_user_message

log = logging.getLogger(__name__)

API = "https://openrouter.ai/api/v1"
# Preferred free model families, best first (matched against model ids).
PREFERENCE = [
    r"google/gemma-\d.*-it:free",
    r"qwen/qwen\d.*:free",
    r"deepseek/.*:free",
    r"nvidia/nemotron-.*(ultra|super).*:free",
    r"meta-llama/.*:free",
    r"mistralai/.*:free",
]
EXCLUDE = re.compile(r"code|safety|guard|lyria|embed|audio|tts", re.I)
FALLBACK_ROUTER = "openrouter/free"
MODELS_TTL = 6 * 3600
_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)


def rank_free_models(models: list[dict]) -> list[str]:
    """Free text-output models ordered by PREFERENCE."""
    free = []
    for m in models:
        pricing = m.get("pricing") or {}
        if str(pricing.get("prompt")) != "0" or str(pricing.get("completion")) != "0":
            continue
        out = (m.get("architecture") or {}).get("output_modalities") or ["text"]
        if "text" not in out or EXCLUDE.search(m.get("id", "")):
            continue
        free.append(m)
    ranked: list[str] = []
    for pattern in PREFERENCE:
        for m in sorted(free, key=lambda m: -(m.get("context_length") or 0)):
            if re.search(pattern, m["id"]) and m["id"] not in ranked:
                ranked.append(m["id"])
    if FALLBACK_ROUTER not in ranked:
        ranked.append(FALLBACK_ROUTER)
    return ranked


class OpenRouterClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._http = http or httpx.AsyncClient(timeout=max(settings.llm_timeout_seconds, 60.0))
        self._models: list[str] = []
        self._vision: set[str] = set()
        self._models_at = 0.0
        self.last_model: str | None = None

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.openrouter_api_key.get_secret_value()}",
            "HTTP-Referer": "https://github.com/saidovahliddin97-oss/AiWathsapp",
            "X-Title": "Family Assistant",
        }

    async def models(self) -> list[str]:
        configured = self.settings.openrouter_model.strip()
        if configured and configured != "auto":
            return [m.strip() for m in configured.split(",") if m.strip()]
        if self._models and time.time() - self._models_at < MODELS_TTL:
            return self._models
        try:
            resp = await self._http.get(f"{API}/models")
            resp.raise_for_status()
            data = resp.json().get("data", [])
            self._models = rank_free_models(data)
            self._vision = {
                m["id"] for m in data if "image" in ((m.get("architecture") or {}).get("input_modalities") or [])
            }
            self._models_at = time.time()
            log.info("openrouter free models: %s", ", ".join(self._models[:5]))
        except (httpx.HTTPError, ValueError, KeyError):
            log.warning("could not load the OpenRouter model list, using the free router")
            self._models = self._models or [FALLBACK_ROUTER]
        return self._models

    async def _chat(self, messages: list[dict], max_tokens: int, models: list[str] | None = None) -> str:
        last_error: GenerationError | None = None
        for model in (models or await self.models())[:4]:
            body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.8}
            try:
                resp = await self._http.post(f"{API}/chat/completions", json=body, headers=self._headers)
            except httpx.TimeoutException:
                last_error = GenerationError("OpenRouter timeout", retryable=True)
                continue
            except httpx.HTTPError:
                last_error = GenerationError("OpenRouter unreachable", retryable=True)
                continue
            if resp.status_code in (401, 403):
                raise GenerationError("OpenRouter key rejected - check OPENROUTER_API_KEY")
            if resp.status_code >= 400:
                last_error = GenerationError(f"OpenRouter {model}: error {resp.status_code} {_error_text(resp)}",
                                             retryable=True)
                continue  # rate-limited / unavailable -> next free model
            try:
                data = resp.json()
                if data.get("error"):
                    last_error = GenerationError(f"OpenRouter {model}: {data['error'].get('message', '')[:120]}")
                    continue
                choice = data["choices"][0]
                text = _THINK.sub("", choice["message"].get("content") or "").strip()
            except (ValueError, KeyError, IndexError, TypeError):
                last_error = GenerationError(f"OpenRouter {model}: unexpected response")
                continue
            if choice.get("finish_reason") == "length" and not text:
                last_error = GenerationError(f"OpenRouter {model}: reply truncated")
                continue
            if not text:
                last_error = GenerationError(f"OpenRouter {model}: empty reply")
                continue
            self.last_model = model
            return text
        raise last_error or GenerationError("OpenRouter: no model available")

    async def generate(self, context: dict, feedback: str | None = None) -> str:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": render_user_message(context, feedback)},
        ]
        return await self._chat(messages, 2048)

    async def describe_image(self, image: bytes, mime_type: str) -> str | None:
        models = [m for m in await self.models() if m in self._vision]
        if not models:
            return None
        url = f"data:{mime_type};base64,{base64.b64encode(image).decode()}"
        messages = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": url}},
            {"type": "text", "text": "Describe this photo in 1-2 short neutral sentences in Russian so someone can "
             "reply to it in a family chat. Mention people, occasion and mood only if clearly visible."},
        ]}]
        try:
            return await self._chat(messages, 512, models=models)
        except GenerationError:
            log.exception("image description failed")
            return None


def _error_text(resp: httpx.Response) -> str:
    try:
        return str((resp.json().get("error") or {}).get("message", ""))[:120]
    except ValueError:
        return ""
