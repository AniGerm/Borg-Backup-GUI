#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_FILE="$SCRIPT_DIR/hetzner-borg-gui.desktop"
ICON_FILE="$SCRIPT_DIR/assets/hetzner-borg-gui.svg"
MARKER_DIR="$HOME/.config/hetzner-borg-gui"
MARKER_FILE="$MARKER_DIR/installed.marker"

APP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
AUTOSTART_DIR="$HOME/.config/autostart"

mkdir -p "$APP_DIR" "$ICON_DIR" "$AUTOSTART_DIR"
install -m 0644 "$APP_FILE" "$APP_DIR/hetzner-borg-gui.desktop"
install -m 0644 "$ICON_FILE" "$ICON_DIR/hetzner-borg-gui.svg"
install -m 0644 "$APP_FILE" "$AUTOSTART_DIR/hetzner-borg-gui.desktop"
mkdir -p "$MARKER_DIR"
date -u +%FT%TZ > "$MARKER_FILE"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APP_DIR" >/dev/null 2>&1 || true
fi

echo "[OK] Launcher installiert: $APP_DIR/hetzner-borg-gui.desktop"
echo "[OK] Icon installiert: $ICON_DIR/hetzner-borg-gui.svg"
echo "[OK] Autostart installiert: $AUTOSTART_DIR/hetzner-borg-gui.desktop"
echo "[OK] Installationsmarker geschrieben: $MARKER_FILE"
echo "[HINWEIS] Im App-Menue nach 'Hetzner Borg GUI' suchen und an Dock anheften."
