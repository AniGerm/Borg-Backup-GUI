#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# -------------------------------------------------------
# Als normaler User: Elevation via pkexec anfordern.
# -------------------------------------------------------
if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    if command -v pkexec >/dev/null 2>&1; then
        exec pkexec env \
            DISPLAY="${DISPLAY:-}" \
            XAUTHORITY="${XAUTHORITY:-}" \
            DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-}" \
            WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-}" \
            XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-}" \
            bash "$SCRIPT_DIR/install_nopasswd_rule.sh" "$@"
    fi
    echo "[FEHLER] pkexec ist nicht verfuegbar." >&2
    exit 1
fi

# -------------------------------------------------------
# Ab hier: Wir laufen als root (via pkexec).
# -------------------------------------------------------
REAL_UID="${PKEXEC_UID:-${SUDO_UID:-}}"
if [[ -z "$REAL_UID" ]]; then
    echo "[FEHLER] Konnte den aufrufenden Benutzer nicht ermitteln (PKEXEC_UID fehlt)." >&2
    exit 1
fi

CURRENT_USER="$(getent passwd "$REAL_UID" | cut -d: -f1)"
REAL_HOME="$(getent passwd "$REAL_UID" | cut -d: -f6)"
MARKER_DIR="$REAL_HOME/.config/borg-backup-gui"
MARKER_FILE="$MARKER_DIR/nopasswd.marker"
SUDOERS_FILE="/etc/sudoers.d/99-borg-backup-gui"
BORG_BIN="$(command -v borg || true)"

if [[ -z "$BORG_BIN" ]]; then
    echo "[FEHLER] borg wurde nicht gefunden." >&2
    exit 1
fi

# Alte Sudoers-Regel entfernen (Falls noch von hetzner-borg-gui)
rm -f "/etc/sudoers.d/99-hetzner-borg-gui"

TMP_FILE="$(mktemp)"
trap 'rm -f "$TMP_FILE"' EXIT

cat > "$TMP_FILE" <<EOF
Defaults!$BORG_BIN env_keep += "BORG_RSH BORG_PASSPHRASE BORG_RELOCATED_REPO_ACCESS_IS_OK BORG_CACHE_DIR AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_ENDPOINT_URL AWS_DEFAULT_REGION"
$CURRENT_USER ALL=(root) NOPASSWD: SETENV: $BORG_BIN
EOF

install -m 0440 "$TMP_FILE" "$SUDOERS_FILE"
visudo -cf "$SUDOERS_FILE" >/dev/null

mkdir -p "$MARKER_DIR"
date -u +%FT%TZ > "$MARKER_FILE"

echo "[OK] NOPASSWD-Regel installiert: $SUDOERS_FILE"
echo "[OK] Installationsmarker geschrieben: $MARKER_FILE"
echo "[HINWEIS] AWS_*-Umgebungsvariablen wurden zur env_keep-Liste hinzugefügt (S3-Unterstützung)."
