#!/bin/bash
# Downloads the latest version of the bot into the given folder, keeping your
# settings (.env), relatives list, WhatsApp login (bridge/auth) and history.
# Usage: bash update.sh <folder>
set -euo pipefail
DEST="${1:-$HOME/FamilyBot}"
BRANCH="claude/final-product-ei38pk"
URL="https://github.com/saidovahliddin97-oss/AiWathsapp/archive/refs/heads/${BRANCH}.zip"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

curl -fsSL --max-time 60 "$URL" -o "$TMP/bot.zip"
unzip -q "$TMP/bot.zip" -d "$TMP"
SRC=$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -1)
[ -f "$SRC/start.command" ] || { echo "update: unexpected archive layout"; exit 1; }

mkdir -p "$DEST"
# The archive never contains your data (.env, relatives.json, bridge/auth, database/*.db
# are git-ignored), so a plain copy keeps it. Scripts are removed first so a running
# start.command keeps reading its old copy instead of a file changing under it.
rm -f "$DEST/start.command" "$DEST/update.sh" "$DEST/install.sh"
cp -R "$SRC/." "$DEST/"
chmod +x "$DEST/start.command" "$DEST/update.sh" "$DEST/install.sh" 2>/dev/null || true
