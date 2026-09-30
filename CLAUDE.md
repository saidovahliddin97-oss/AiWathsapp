# Family Assistant — notes for Claude Code

WhatsApp bot that writes to the owner's relatives in Tajik (Cyrillic). The owner is
not a developer: explain in Russian, short, and do the work yourself.

## Layout on the owner's Mac
- Installed at `~/FamilyBot` by `install.sh`; Desktop shortcut «Семейный бот.command» -> `start.command`.
- `start.command` auto-updates code from GitHub (branch `claude/final-product-ei38pk`) on every
  launch, keeping `.env`, `config/relatives.json`, `bridge/auth/` (WhatsApp login) and `database/`.
  Changes made locally to tracked files are overwritten on the next launch — commit/push them
  to that branch, or run with `AUTO_UPDATE=false`.
- Two processes: Python FastAPI backend (`app/`, port 8000, log `database/server.log`) and the
  Node WhatsApp bridge (`bridge/index.js`, Baileys, port 3001, prints the QR / status).

## Run & check
- Start: `bash start.command` (validates `.env` via `python -m app.config` and the relatives list
  via `python -m app.memory config/relatives.json`, then starts both processes).
- Health: `curl -s http://127.0.0.1:8000/health`; demo UI (no WhatsApp sends): http://127.0.0.1:8000/demo
- Tests: `.venv/bin/python -m pytest -q`
- WhatsApp commands (owner writes to the chat with themself): `/статус`, `/список`, `/добавить <номер> <кто> <обращение>`,
  `/удалить <имя>`, `/авто вкл|выкл [имя]`, `/группы`, `/группа вкл|выкл <название>`, `/привет имя|всем`, `/пауза N`, `/старт`, `/почему` (explains the last decisions).
- Groups are ignored unless enabled with `/группа вкл`. Modes set by commands persist across restarts.

## Common problems
- `.env` booleans must be `true`/`false`; phone numbers without `+` go to `OWNER_PHONE`.
- TextEdit smart quotes in `relatives.json` are auto-repaired; other JSON errors are reported with line numbers.
- "backend is not reachable" in the bridge window = the Python server crashed: read `database/server.log`.
- Logged out of WhatsApp: delete `bridge/auth/` and start again to get a new QR.

## Never
- Never print, commit or send secrets from `.env` (GEMINI_API_KEY, ANTHROPIC_API_KEY, BRIDGE_TOKEN).
- Never send test messages to real relatives without the owner's explicit OK; use `/demo` or `DRY_RUN=true`.
