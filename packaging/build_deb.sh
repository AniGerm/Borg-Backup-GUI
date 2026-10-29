#!/usr/bin/env bash
# Build an amd64 .deb for Borg Backup GUI (CI / GitHub Actions release.yml).
# Production releases: tag vX.Y.Z and push — do not publish local .deb builds.
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
  "$DEB_ROOT/usr/lib/borg-backup-gui/vendor" \
  "$DEB_ROOT/usr/share/applications" \
  "$DEB_ROOT/etc/xdg/autostart" \
  "$DEB_ROOT/usr/share/icons/hicolor/scalable/apps" \
  "$DEB_ROOT/usr/share/doc/borg-backup-gui" \
  "$DEB_ROOT/usr/share/polkit-1/actions" \
  "$DEB_ROOT/lib/systemd/system"

install -m 0644 \
  borg_backup_gui.py borg_model.py borg_updates.py \
  borg_engine.py borg_daemon.py borg_ipc.py borg_sysconfig.py \
  "$DEB_ROOT/usr/lib/borg-backup-gui/"
install -m 0755 start_gui.sh restore.sh "$DEB_ROOT/usr/lib/borg-backup-gui/"
install -m 0755 packaging/apply-update.sh "$DEB_ROOT/usr/lib/borg-backup-gui/apply-update.sh"
printf '%s\n' "$VERSION" >"$DEB_ROOT/usr/lib/borg-backup-gui/VERSION"
mkdir -p "$DEB_ROOT/usr/lib/borg-backup-gui/assets"
install -m 0644 assets/borg-backup-gui.svg "$DEB_ROOT/usr/lib/borg-backup-gui/assets/"
install -m 0644 LICENSE README.md SECURITY.md docs/RESTORE.md "$DEB_ROOT/usr/share/doc/borg-backup-gui/"

# Vendor fpdf2 (+ deps already via apt: fonttools, defusedxml) for offline PDF protocols.
python3 -m pip install --quiet --no-deps --target "$DEB_ROOT/usr/lib/borg-backup-gui/vendor" "fpdf2>=2.7.0"
find "$DEB_ROOT/usr/lib/borg-backup-gui/vendor" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
find "$DEB_ROOT/usr/lib/borg-backup-gui/vendor" -type f -name '*.pyc' -delete 2>/dev/null || true

install -m 0644 packaging/borg-backup-guid.service "$DEB_ROOT/lib/systemd/system/borg-backup-guid.service"
install -m 0644 packaging/org.borgbackupgui.policy "$DEB_ROOT/usr/share/polkit-1/actions/org.borgbackupgui.policy"

cat >"$DEB_ROOT/usr/bin/borg-backup-gui" <<'WRAP'
#!/usr/bin/env bash
set -euo pipefail
APP_ROOT=/usr/lib/borg-backup-gui
export BORG_BACKUP_GUI_SYSTEM=1
exec "$APP_ROOT/start_gui.sh" "$@"
WRAP
chmod 0755 "$DEB_ROOT/usr/bin/borg-backup-gui"

# Headless entry for the systemd service (same tree as the GUI modules).
cat >"$DEB_ROOT/usr/bin/borg-backup-guid" <<'WRAP'
#!/usr/bin/env bash
set -euo pipefail
exec /usr/bin/python3 -s -E /usr/lib/borg-backup-gui/borg_daemon.py "$@"
WRAP
chmod 0755 "$DEB_ROOT/usr/bin/borg-backup-guid"

DESKTOP_FILE="$DEB_ROOT/usr/share/applications/borg-backup-gui.desktop"
cat >"$DESKTOP_FILE" <<EOF
[Desktop Entry]
Version=3.0
Type=Application
Name=Borg Backup GUI
Comment=System backups with Borg — verified, multi-target, auditable
Exec=borg-backup-gui
Icon=borg-backup-gui
Terminal=false
Categories=Utility;Archiving;
Keywords=backup;borg;restore;
StartupNotify=true
StartupWMClass=BorgBackupGUI
EOF

# Autostart: tray mode (window stays hidden when AppIndicator is available)
cat >"$DEB_ROOT/etc/xdg/autostart/borg-backup-gui.desktop" <<EOF
[Desktop Entry]
Version=3.0
Type=Application
Name=Borg Backup GUI
Comment=System backups with Borg (Hintergrund)
Exec=borg-backup-gui --tray
Icon=borg-backup-gui
Terminal=false
Categories=Utility;Archiving;
X-GNOME-Autostart-enabled=true
EOF

install -m 0644 assets/borg-backup-gui.svg \
  "$DEB_ROOT/usr/share/icons/hicolor/scalable/apps/borg-backup-gui.svg"

SIZE="$(du -sk "$DEB_ROOT" | cut -f1)"
cat >"$DEB_ROOT/DEBIAN/control" <<EOF
Package: ${PKG_NAME}
Version: ${VERSION}
Section: utils
Priority: optional
Architecture: ${ARCH}
Depends: python3 (>= 3.10), python3-tk, python3-gi, gir1.2-ayatanaappindicator3-0.1, python3-pil, python3-fonttools, python3-defusedxml, borgbackup, policykit-1 | polkitd, openssh-client
Maintainer: Borg Backup GUI Maintainers <noreply@users.noreply.github.com>
Installed-Size: ${SIZE}
Homepage: https://github.com/AniGerm/Borg-Backup-GUI
Description: Desktop GUI and root service for full-system BorgBackup
 Borg Backup GUI provides scheduled, encrypted system backups with Canary
 restore verification, multi-destination jobs, and PDF protocol reports.
 Backups run in the borg-backup-guid systemd service (root, polkit).
EOF

cat >"$DEB_ROOT/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
# Remove legacy NOPASSWD sudoers from 2.x (security: free SETENV on borg).
rm -f /etc/sudoers.d/99-borg-backup-gui /etc/sudoers.d/99-hetzner-borg-gui || true
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database -q /usr/share/applications || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -q /usr/share/icons/hicolor || true
fi
if command -v deb-systemd-helper >/dev/null 2>&1; then
  deb-systemd-helper enable borg-backup-guid.service >/dev/null || true
fi
if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload || true
  systemctl start borg-backup-guid.service || true
fi
echo "Borg Backup GUI installed. Service: borg-backup-guid. Backups need no sudoers rule."
EOF
chmod 0755 "$DEB_ROOT/DEBIAN/postinst"

cat >"$DEB_ROOT/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "remove" ] && command -v systemctl >/dev/null 2>&1; then
  systemctl stop borg-backup-guid.service || true
fi
EOF
chmod 0755 "$DEB_ROOT/DEBIAN/prerm"

cat >"$DEB_ROOT/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload || true
fi
if [ "$1" = "purge" ]; then
  rm -rf /etc/borg-backup-gui /var/lib/borg-backup-gui || true
fi
EOF
chmod 0755 "$DEB_ROOT/DEBIAN/postrm"

OUT_DIR="${ROOT}/dist"
mkdir -p "$OUT_DIR"
OUT_DEB="${OUT_DIR}/${PKG_NAME}_${VERSION}_${ARCH}.deb"
dpkg-deb --build --root-owner-group "$DEB_ROOT" "$OUT_DEB"
echo "Built $OUT_DEB"
ls -lh "$OUT_DEB"
