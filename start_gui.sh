#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP="$SCRIPT_DIR/borg_backup_gui.py"
INIT_SCRIPT="$SCRIPT_DIR/install_desktop_launcher.sh"
NOPASSWD_SCRIPT="$SCRIPT_DIR/install_nopasswd_rule.sh"
MARKER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/borg-backup-gui"
MARKER_FILE="$MARKER_DIR/installed.marker"
NOPASSWD_MARKER="$MARKER_DIR/nopasswd.marker"

# System package: keep writable venv under the user's config dir.
if [[ "${BORG_BACKUP_GUI_SYSTEM:-}" == "1" ]] || [[ "$SCRIPT_DIR" == /usr/* ]]; then
    VENV_DIR="$MARKER_DIR/venv"
else
    VENV_DIR="$SCRIPT_DIR/.venv"
fi
VENV_PY="$VENV_DIR/bin/python"

if [[ ! -f "$APP" ]]; then
    echo "[FEHLER] App-Datei nicht gefunden: $APP" >&2
    exit 1
fi

mkdir -p "$MARKER_DIR"

if [[ ! -x "$VENV_PY" ]]; then
    echo "[INFO] Erzeuge venv in $VENV_DIR"
    python3 -m venv "$VENV_DIR"
    "$VENV_PY" -m pip install --upgrade pip >/dev/null
    "$VENV_PY" -m pip install -r "$SCRIPT_DIR/requirements.txt"
fi

# First-run launcher/nopasswd prompts are optional when already installed via .deb
# (desktop file is system-wide). Still offer NOPASSWD once.
if [[ ! -f "$MARKER_FILE" || ! -f "$NOPASSWD_MARKER" ]]; then
    if command -v python3 >/dev/null 2>&1; then
        if python3 - <<'PY'
import tkinter as tk
from tkinter import messagebox
root = tk.Tk()
root.withdraw()
answer = messagebox.askyesno(
    "Borg Backup GUI einrichten",
    "Die erste Einrichtung ist noch nicht vollständig.\n\n"
    "Launcher / Autostart und optional passwortfreie Borg-Rechte jetzt einrichten?\n"
    "(sudoers-Regel ist immer opt-in und wird bestätigt.)",
)
root.destroy()
raise SystemExit(0 if answer else 1)
PY
        then
            if [[ ! -f "$MARKER_FILE" ]]; then
                "$INIT_SCRIPT" || true
            fi
            if [[ ! -f "$NOPASSWD_MARKER" ]]; then
                "$NOPASSWD_SCRIPT" || true
            fi
        else
            # Allow skipping; in-app wizard will still guide the user.
            date -u +%FT%TZ >"$MARKER_FILE" || true
        fi
    fi
fi

SYS_DIST="/usr/lib/python3/dist-packages"
if [[ -d "$SYS_DIST" ]]; then
    export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$SYS_DIST"
fi

unset GTK_PATH GTK_EXE_PREFIX GTK_IM_MODULE_FILE GDK_PIXBUF_MODULE_FILE GTK_MODULES LD_LIBRARY_PATH

exec "$VENV_PY" "$APP"
