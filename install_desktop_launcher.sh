#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_FILE="$SCRIPT_DIR/borg-backup-gui.desktop"
ICON_FILE="$SCRIPT_DIR/assets/borg-backup-gui.svg"
MARKER_DIR="$HOME/.config/borg-backup-gui"
MARKER_FILE="$MARKER_DIR/installed.marker"

APP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
AUTOSTART_DIR="$HOME/.config/autostart"

mkdir -p "$APP_DIR" "$ICON_DIR" "$AUTOSTART_DIR"
install -m 0644 "$APP_FILE" "$APP_DIR/borg-backup-gui.desktop"
install -m 0644 "$ICON_FILE" "$ICON_DIR/borg-backup-gui.svg"
install -m 0644 "$APP_FILE" "$AUTOSTART_DIR/borg-backup-gui.desktop"
mkdir -p "$MARKER_DIR"
date -u +%FT%TZ > "$MARKER_FILE"

# Alte Launcher entfernen
rm -f "$APP_DIR/hetzner-borg-gui.desktop"
rm -f "$AUTOSTART_DIR/hetzner-borg-gui.desktop"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APP_DIR" >/dev/null 2>&1 || true
fi

echo "[OK] Launcher installiert: $APP_DIR/borg-backup-gui.desktop"
echo "[OK] Icon installiert: $ICON_DIR/borg-backup-gui.svg"
echo "[OK] Autostart installiert: $AUTOSTART_DIR/borg-backup-gui.desktop"
echo "[OK] Installationsmarker geschrieben: $MARKER_FILE"
echo "[HINWEIS] Im App-Menue nach 'Borg Backup GUI' suchen und an Dock anheften."
