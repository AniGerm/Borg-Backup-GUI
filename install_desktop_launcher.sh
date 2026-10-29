#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ICON_SRC="$SCRIPT_DIR/assets/borg-backup-gui.svg"
MARKER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/borg-backup-gui"
MARKER_FILE="$MARKER_DIR/installed.marker"

APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"
AUTOSTART_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"

mkdir -p "$APP_DIR" "$ICON_DIR" "$AUTOSTART_DIR" "$MARKER_DIR"
install -m 0644 "$ICON_SRC" "$ICON_DIR/borg-backup-gui.svg"

# Rewrite Exec/Icon to this checkout (portable — no hardcoded usernames).
cat >"$APP_DIR/borg-backup-gui.desktop" <<EOF
[Desktop Entry]
Version=2.0
Type=Application
Name=Borg Backup GUI
Comment=System backups with Borg — verified, multi-target, auditable
Exec=$SCRIPT_DIR/start_gui.sh
Icon=$ICON_DIR/borg-backup-gui.svg
Terminal=false
Categories=Utility;Archiving;
Keywords=backup;borg;restore;hetzner;
StartupNotify=true
StartupWMClass=BorgBackupGUI
EOF
chmod 0644 "$APP_DIR/borg-backup-gui.desktop"
install -m 0644 "$APP_DIR/borg-backup-gui.desktop" "$AUTOSTART_DIR/borg-backup-gui.desktop"

date -u +%FT%TZ >"$MARKER_FILE"

rm -f "$APP_DIR/hetzner-borg-gui.desktop" "$AUTOSTART_DIR/hetzner-borg-gui.desktop"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APP_DIR" >/dev/null 2>&1 || true
fi

echo "[OK] Launcher: $APP_DIR/borg-backup-gui.desktop"
echo "[OK] Icon: $ICON_DIR/borg-backup-gui.svg"
echo "[OK] Autostart: $AUTOSTART_DIR/borg-backup-gui.desktop"
