import httpx
import pytest

from app.claude import GenerationError, build_generator
from app.config import Settings
from app.models import Mode
from app.openrouter import FALLBACK_ROUTER, OpenRouterClient, rank_free_models
from app.prompts import build_context
from app.validation import validate_reply

FREE = {"prompt": "0", "completion": "0"}
MODELS = [
    {"id": "nvidia/nemotron-3-ultra-550b-a55b:free", "pricing": FREE, "context_length": 1000000},
    {"id": "google/gemma-4-31b-it:free", "pricing": FREE, "context_length": 262144,
     "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]}},
    {"id": "qwen/qwen3.8-27b:free", "pricing": FREE, "context_length": 262144},
    {"id": "google/lyria-3-pro-preview", "pricing": FREE, "architecture": {"output_modalities": ["audio"]}},
    {"id": "cohere/north-mini-code:free", "pricing": FREE},
    {"id": "anthropic/claude-opus-5.5", "pricing": {"prompt": "0.000004", "completion": "0.00002"}},
]


def test_rank_prefers_multilingual_free_models():
    ranked = rank_free_models(MODELS)
    assert ranked[:3] == ["google/gemma-4-31b-it:free", "qwen/qwen3.8-27b:free", "nvidia/nemotron-3-ultra-550b-a55b:free"]
    assert ranked[-1] == FALLBACK_ROUTER
    assert not any("claude" in m or "lyria" in m or "code" in m for m in ranked)


def ctx():
    from tests.test_claude import REL
    return build_context(Mode.FULL_CHAT, REL, [], [], incoming_text="Салом")


def client(handler, **kw):
    s = Settings(_env_file=None, openrouter_api_key="or-key", **kw)
    return OpenRouterClient(s, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_falls_back_to_next_free_model_and_strips_thinking():
    used = []

    def handler(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": MODELS})
        import json
        model = json.loads(req.content)["model"]
        used.append(model)
        assert req.headers["authorization"] == "Bearer or-key"
        if model.startswith("google/"):
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "<think>hm</think>Салом, Модарҷон!"},
                                                       "finish_reason": "stop"}]})

    c = client(handler)
    assert await c.generate(ctx()) == "Салом, Модарҷон!"
    assert used == ["google/gemma-4-31b-it:free", "qwen/qwen3.8-27b:free"] and c.last_model == "qwen/qwen3.8-27b:free"


async def test_bad_key_and_all_models_down():
    with pytest.raises(GenerationError, match="key rejected"):
        await client(lambda r: httpx.Response(401), openrouter_model="x:free").generate(ctx())
    with pytest.raises(GenerationError, match="error 503"):
        await client(lambda r: httpx.Response(503), openrouter_model="a:free,b:free").generate(ctx())


def test_provider_auto_prefers_openrouter_over_gemini():
    s = Settings(_env_file=None, openrouter_api_key="k", gemini_api_key="g", anthropic_api_key="")
    assert type(build_generator(s)).__name__ == "OpenRouterClient"


def test_reply_language_follows_relative():
    assert validate_reply("Привет, Али! Всё хорошо, спасибо.", Mode.FULL_CHAT, incoming="Привет, как дела?").ok
    assert validate_reply("Hi Ali! All good, thanks.", Mode.FULL_CHAT, incoming="Hi, how are you?").ok
    assert not validate_reply("Hi Ali! All good.", Mode.FULL_CHAT, incoming="Салом, чӣ хел?").ok
