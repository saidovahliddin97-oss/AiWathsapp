from app.models import Fact, Mode
from app.validation import clean_reply, count_emoji, validate_reply


def test_accepts_natural_tajik():
    r = validate_reply("Ваалейкум ассалом, Модарҷон! Ташаккур, хубам 🙂", Mode.FULL_CHAT)
    assert r.ok, r.problems


def test_empty():
    assert not validate_reply("   ", Mode.FULL_CHAT).ok


def test_strips_quotes_and_labels():
    assert clean_reply('«Салом, Модарҷон!»') == "Салом, Модарҷон!"
    assert clean_reply("Ответ: Салом!") == "Салом!"


def test_rejects_meta_leaks():
    for bad in ["Ман AI ҳастам", "Бо Claude навишта шуд", "Системный промпт гуфт", '{"mode": "FULL_CHAT"}',
                "Ин ҷавоби бот аст"]:
        assert not validate_reply(bad, Mode.FULL_CHAT).ok, bad


def test_rejects_non_cyrillic():
    assert not validate_reply("Salom, Modarjon! Khubam.", Mode.FULL_CHAT).ok


def test_length_limits():
    long = "Салом, Модарҷон. " * 60
    assert not validate_reply(long, Mode.FULL_CHAT).ok
    assert not validate_reply("Салом. " * 10, Mode.GREETING_ONLY).ok


def test_emoji_limit():
    assert count_emoji("Салом 🙂🙂") == 2
    assert not validate_reply("Салом 🎉🎉🎉🎉", Mode.FULL_CHAT).ok


def test_fabricated_claims():
    for bad in ["Ҳа, ба хона расидам.", "Ман дар кор ҳастам ҳозир.", "Пагоҳ меоям!", "Пул мефиристам, хавотир нашавед.",
                "Каме бемор шудам."]:
        r = validate_reply(bad, Mode.FULL_CHAT)
        assert not r.ok and "unconfirmed" in r.feedback, bad


def test_claims_allowed_with_facts():
    facts = [Fact(fact="Корбар имрӯз ба хона расид", source="user")]
    assert validate_reply("Ҳа, Модарҷон, ба хона расидам.", Mode.FULL_CHAT, facts).ok


def test_neutral_closing_ok():
    assert validate_reply("Хуб, ҳозир каме банд ҳастам, баъдтар боз гап мезанем.", Mode.FULL_CHAT).ok


def test_explain_problems_russian():
    from app.validation import explain_problems
    r = validate_reply("Ҳа, ба хона расидам. Salom hello world friend", Mode.FULL_CHAT)
    text = explain_problems(r.feedback)
    assert "не на таджикском" in text and "придумал факт о вас (arrival/location)" in text


def test_russian_idioms_not_location():
    assert validate_reply("Привет, Али! Я в порядке, спасибо.", Mode.FULL_CHAT).ok
    assert not validate_reply("Я сейчас в Москве.", Mode.FULL_CHAT).ok
