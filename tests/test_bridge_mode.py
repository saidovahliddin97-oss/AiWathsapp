"""Free linked-device mode: owner commands, manual pause, groups, notifications, scheduler."""
from __future__ import annotations

import time

import httpx
import pytest

from app import scheduler
from app.bridge import BridgeSender
from app.config import Settings
from app.gemini import GeminiClient
from app.claude import GenerationError
from app.models import IncomingMessage, Mode
from app.prompts import build_context
from app.webhook import DryRunSender
from app.whatsapp import WhatsAppError
from tests.conftest import FakeGenerator, make_assistant

MOM = "992900000001"
GROUP = "120363000000000001@g.us"


@pytest.fixture
def personal(settings):
    settings.personal_number = True
    settings.owner_names = "Алишер, Алик"
    return settings


def owner_cmd(text, mid):
    return IncomingMessage(message_id=mid, phone="992911111111", text=text, from_me=True, self_chat=True)


async def test_owner_commands_toggle_autopilot(personal, store):
    sender = DryRunSender()
    gen = FakeGenerator(["Хуб, Модарҷон!"])
    a = make_assistant(personal, store, gen, sender)
    res = await a.process(owner_cmd("/авто выкл", "c1"))
    assert res.status == "owner_command" and not a.autopilot_on()
    assert all(r.mode is Mode.GREETING_ONLY for r in store.list_relatives())
    assert sender.sent[-1][0] == "me"
    r = await a.process(IncomingMessage(message_id="m1", phone=MOM, text="Салом"))
    assert r.status == "skipped_greeting_only" and gen.calls == []
    assert sender.sent[-1] == ("me", "✉️ Модарҷон: Салом")
    # enable just one person for testing
    await a.process(owner_cmd("/авто вкл модар", "c2"))
    assert (await a.process(IncomingMessage(message_id="m2", phone=MOM, text="Салом"))).status == "dry_run"
    assert (await a.process(IncomingMessage(message_id="m3", phone="992900000004", text="Салом"))).status == "skipped_greeting_only"
    await a.process(owner_cmd("/авто вкл", "c3"))
    assert all(r.mode is Mode.FULL_CHAT for r in store.list_relatives())


async def test_old_global_flag_migrated(settings, store):
    store.set_kv("autopilot", "0")
    a = make_assistant(settings, store, FakeGenerator())
    assert all(r.mode is Mode.GREETING_ONLY for r in store.list_relatives()) and a.default_mode() is Mode.GREETING_ONLY


async def test_modes_survive_config_reload(settings, store, tmp_path):
    from app.memory import load_relatives_config
    f = tmp_path / "r.json"
    f.write_text('{"relatives": [{"id": "mom", "phone": "992900000001", "name": "Модар", "mode": "FULL_CHAT"}]}')
    store.set_relative_mode("mom", Mode.GREETING_ONLY)
    load_relatives_config(store, f)
    assert store.get_relative("mom").mode is Mode.GREETING_ONLY


async def test_owner_command_per_relative_and_greet(personal, store):
    sender = DryRunSender()
    a = make_assistant(personal, store, FakeGenerator(["Ассалому алейкум, Модарҷон! Саломат бошед."]), sender)
    await a.process(owner_cmd("/авто выкл модар", "c1"))
    assert store.get_relative("mom").mode is Mode.GREETING_ONLY
    res = await a.process(owner_cmd("/привет Модарҷон", "c2"))
    assert "Ассалому" in res.reply
    assert (MOM, "Ассалому алейкум, Модарҷон! Саломат бошед.") in sender.sent
    assert (await a.process(owner_cmd("/список", "c3"))).reply.count("•") == 4
    assert "Переписывается: 2 из 4" in (await a.process(owner_cmd("/статус", "c4"))).reply
    assert "/пауза" in (await a.process(owner_cmd("/чтото", "c5"))).reply


