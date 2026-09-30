#!/bin/bash
# One-command install for macOS. In Terminal:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/saidovahliddin97-oss/AiWathsapp/claude/final-product-ei38pk/install.sh)"
set -euo pipefail
DEST="$HOME/FamilyBot"
RAW="https://raw.githubusercontent.com/saidovahliddin97-oss/AiWathsapp/claude/final-product-ei38pk"
echo "▶ Скачиваю семейного бота в $DEST"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
curl -fsSL "$RAW/update.sh" -o "$TMP/update.sh"
bash "$TMP/update.sh" "$DEST"

# Перенос настроек из старой копии (скачанной ZIP-архивом), если она есть
if [ ! -f "$DEST/.env" ]; then
  OLD=""
  while IFS= read -r f; do
    d=$(dirname "$f")
    [ "$d" = "$DEST" ] && continue
    if [ -f "$d/.env" ]; then OLD="$d"; [ -d "$d/bridge/auth" ] && break; fi
  done < <(find "$HOME/Desktop" "$HOME/Downloads" "$HOME/Documents" -maxdepth 4 -name start.command 2>/dev/null)
  if [ -n "$OLD" ]; then
    echo "▶ Переношу ваши настройки из $OLD"
    cp "$OLD/.env" "$DEST/.env"
    [ -f "$OLD/config/relatives.json" ] && cp "$OLD/config/relatives.json" "$DEST/config/relatives.json"
    [ -d "$OLD/bridge/auth" ] && mkdir -p "$DEST/bridge" && cp -R "$OLD/bridge/auth" "$DEST/bridge/auth"
    [ -d "$OLD/database" ] && cp -R "$OLD/database" "$DEST/" 2>/dev/null || true
    # старая копия больше не нужна: чтобы два бота не работали одновременно
    mv "$OLD/start.command" "$OLD/start.command.old" 2>/dev/null || true
  fi
fi

ln -sf "$DEST/start.command" "$HOME/Desktop/Семейный бот.command"
echo "✓ Готово. На Рабочем столе появился ярлык «Семейный бот» — им запускайте бота в следующий раз."
exec bash "$DEST/start.command"
