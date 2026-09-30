"""Deterministic validation of a generated reply before it goes to WhatsApp."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.models import Fact, Mode

MAX_CHARS = {Mode.GREETING_ONLY: 300, Mode.FULL_CHAT: 700}
MAX_SENTENCES = {Mode.GREETING_ONLY: 4, Mode.FULL_CHAT: 7}
MAX_EMOJI = 3

# Technical / meta words that must never reach a relative.
_LEAK_PATTERNS = [
    r"system\s*prompt", r"системн\w*\s+(промпт|инструкц)", r"\bprompt\b", r"промпт",
    r"инструкци", r"дастурамал", r"\bmetadata\b", r"метадан", r"\bwebhook\b", r"вебхук",
    r"\bbackend\b", r"бэкенд", r"\bAPI\b", r"\bJSON\b", r"\bClaude\b", r"Anthropic", r"Клод",
    r"\bAI\b", r"\bИИ\b", r"искусственн\w+\s+интеллект", r"зеҳни\s+сунъӣ", r"language model",
    r"языков\w+\s+модел", r"\bассистент", r"\bassistant\b", r"known_facts", r"recent_messages",
    r"incoming_message", r"relative_id", r"age_group", r"GREETING_ONLY", r"FULL_CHAT",
    r"<\/?context>", r"[{}]", r"\bчат-?бот", r"\bбот\b", r"as an ai", r"i am an ai",
]
_LEAK_RE = re.compile("|".join(_LEAK_PATTERNS), re.IGNORECASE)

# Critical claims about the User that require supporting facts.
# (category, claim-pattern, pattern a known fact must match to allow the claim)
_CLAIMS: list[tuple[str, re.Pattern[str], re.Pattern[str]]] = [
    (
        "arrival/location",
        re.compile(r"\b(расидам|омадам|омада\s+расидам|ба\s+хона\s+омадам|приехал|доехал|я\s+дома)\b", re.I),
        re.compile(r"расид|омад|приехал|доехал|дома|хона", re.I),
    ),
    (
        "location",
        re.compile(
            r"\bман\s+(ҳозир\s+)?дар\s+\w+(\s+\w+)?\s+(ҳастам|мебошам)\b"
            r"|\bя\s+(сейчас\s+)?в\s+(?!порядк|норм|курс|восторг|шок|хорош|отличн|себе\b)\w+",
            re.I,
        ),
        re.compile(r"дар\s+\w+|живёт|живет|зиндагӣ|location|находится|шаҳр", re.I),
    ),
    (
        "work",
        re.compile(r"\b(дар\s+кор(\s+ҳастам)?|кор\s+карда\s+истодаам|на\s+работе|корам\s+зиёд)\b", re.I),
        re.compile(r"кор|работ|work", re.I),
    ),
    (
        "health",
        re.compile(r"\b(бемор\s+шудам|касал\s+шудам|беморам|касалам|я\s+заболел|я\s+болею)\b", re.I),
        re.compile(r"бемор|касал|болеет|заболел|health", re.I),
    ),
    (
        "visit/travel promise",
        re.compile(r"\b(пагоҳ|фардо|имрӯз|ҳафтаи\s+оянда|дар\s+ҳамин\s+ҳафта|завтра)\b[^.!?]{0,40}\b(меоям|меравам|мерасам|приеду|прилечу)\b|\b(меоям|мерасам|приеду)\b", re.I),
        re.compile(r"меояд|меоям|приедет|приеду|сафар|поездк|рейс|билет|visit|travel|trip", re.I),
    ),
    (
        "money promise",
        re.compile(r"\b(пул\s+мефиристам|пул\s+равон\s+мекунам|деньги\s+(отправлю|пришлю|переведу)|мефиристам\s+пул)\b", re.I),
        re.compile(r"пул|деньг|money|перевод", re.I),
    ),
]

_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF❤☺⭐]"
)
_CYR_RE = re.compile(r"[А-Яа-яЁёҒғӢӣҚқӮӯҲҳҶҷ]")
_LAT_RE = re.compile(r"[A-Za-z]")
_SENTENCE_RE = re.compile(r"[^.!?…]+[.!?…]*")


@dataclass
class ValidationResult:
    ok: bool
    text: str
    problems: list[str] = field(default_factory=list)

    @property
    def feedback(self) -> str:
        return "; ".join(self.problems)


def clean_reply(text: str) -> str:
    """Normalise cosmetic artefacts the model sometimes adds."""
    t = (text or "").strip()
    # strip wrapping quotes / code fences
    t = re.sub(r"^```\w*\s*|\s*```$", "", t).strip()
    if len(t) >= 2 and t[0] in "\"«'“" and t[-1] in "\"»'”":
        t = t[1:-1].strip()
    # strip leading labels like "Ответ:" / "Message:"
    t = re.sub(r"^(ответ|сообщение|message|reply|паём|ҷавоб)\s*:\s*", "", t, flags=re.I)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def count_emoji(text: str) -> int:
    return len(_EMOJI_RE.findall(text))


def validate_reply(text: str, mode: Mode, facts: list[Fact] | None = None) -> ValidationResult:
    t = clean_reply(text)
    problems: list[str] = []

    if not t:
        return ValidationResult(False, t, ["reply is empty"])

    leak = _LEAK_RE.search(t)
    if leak:
        problems.append(f"contains technical/meta content ({leak.group(0)!r})")

    cyr, lat = len(_CYR_RE.findall(t)), len(_LAT_RE.findall(t))
    if cyr == 0 or lat > cyr * 0.2:
        problems.append("must be written in Tajik Cyrillic")

    if len(t) > MAX_CHARS[mode]:
        problems.append(f"too long ({len(t)} chars, max {MAX_CHARS[mode]})")
    sentences = [s for s in _SENTENCE_RE.findall(t) if s.strip()]
    if len(sentences) > MAX_SENTENCES[mode]:
        problems.append(f"too many sentences ({len(sentences)})")

    if count_emoji(t) > MAX_EMOJI:
        problems.append("too many emojis")

    facts_text = " ".join(f.fact for f in (facts or []))
    for category, claim, support in _CLAIMS:
        if claim.search(t) and not support.search(facts_text):
            problems.append(f"states an unconfirmed {category} fact about the user")

    return ValidationResult(not problems, t, problems)


_RU = [
    ("reply is empty", "модель вернула пустой ответ"),
    ("contains technical/meta content", "в ответе служебные слова"),
    ("must be written in Tajik Cyrillic", "ответ не на таджикском (не кириллица)"),
    ("too long", "слишком длинно"),
    ("too many sentences", "слишком много предложений"),
    ("too many emojis", "слишком много эмодзи"),
    ("states an unconfirmed", "бот придумал факт о вас"),
]


def explain_problems(feedback: str | None) -> str:
    """Human (Russian) version of validator feedback for the owner."""
    if not feedback:
        return "неизвестно"
    out = []
    for part in feedback.split("; "):
        ru = next((r for en, r in _RU if part.startswith(en)), part)
        detail = part[part.find("("):] if "(" in part else ""
        if part.startswith("states an unconfirmed"):
            detail = "(" + part.removeprefix("states an unconfirmed ").removesuffix(" fact about the user") + ")"
        out.append(f"{ru} {detail}".strip())
    return "; ".join(out)