async def test_add_and_remove_contact(personal, store, tmp_path):
    import json
    personal.relatives_file = str(tmp_path / "relatives.json")
    gen = FakeGenerator(["Салом, дӯстам!"])
    a = make_assistant(personal, store, gen)
    res = await a.process(owner_cmd("/добавить +992 90 123-45-67 друг Тест", "c1"))
    assert res.reply.startswith("Добавил") and "+992901234567" in res.reply
    rel = store.find_relative_by_phone("992901234567")
    assert rel.relation == "friend" and rel.address == "Тест"
    saved = json.loads((tmp_path / "relatives.json").read_text(encoding="utf-8"))
    assert saved["relatives"][0]["phone"] == "992901234567"
    assert (await a.process(IncomingMessage(message_id="t1", phone="992901234567", text="салом"))).status == "dry_run"
    res = await a.process(owner_cmd("/удалить Тест", "c2"))
    assert res.reply.startswith("Удалил") and store.find_relative_by_phone("992901234567") is None
    assert json.loads((tmp_path / "relatives.json").read_text(encoding="utf-8"))["relatives"] == []
    assert (await a.process(owner_cmd("/добавить 12 мама", "c3"))).reply.startswith("Пример")


async def test_owner_from_personal_phone_in_separate_number_mode(settings, store):
    settings.owner_phone = "+992 911 111 111"
    sender = DryRunSender()
    a = make_assistant(settings, store, FakeGenerator(), sender)
    res = await a.process(IncomingMessage(message_id="c1", phone="992911111111", text="/пауза 2"))
    assert res.status == "owner_command" and a.paused("mom")
    assert sender.sent[-1][0] == "992911111111"
    await a.process(IncomingMessage(message_id="c2", phone="992911111111", text="/старт"))
    assert not a.paused("mom")


async def test_manual_reply_pauses_bot(personal, store):
    gen = FakeGenerator(["Хуб, Модарҷон!"])
    a = make_assistant(personal, store, gen)
    rec = await a.process(IncomingMessage(message_id="o1", phone=MOM, text="Ҳа, модарҷон, хубам", from_me=True))
    assert rec.status == "owner_message_recorded"
    assert store.recent_messages("mom")[-1].sender == "assistant"
    res = await a.process(IncomingMessage(message_id="m1", phone=MOM, text="Нон хӯрдӣ?"))
    assert res.status == "skipped_manual_pause" and gen.calls == []
    store.set_kv("manual:mom", str(time.time() - 3 * 3600))
    assert (await a.process(IncomingMessage(message_id="m2", phone=MOM, text="Нон хӯрдӣ?"))).status == "dry_run"


async def test_group_only_answers_when_addressed(personal, store):
    sender = DryRunSender()
    gen = FakeGenerator(["Ваалейкум ассалом, Карим тағо!"])
    a = make_assistant(personal, store, gen, sender)
    base = dict(phone="992900000004", chat_id=GROUP, is_group=True, group_name="Оила")
    # groups are off by default: nothing is stored or answered, even when addressed
    r0 = await a.process(IncomingMessage(message_id="g0", text="Алишер, салом!", **base))
    assert r0.detail == "group disabled" and gen.calls == [] and store.get_relative("grp_120363000000000001") is None
    assert "Оила — выключено" in (await a.process(owner_cmd("/группы", "c0"))).reply
    assert "бот будет отвечать" in (await a.process(owner_cmd("/группа вкл оила", "c1"))).reply

    r1 = await a.process(IncomingMessage(message_id="g1", text="Ҳама салом!", **base))
    assert r1.status == "group_not_addressed" and gen.calls == []
    r2 = await a.process(IncomingMessage(message_id="g2", text="Алишер, чӣ хел ту?", **base))
    assert r2.status == "dry_run"
    assert sender.sent[-1] == (GROUP, "Ваалейкум ассалом, Карим тағо!")
    ctx = gen.calls[0][0]
    assert ctx["group_speaker"]["name"] == "Карим тағо" and ctx["relative"]["relation"] == "family_group"
    assert ctx["recent_messages"][0]["text"] == "Карим тағо: Ҳама салом!"
    r3 = await a.process(IncomingMessage(message_id="g3", text="ок", addressed_to_bot=True, **base))
    assert r3.status == "dry_run"
    await a.process(owner_cmd("/группа выкл Оила", "c2"))
    assert (await a.process(IncomingMessage(message_id="g4", text="Алишер?", **base))).detail == "group disabled"


async def test_important_news_and_failures_notify_owner(personal, store):
    sender = DryRunSender()
    a = make_assistant(personal, store, FakeGenerator(error=GenerationError("refusal")), sender)
    await a.process(IncomingMessage(message_id="m1", phone=MOM, text="Бобоят бемор шуд, дар беморхона аст."))
    texts = [t for to, t in sender.sent if to == "me"]
    assert any(t.startswith("⚠️") for t in texts) and any(t.startswith("❗") for t in texts)


