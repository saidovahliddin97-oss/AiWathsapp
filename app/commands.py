"""Owner commands sent over WhatsApp (to your own "message yourself" chat in
personal-number mode, or from your personal phone to the bot's number).

Examples:  /статус   /авто выкл   /авто вкл Модар   /привет всем   /пауза 3
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from app.models import Mode

if TYPE_CHECKING:
    from app.webhook import Assistant

HELP = """Командаҳо / Команды:
/статус — состояние бота
/список — родственники и режимы
/авто вкл | выкл — автопилот для всех
/авто вкл <имя> — полноценное общение с человеком (FULL_CHAT)
/авто выкл <имя> — только приветствия (GREETING_ONLY)
/привет <имя> | всем — отправить приветствие сейчас
/рассылка вкл | выкл — приветствия по расписанию
/пауза <часы> — бот молчит N часов
/старт — снять паузу"""

ON = {"on", "вкл", "да", "1", "включить", "фаъол"}
OFF = {"off", "выкл", "нет", "0", "выключить", "хомӯш"}
ALIASES = {
    "help": "help", "помощь": "help", "ёрдам": "help", "команды": "help",
    "status": "status", "статус": "status",
    "list": "list", "список": "list", "рӯйхат": "list",
    "auto": "auto", "авто": "auto", "автопилот": "auto",
    "greet": "greet", "привет": "greet", "салом": "greet",
    "greetings": "greetings", "рассылка": "greetings",
    "pause": "pause", "пауза": "pause",
    "resume": "resume", "старт": "resume", "продолжить": "resume",
}


def _onoff(word: str) -> bool | None:
    w = word.lower()
    return True if w in ON else False if w in OFF else None


async def handle_command(assistant: "Assistant", text: str) -> str | None:
    text = (text or "").strip()
    if not text.startswith("/"):
        return None
    parts = text[1:].split()
    if not parts:
        return HELP
    cmd = ALIASES.get(parts[0].lower())
    args = parts[1:]
    store = assistant.store

    if cmd in (None, "help"):
        return HELP

    if cmd == "status":
        pause_until = float(store.get_kv("pause_all_until", "0"))
        paused = f"пауза до {time.strftime('%H:%M', time.localtime(pause_until))}" if pause_until > time.time() else "нет паузы"
        return (
            f"Автопилот: {'вкл' if assistant.autopilot_on() else 'выкл'}\n"
            f"Приветствия по расписанию: {'вкл' if store.get_flag('greetings', assistant.settings.greetings_enabled) else 'выкл'}\n"
            f"{paused}\n"
            f"Модель: {assistant.generator_label}\n"
            f"Кандидатов в память: {len(store.list_fact_candidates())}"
        )

    if cmd == "list":
        rows = [
            f"• {r.address or r.name} ({r.id}) — {'общение' if r.mode is Mode.FULL_CHAT else 'только привет'}"
            for r in store.list_relatives()
            if r.relation != "family_group"
        ]
        return "\n".join(rows) or "Список пуст — заполните config/relatives.json"

    if cmd == "auto":
        if not args or _onoff(args[0]) is None:
            return "Пример: /авто вкл  или  /авто выкл Модар"
        on = _onoff(args[0])
        if len(args) == 1:
            store.set_flag("autopilot", on)
            return f"Автопилот {'включён' if on else 'выключен'} для всех."
        rel = store.find_relative_by_name(" ".join(args[1:]))
        if rel is None:
            return "Не нашёл такого человека. /список — все имена."
        store.set_relative_mode(rel.id, Mode.FULL_CHAT if on else Mode.GREETING_ONLY)
        return f"{rel.address or rel.name}: {'полноценное общение' if on else 'только приветствия'}."

    if cmd == "greet":
        target = " ".join(args).strip()
        if not target:
            return "Пример: /привет Модар  или  /привет всем"
        if target.lower() in ("всем", "all", "ҳама"):
            assistant.store.set_kv("greet_all_requested", str(time.time()))
            return "Хорошо, разошлю приветствия всем постепенно, с паузами."
        rel = store.find_relative_by_name(target)
        if rel is None:
            return "Не нашёл такого человека. /список — все имена."
        res = await assistant.greet(rel.id)
        return f"{rel.address or rel.name}: {res.reply}" if res.reply else f"Не получилось: {res.status} {res.detail or ''}"

    if cmd == "greetings":
        on = _onoff(args[0]) if args else None
        if on is None:
            return "Пример: /рассылка вкл"
        store.set_flag("greetings", on)
        return f"Приветствия по расписанию {'включены' if on else 'выключены'}."

    if cmd == "pause":
        try:
            hours = float(args[0].replace(",", ".")) if args else 1.0
        except ValueError:
            return "Пример: /пауза 3"
        store.set_kv("pause_all_until", str(time.time() + hours * 3600))
        return f"Молчу {hours:g} ч. /старт — снять паузу."

    if cmd == "resume":
        store.set_kv("pause_all_until", "0")
        return "Пауза снята."
    return HELP
