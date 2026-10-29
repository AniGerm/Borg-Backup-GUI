#!/bin/bash
# Installiert ein borg-backup-gui_*.deb.
# Aufruf: pkexec /usr/lib/borg-backup-gui/apply-update.sh /pfad/zum.deb
set -euo pipefail
DEB="${1:-}"
if [[ -z "$DEB" || ! -f "$DEB" ]]; then
  echo "Usage: $0 /path/to/borg-backup-gui_VERSION_amd64.deb" >&2
  exit 2
fi
case "$(basename "$DEB")" in
  borg-backup-gui_*.deb) ;;
  *)
    echo "Nur borg-backup-gui_*.deb erlaubt." >&2
    exit 2
    ;;
esac
export DEBIAN_FRONTEND=noninteractive
if command -v apt-get >/dev/null 2>&1; then
  apt-get install -y --reinstall "$DEB" || dpkg -i "$DEB"
else
  dpkg -i "$DEB"
fi
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database -q /usr/share/applications || true
fi
echo "borg-backup-gui Update installiert. Bitte App neu starten."