async def test_unknown_sender_notified_once(personal, store):
    sender = DryRunSender()
    a = make_assistant(personal, store, FakeGenerator(), sender)
    for i in range(2):
        await a.process(IncomingMessage(message_id=f"u{i}", phone="15550001111", text="hi", profile_name="Bob"))
    assert len([1 for to, t in sender.sent if t.startswith("👤")]) == 1


async def test_scheduler_greets_due_relatives_only(settings, store):
    settings.greeting_hours = "0-24"
    sender = DryRunSender()
    a = make_assistant(settings, store, FakeGenerator(["Ассалому алейкум! Саломат бошед."]), sender)
    slept = []

    async def fake_sleep(sec):
        slept.append(sec)

    # first run only sets the baseline -> nobody is greeted right after install
    assert await scheduler.run_once(a, sleep=fake_sleep) == 0
    store.set_kv("greetings_baseline", str(time.time() - 8 * 86400))
    store.add_message("sis", "relative", "салом")  # recent contact -> not due
    sent = await scheduler.run_once(a, sleep=fake_sleep)
    assert sent == 3 and len(slept) == 2 and all(60 <= s <= 300 for s in slept)
    assert "992900000005" not in [to for to, _ in sender.sent]
    # greeted relatives are not due again
    assert await scheduler.run_once(a, sleep=fake_sleep) == 0


async def test_scheduler_respects_window_and_greet_all(settings, store):
    settings.greeting_hours = "0-0"
    a = make_assistant(settings, store, FakeGenerator(["Салом! Саломат бошед."]))

    async def no_sleep(_):
        pass

    store.set_kv("greetings_baseline", str(time.time() - 30 * 86400))
    assert await scheduler.run_once(a, sleep=no_sleep) == 0  # outside hours
    store.set_kv("greet_all_requested", str(time.time()))
    assert await scheduler.run_once(a, sleep=no_sleep) == 4  # /привет всем ignores the window
    assert await scheduler.run_once(a, sleep=no_sleep) == 0


# ---------------------------------------------------------------- Gemini
def gemini(handler, **kw):
    s = Settings(_env_file=None, gemini_api_key="g-key", llm_max_retries=1, **kw)
    return GeminiClient(s, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def ctx():
    from tests.test_claude import REL

    return build_context(Mode.FULL_CHAT, REL, [], [], incoming_text="Салом")


async def test_gemini_success():
    seen = {}

    def handler(req):
        seen["key"], seen["url"] = req.headers["x-goog-api-key"], str(req.url)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "Салом, Модарҷон!"}]}, "finishReason": "STOP"}]})

    assert await gemini(handler).generate(ctx()) == "Салом, Модарҷон!"
    assert seen["key"] == "g-key" and "gemini-flash-latest:generateContent" in seen["url"]


async def test_gemini_retries_then_errors(monkeypatch):
    calls = []

    async def no_sleep(_):
        pass

    monkeypatch.setattr("app.gemini.asyncio.sleep", no_sleep)

    def handler(req):
        calls.append(1)
        return httpx.Response(429 if len(calls) == 1 else 200, json={"candidates": [{"content": {"parts": [{"text": "Хуб!"}]}}]})

    assert await gemini(handler).generate(ctx()) == "Хуб!" and len(calls) == 2
    with pytest.raises(GenerationError, match="not found"):
        await gemini(lambda r: httpx.Response(404, json={})).generate(ctx())
    with pytest.raises(GenerationError, match="blocked"):
        await gemini(lambda r: httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})).generate(ctx())


async def test_bridge_sender():
    seen = {}

    def handler(req):
        seen["token"], seen["body"] = req.headers["x-bridge-token"], req.content
        return httpx.Response(200, json={"id": "ABC"})

    s = Settings(_env_file=None, bridge_token="bt")
    sender = BridgeSender(s, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await sender.send_text("992900000001", "Салом", quote_id="Q") == "ABC"
    assert seen["token"] == "bt" and b'"quote_id":"Q"' in seen["body"]
    down = BridgeSender(s, http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))))
    with pytest.raises(WhatsAppError) as ei:
        await down.send_text("1", "x")
    assert ei.value.retryable
