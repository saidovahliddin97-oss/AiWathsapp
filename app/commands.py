"""Owner commands sent over WhatsApp (to your own "message yourself" chat in
personal-number mode, or from your personal phone to the bot's number).

Examples:  /статус   /авто выкл   /авто вкл Модар   /привет всем   /пауза 3
"""
from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from app.memory import ConfigError, normalize_phone, remove_relative_from_config, save_relative_to_config
from app.models import AgeGroup, Mode, Relative

if TYPE_CHECKING:
    from app.webhook import Assistant

HELP = """Команды:
/статус — состояние бота
/список — контакты и режимы
/добавить <номер> <кто> <обращение> — пример: /добавить 992901234567 мама Модарҷон
/удалить <имя> — убрать контакт
/авто вкл <имя> — бот переписывается с человеком
/авто выкл <имя> — только приветствия, на сообщения не отвечает
/авто вкл | выкл — то же самое сразу для всех
/группы — группы, которые бот видел
/группа вкл | выкл <название> — разрешить боту отвечать в группе
/привет <имя> | всем — отправить приветствие сейчас
/рассылка вкл | выкл — приветствия по расписанию
/пауза <часы> — бот молчит N часов;  /старт — снять все паузы
/почему — что бот сделал с последними сообщениями и почему"""

