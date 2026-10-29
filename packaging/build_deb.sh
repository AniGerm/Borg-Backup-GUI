#!/usr/bin/env bash
# Build an amd64 .deb for Borg Backup GUI (run from repo root or packaging/).
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION="$(python3 -c "import re; print(re.search(r'__version__\\s*=\\s*[\"\\']([^\"\\']+)', open('borg_backup_gui.py').read()).group(1))")"
ARCH="amd64"
PKG_NAME="borg-backup-gui"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

DEB_ROOT="$STAGE/${PKG_NAME}_${VERSION}_${ARCH}"
mkdir -p \
  "$DEB_ROOT/DEBIAN" \
  "$DEB_ROOT/usr/bin" \
  "$DEB_ROOT/usr/lib/borg-backup-gui" \
  "$DEB_ROOT/usr/share/applications" \
  "$DEB_ROOT/etc/xdg/autostart" \
  "$DEB_ROOT/usr/share/icons/hicolor/scalable/apps" \
  "$DEB_ROOT/usr/share/doc/borg-backup-gui"

install -m 0644 borg_backup_gui.py borg_model.py borg_updates.py "$DEB_ROOT/usr/lib/borg-backup-gui/"
install -m 0755 start_gui.sh install_nopasswd_rule.sh install_desktop_launcher.sh restore.sh \
  "$DEB_ROOT/usr/lib/borg-backup-gui/"
install -m 0755 packaging/apply-update.sh "$DEB_ROOT/usr/lib/borg-backup-gui/apply-update.sh"
install -m 0644 requirements.txt "$DEB_ROOT/usr/lib/borg-backup-gui/"
printf '%s\n' "$VERSION" >"$DEB_ROOT/usr/lib/borg-backup-gui/VERSION"
mkdir -p "$DEB_ROOT/usr/lib/borg-backup-gui/assets"
install -m 0644 assets/borg-backup-gui.svg "$DEB_ROOT/usr/lib/borg-backup-gui/assets/"
install -m 0644 LICENSE README.md SECURITY.md "$DEB_ROOT/usr/share/doc/borg-backup-gui/"

# Wrapper: prefer system python3; create user venv for pip deps on first run via start_gui.sh
cat >"$DEB_ROOT/usr/bin/borg-backup-gui" <<'WRAP'
#!/usr/bin/env bash
set -euo pipefail
APP_ROOT=/usr/lib/borg-backup-gui
export BORG_BACKUP_GUI_SYSTEM=1
exec "$APP_ROOT/start_gui.sh" "$@"
WRAP
chmod 0755 "$DEB_ROOT/usr/bin/borg-backup-gui"

DESKTOP_FILE="$DEB_ROOT/usr/share/applications/borg-backup-gui.desktop"
cat >"$DESKTOP_FILE" <<EOF
[Desktop Entry]
Version=2.0
Type=Application
Name=Borg Backup GUI
Comment=System backups with Borg — verified, multi-target, auditable
Exec=borg-backup-gui
Icon=borg-backup-gui
Terminal=false
Categories=Utility;Archiving;
Keywords=backup;borg;restore;hetzner;
StartupNotify=true
StartupWMClass=BorgBackupGUI
X-GNOME-Autostart-enabled=true
EOF
# System-wide autostart (login) — same entry as the app menu
install -m 0644 "$DESKTOP_FILE" "$DEB_ROOT/etc/xdg/autostart/borg-backup-gui.desktop"

install -m 0644 assets/borg-backup-gui.svg \
  "$DEB_ROOT/usr/share/icons/hicolor/scalable/apps/borg-backup-gui.svg"

SIZE="$(du -sk "$DEB_ROOT" | cut -f1)"
cat >"$DEB_ROOT/DEBIAN/control" <<EOF
Package: ${PKG_NAME}
Version: ${VERSION}
Section: utils
Priority: optional
Architecture: ${ARCH}
Depends: python3 (>= 3.10), python3-tk, python3-venv, borgbackup
Recommends: python3-pil
Maintainer: Borg Backup GUI Maintainers <noreply@users.noreply.github.com>
Installed-Size: ${SIZE}
Homepage: https://github.com/AniGerm/Borg-Backup-GUI
Description: Desktop GUI for full-system BorgBackup
 Borg Backup GUI provides scheduled, encrypted system backups with Canary
 restore verification, multi-destination jobs, and PDF protocol reports.
EOF

cat >"$DEB_ROOT/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database -q /usr/share/applications || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -q /usr/share/icons/hicolor || true
fi
echo "Borg Backup GUI installed. Optional: run /usr/lib/borg-backup-gui/install_nopasswd_rule.sh for unattended system backups."
EOF
chmod 0755 "$DEB_ROOT/DEBIAN/postinst"

OUT_DIR="${ROOT}/dist"
mkdir -p "$OUT_DIR"
OUT_DEB="${OUT_DIR}/${PKG_NAME}_${VERSION}_${ARCH}.deb"
dpkg-deb --build --root-owner-group "$DEB_ROOT" "$OUT_DEB"
echo "Built $OUT_DEB"
ls -lh "$OUT_DEB"
