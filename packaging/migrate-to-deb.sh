#!/usr/bin/env bash
# One-shot: switch from git-checkout + start_gui.sh to the system .deb install.
# Keeps ~/.config/borg-backup-gui/ (UI prefs, protocols). Secrets migrate into the service.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "==> Borg Backup GUI: Migrate checkout → .deb"

# 1) Stop checkout GUI only (this repo path)
if pgrep -f "${ROOT}/borg_backup_gui.py" >/dev/null 2>&1; then
  echo "[*] Beende laufende Checkout-GUI…"
  pkill -f "${ROOT}/borg_backup_gui.py" || true
  sleep 1
fi
# Also stop a system-installed instance so reinstall is clean
if pgrep -f '/usr/lib/borg-backup-gui/borg_backup_gui.py' >/dev/null 2>&1; then
  echo "[*] Beende systemweite GUI…"
  pkill -f '/usr/lib/borg-backup-gui/borg_backup_gui.py' || true
  sleep 1
fi

# 2) Build .deb if missing
shopt -s nullglob
DEB_CANDIDATES=(dist/borg-backup-gui_*_amd64.deb)
if ((${#DEB_CANDIDATES[@]} == 0)); then
  echo "[*] Kein Deb in dist/ — baue Paket…"
  chmod +x packaging/build_deb.sh
  ./packaging/build_deb.sh
  DEB_CANDIDATES=(dist/borg-backup-gui_*_amd64.deb)
fi
# Newest by mtime
DEB="$(ls -1t dist/borg-backup-gui_*_amd64.deb | head -n1)"
if [[ -z "$DEB" || ! -f "$DEB" ]]; then
  echo "[FEHLER] Kein .deb gefunden." >&2
  exit 1
fi
echo "[*] Verwende Paket: $DEB"

# 3) Install
echo "[*] Installiere mit apt (sudo)…"
sudo apt-get install -y "./$DEB"

# 4) Remove shadow launchers that point at the git checkout
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
AUTOSTART_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
echo "[*] Entferne alte User-Launcher (Checkout-Pfade)…"
rm -f \
  "$APP_DIR/borg-backup-gui.desktop" \
  "$APP_DIR/hetzner-borg-gui.desktop" \
  "$AUTOSTART_DIR/borg-backup-gui.desktop" \
  "$AUTOSTART_DIR/hetzner-borg-gui.desktop"

# 5) Refresh caches (best-effort)
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APP_DIR" >/dev/null 2>&1 || true
  sudo update-desktop-database /usr/share/applications >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -q "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" 2>/dev/null || true
  sudo gtk-update-icon-cache -q /usr/share/icons/hicolor 2>/dev/null || true
fi

# 6) Verify
echo "[*] Prüfung:"
command -v borg-backup-gui
dpkg -l borg-backup-gui | tail -1 || true
test -f /usr/share/applications/borg-backup-gui.desktop && echo "  OK app menu: /usr/share/applications/borg-backup-gui.desktop"
test -f /etc/xdg/autostart/borg-backup-gui.desktop && echo "  OK autostart: /etc/xdg/autostart/borg-backup-gui.desktop"
test -f /usr/share/icons/hicolor/scalable/apps/borg-backup-gui.svg && echo "  OK icon: hicolor scalable"
test -d "$HOME/.config/borg-backup-gui" && echo "  OK config: ~/.config/borg-backup-gui (unverändert)"

# Marker so start_gui does not re-offer checkout launcher install
MARKER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/borg-backup-gui"
mkdir -p "$MARKER_DIR"
date -u +%FT%TZ >"$MARKER_DIR/installed.marker"

# 7) Start if DISPLAY available
if [[ -n "${DISPLAY:-}" || -n "${WAYLAND_DISPLAY:-}" ]]; then
  echo "[*] Starte borg-backup-gui…"
  nohup borg-backup-gui >/tmp/borg-backup-gui-migrate.log 2>&1 &
  sleep 1
  if pgrep -f '/usr/lib/borg-backup-gui/borg_backup_gui.py' >/dev/null 2>&1; then
    echo "[OK] GUI läuft aus dem Deb-Paket."
  else
    echo "[HINWEIS] Start angestoßen — Log: /tmp/borg-backup-gui-migrate.log"
  fi
else
  echo "[HINWEIS] Kein DISPLAY — starte später: borg-backup-gui"
fi

echo ""
echo "Fertig. Alltag: App-Menü oder 'borg-backup-gui'. Autostart: Login."
echo "Dienst: systemctl status borg-backup-guid"
