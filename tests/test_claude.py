"""Claude client: request shape and error handling, using a mocked SDK client."""
from __future__ import annotations

import types

import anthropic
import httpx
import pytest

from app.claude import ClaudeClient, GenerationError, OfflineGenerator
from app.config import Settings
from app.models import AgeGroup, Mode, Relative
from app.prompts import SYSTEM_PROMPT, build_context, render_user_message


def _resp(text="Салом, Модарҷон!", stop="end_turn"):
    return types.SimpleNamespace(
        stop_reason=stop,
        content=[types.SimpleNamespace(type="thinking", thinking=""), types.SimpleNamespace(type="text", text=text)],
    )


class FakeMessages:
    def __init__(self, result):
        self.result = result
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def make_client(result, **overrides):
    msgs = FakeMessages(result)
    sdk = types.SimpleNamespace(messages=msgs, beta=types.SimpleNamespace(messages=msgs))
    s = Settings(_env_file=None, anthropic_api_key="test-key", **overrides)
    return ClaudeClient(s, client=sdk), msgs


REL = Relative(id="mom", phone="1", name="Модар", relation="mother", age_group=AgeGroup.ELDER, address="Модарҷон")


def ctx():
    return build_context(Mode.FULL_CHAT, REL, [], [], incoming_text="Салом")


async def test_request_shape_with_fallbacks():
    client, msgs = make_client(_resp())
    assert await client.generate(ctx()) == "Салом, Модарҷон!"
    kw = msgs.kwargs
    assert kw["model"] == "claude-opus-5-5"
    assert kw["system"] == SYSTEM_PROMPT
    assert kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["output_config"] == {"effort": "low"}
    assert "<context>" in kw["messages"][0]["content"]
    assert "temperature" not in kw


async def test_request_without_fallbacks():
    client, msgs = make_client(_resp(), llm_use_fallbacks=False)
    await client.generate(ctx())
    assert "fallbacks" not in msgs.kwargs and "betas" not in msgs.kwargs


def _req():
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


@pytest.mark.parametrize(
    "exc, retryable",
    [
        (lambda: anthropic.APITimeoutError(request=_req()), True),
        (lambda: anthropic.APIConnectionError(request=_req()), True),
        (lambda: anthropic.RateLimitError("rl", response=httpx.Response(429, request=_req()), body=None), True),
        (lambda: anthropic.InternalServerError("x", response=httpx.Response(500, request=_req()), body=None), True),
        (lambda: anthropic.BadRequestError("x", response=httpx.Response(400, request=_req()), body=None), False),
        (lambda: anthropic.AuthenticationError("x", response=httpx.Response(401, request=_req()), body=None), False),
    ],
)
async def test_errors_mapped(exc, retryable):
    client, _ = make_client(exc())
    with pytest.raises(GenerationError) as ei:
        await client.generate(ctx())
    assert ei.value.retryable is retryable


async def test_refusal_and_empty():
    client, _ = make_client(_resp(stop="refusal", text=""))
    with pytest.raises(GenerationError, match="refusal"):
        await client.generate(ctx())
    client, _ = make_client(_resp(text="  "))
    with pytest.raises(GenerationError, match="empty"):
        await client.generate(ctx())


def test_api_key_not_in_repr():
    s = Settings(_env_file=None, anthropic_api_key="sk-ant-secret")
    assert "sk-ant-secret" not in repr(s) and "sk-ant-secret" not in str(s.model_dump())


def test_context_is_minimal():
    c = ctx()
    assert set(c) == {"mode", "relative", "user_context", "relative_context", "recent_messages", "incoming_message"}
    assert "phone" not in c["relative"]
    msg = render_user_message(c, feedback="too long")
    assert "too long" in msg and "FULL_CHAT" in msg


async def test_offline_generator_passes_validation():
    from app.validation import validate_reply
    gen = OfflineGenerator()
    for text in ["Ассалому алейкум!", "Кай меоӣ?", "Бобо бемор шуд", "Табрик, тӯй шуд!", "ок"]:
        out = await gen.generate(build_context(Mode.FULL_CHAT, REL, [], [], incoming_text=text))
        assert validate_reply(out, Mode.FULL_CHAT).ok, out
    g = await gen.generate(build_context(Mode.GREETING_ONLY, REL, [], [], recent_greetings=[]))
    assert validate_reply(g, Mode.GREETING_ONLY).ok
