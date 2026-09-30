#!/bin/bash
# Family Assistant — запуск на Mac. Двойной клик (или: bash start.command в Терминале).
SELF="$(readlink "$0" 2>/dev/null || echo "$0")"
cd "$(dirname "$SELF")" || exit 1

# Автообновление: при каждом запуске подтягиваем последнюю версию (настройки не трогаются)
if [ -z "${FB_UPDATED:-}" ] && [ "${AUTO_UPDATE:-true}" != "false" ] && [ -f update.sh ]; then
  if bash update.sh "$PWD" >/dev/null 2>&1; then
    FB_UPDATED=1 exec bash "$PWD/start.command"
  else
    echo "(не удалось проверить обновления — запускаю текущую версию)"
  fi
fi
say_step() { printf "\n\033[1;32m▶ %s\033[0m\n" "$1"; }
fail() { printf "\n\033[1;31m✖ %s\033[0m\n" "$1"; read -r -p "Нажмите Enter, чтобы закрыть"; exit 1; }

say_step "Проверяю Python и Node.js"
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  open "https://www.python.org/downloads/macos/"
  fail "Нужен Python 3.10+. Установите его с открывшейся страницы (кнопка Download) и запустите этот файл снова."
fi
if ! command -v node >/dev/null 2>&1 || ! node -e 'const [a,b]=process.versions.node.split(".").map(Number); process.exit(a>20||(a===20&&b>=12)?0:1)'; then
  open "https://nodejs.org/"
  fail "Нужен Node.js (версия LTS). Установите его с открывшейся страницы и запустите этот файл снова."
fi

if [ ! -f .env ]; then cp .env.example .env; fi
if ! grep -qE '^BRIDGE_TOKEN=.+' .env; then
  TOKEN=$(openssl rand -hex 24)
  if grep -q '^BRIDGE_TOKEN=' .env; then sed -i '' "s/^BRIDGE_TOKEN=.*/BRIDGE_TOKEN=$TOKEN/" .env; else echo "BRIDGE_TOKEN=$TOKEN" >> .env; fi
fi

if ! grep -qE '^(GEMINI_API_KEY|ANTHROPIC_API_KEY)=.+' .env; then
  say_step "Нужен ключ модели"
  echo "1) Откроется страница Google AI Studio — нажмите «Create API key» и скопируйте ключ."
  echo "2) Откроется файл настроек .env — вставьте ключ после GEMINI_API_KEY= и сохраните (Cmd+S)."
  echo "   Там же заполните OWNER_PHONE (ваш личный номер без +) и OWNER_NAMES (ваше имя)."
  open "https://aistudio.google.com/apikey"
  open -e .env
  read -r -p "Когда сохраните файл — нажмите Enter..."
fi

if [ ! -f config/relatives.json ]; then
  cp config/relatives.example.json config/relatives.json
  say_step "Заполните список родственников"
  echo "Откроется config/relatives.json — впишите реальные номера (без +), имена и обращения, сохраните (Cmd+S)."
  open -e config/relatives.json
  read -r -p "Когда сохраните файл — нажмите Enter..."
fi

say_step "Устанавливаю зависимости (первый раз — пару минут)"
if [ ! -x .venv/bin/python ]; then "$PY" -m venv .venv || fail "Не удалось создать .venv"; fi
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt || fail "Не удалось установить Python-пакеты"
(cd bridge && npm install --no-audit --no-fund --silent) || fail "Не удалось установить пакеты моста (npm)"

mkdir -p database
say_step "Проверяю список родственников"
.venv/bin/python -m app.memory config/relatives.json || {
  open -e config/relatives.json
  fail "Исправьте config/relatives.json (ошибка указана выше), сохраните и запустите start.command снова."
}

# остатки прошлого запуска могут занимать порты
for port in 8000 3001; do
  pids=$(lsof -ti tcp:$port 2>/dev/null)
  [ -n "$pids" ] && kill $pids 2>/dev/null && sleep 1
done

say_step "Запускаю бота"
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 > database/server.log 2>&1 &
BACKEND=$!
trap 'kill $BACKEND 2>/dev/null' EXIT INT TERM
HEALTH=""
for _ in $(seq 1 30); do
  HEALTH=$(curl -s -m 2 http://127.0.0.1:8000/health) && [ -n "$HEALTH" ] && break
  kill -0 $BACKEND 2>/dev/null || break
  sleep 1
done
if [ -z "$HEALTH" ]; then
  echo "----- database/server.log -----"; tail -30 database/server.log
  fail "Сервер бота не запустился (причина выше). Пришлите этот текст — помогу."
fi
echo "Сервер: $HEALTH"
echo "Демо-страница: http://127.0.0.1:8000/demo   ·   журнал: database/server.log"
echo "Чтобы Mac не засыпал, пока окно открыто, включён caffeinate. Остановить бота — закройте окно или Ctrl+C."

caffeinate -i -w $$ &
cd bridge && node index.js