# who -> (relation, age group). Words in Russian and Tajik.
RELATIONS = {
    "мама": ("mother", "elder"), "модар": ("mother", "elder"), "ана": ("mother", "elder"),
    "папа": ("father", "elder"), "падар": ("father", "elder"), "дада": ("father", "elder"),
    "бабушка": ("grandmother", "elder"), "биби": ("grandmother", "elder"), "бибӣ": ("grandmother", "elder"),
    "дедушка": ("grandfather", "elder"), "бобо": ("grandfather", "elder"),
    "тётя": ("aunt", "elder"), "тетя": ("aunt", "elder"), "хола": ("aunt", "elder"), "амма": ("aunt", "elder"),
    "дядя": ("uncle", "elder"), "тағо": ("uncle", "elder"), "таго": ("uncle", "elder"), "амак": ("uncle", "elder"),
    "брат": ("brother", "peer"), "бародар": ("brother", "peer"), "ака": ("brother", "elder"),
    "сестра": ("sister", "peer"), "хоҳар": ("sister", "peer"), "апа": ("sister", "elder"),
    "младший": ("brother", "younger"), "младшая": ("sister", "younger"), "додар": ("brother", "younger"),
    "друг": ("friend", "peer"), "подруга": ("friend", "peer"), "дӯст": ("friend", "peer"),
    "тест": ("friend", "peer"),
}

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
    "add": "add", "добавить": "add", "илова": "add",
    "remove": "remove", "удалить": "remove",
    "groups": "groups", "группы": "groups",
    "why": "why", "почему": "why", "лог": "why", "журнал": "why",
    "group": "group", "группа": "group",
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
        rels = [r for r in store.list_relatives() if r.relation != "family_group"]
        full = sum(r.mode is Mode.FULL_CHAT for r in rels)
        groups_on = sum(v == "1" for v in store.kv_prefix("group_on:").values())
        return (
            f"Переписывается: {full} из {len(rels)} контактов (остальным только приветствия)\n"
            f"Группы, где бот может отвечать: {groups_on}\n"
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
            mode = Mode.FULL_CHAT if on else Mode.GREETING_ONLY
            store.set_all_modes(mode)
            store.set_kv("default_mode", mode.value)
            return ("Бот переписывается со всеми контактами." if on
                    else "Бот больше никому не отвечает (только приветствия). Включить одного: /авто вкл <имя>")
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
        for rel_id in store.kv_prefix("manual:"):
            store.set_kv(f"manual:{rel_id}", "0")
        return "Паузы сняты — бот снова отвечает во всех включённых чатах."

    if cmd == "why":
        return _why(assistant)

    if cmd == "add":
        return _add(assistant, args)

    if cmd == "remove":
        rel = store.find_relative_by_name(" ".join(args)) if args else None
        if rel is None or rel.relation == "family_group":
            return "Не нашёл такого человека. /список — все имена."
        store.delete_relative(rel.id)
        try:
            remove_relative_from_config(assistant.settings.relatives_file, rel.id)
        except ConfigError as e:
            return f"Удалил из бота, но файл списка с ошибкой: {e}"
        return f"Удалил {rel.address or rel.name}. Бот больше не будет ему отвечать."

    if cmd == "groups":
        seen = store.kv_prefix("group_seen:")
        on = store.kv_prefix("group_on:")
        if not seen:
            return "Бот пока не видел сообщений ни в одной группе. Когда в группе кто-то напишет — она появится здесь."
        return "\n".join(f"• {name} — {'бот отвечает' if on.get(jid) == '1' else 'выключено'}" for jid, name in seen.items())

    if cmd == "group":
        on = _onoff(args[0]) if args else None
        query = " ".join(args[1:]).strip().lower()
        if on is None or not query:
            return "Пример: /группа вкл Оила   (список: /группы)"
        matches = [(jid, name) for jid, name in store.kv_prefix("group_seen:").items() if query in name.lower()]
        if len(matches) != 1:
            return "Не нашёл такую группу. /группы — список." if not matches else "Подходит несколько групп, уточните название."
        jid, name = matches[0]
        store.set_flag(f"group_on:{jid}", on)
        return (f"Группа «{name}»: бот будет отвечать, только когда обращаются к вам по имени или через @."
                if on else f"Группа «{name}»: бот молчит.")
    return HELP


def _add(assistant: "Assistant", args: list[str]) -> str:
    usage = "Пример: /добавить 992901234567 мама Модарҷон\n(кто: мама, папа, брат, сестра, ака, апа, хола, тағо, друг, младший…)"
    # the number may be typed with spaces: "+992 90 123-45-67"
    i = 0
    while i < len(args) and re.fullmatch(r"[+\d()\-]+", args[i]):
        i += 1
    phone = normalize_phone("".join(args[:i]))
    rest = args[i:]
    if len(phone) < 8 or not rest:
        return usage
    who = rest[0].lower()
    relation, age = RELATIONS.get(who, ("relative", "peer"))
    address = " ".join(rest[1:]).strip() or rest[0]
    existing = assistant.store.find_relative_by_phone(phone)
    rel_id = existing.id if existing else re.sub(r"\W+", "_", f"{relation}_{phone[-4:]}").lower()
    rel = Relative(
        id=rel_id, phone=phone, name=address, relation=relation, age_group=AgeGroup(age),
        address=address, mode=existing.mode if existing else assistant.default_mode(),
    )
    try:
        save_relative_to_config(assistant.settings.relatives_file, rel)
    except ConfigError as e:
        return f"Не смог записать в список: {e}"
    assistant.store.upsert_relative(rel)
    mode = "переписывается" if rel.mode is Mode.FULL_CHAT else "только приветствия (включить: /авто вкл " + address + ")"
    return f"{'Обновил' if existing else 'Добавил'}: {address} (+{phone}, {relation}, {age}). Режим: {mode}."


def _why(assistant: "Assistant") -> str:
    import json

    try:
        items = json.loads(assistant.store.get_kv("journal", "[]"))
    except ValueError:
        items = []
    if not items:
        return ("Бот ещё не получил ни одного сообщения от других людей.\n"
                "Если вам писали, а здесь пусто — сообщение не дошло до бота: проверьте, что окно бота на Mac открыто.")
    pause_min = assistant.settings.manual_pause_minutes
    lines = []
    for it in items[-5:]:
        when = time.strftime("%H:%M", time.localtime(it["t"]))
        st, who, d = it["status"], it["who"], it.get("detail", "")
        if st in ("sent", "dry_run"):
            why = f"ответил: «{it.get('reply', '')}»" + (" (тестовый режим DRY_RUN — в WhatsApp не ушло)" if st == "dry_run" else "")
        elif st == "ignored_unknown_sender":
            num = d.split()[0].lstrip("+") if d else "номер"
            why = f"его нет в списке ({d}). Добавить: /добавить {num} друг Имя"
        elif st == "skipped_greeting_only":
            why = f"режим «только приветствия». Включить переписку: /авто вкл {who}"
        elif st == "skipped_manual_pause":
            why = f"вы недавно писали этому человеку сами — бот молчит {pause_min} мин. Снять: /старт"
        elif st == "group_not_addressed":
            why = "в группе обращались не к вам"
        elif st == "ignored_empty":
            why = "пустое сообщение"
        elif st == "generation_failed":
            why = f"ошибка модели: {d}"
        elif st == "validation_failed":
            why = f"ответ модели не прошёл проверку ({d})"
        elif st == "send_failed_queued":
            why = f"не удалось отправить в WhatsApp, повторю позже ({d})"
        elif st == "owner_message_recorded":
            why = "это вы написали сами — бот запомнил и пока молчит в этом чате"
        else:
            why = f"{st} {d}"
        lines.append(f"{when} {who}: {why}")
    return "Последние сообщения:\n" + "\n".join(lines)
