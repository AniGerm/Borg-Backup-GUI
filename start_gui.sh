#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP="$SCRIPT_DIR/borg_backup_gui.py"
APP_OLD="$SCRIPT_DIR/hetzner_borg_gui.py"
VENV_DIR="$SCRIPT_DIR/.venv"
VENV_PY="$VENV_DIR/bin/python"
INIT_SCRIPT="$SCRIPT_DIR/install_desktop_launcher.sh"
MARKER_DIR="$HOME/.config/borg-backup-gui"
MARKER_FILE="$MARKER_DIR/installed.marker"
NOPASSWD_SCRIPT="$SCRIPT_DIR/install_nopasswd_rule.sh"
NOPASSWD_MARKER="$MARKER_DIR/nopasswd.marker"

if [[ ! -f "$APP" ]] && [[ ! -f "$APP_OLD" ]]; then
    echo "[FEHLER] App-Datei nicht gefunden." >&2
    exit 1
fi

if [[ ! -x "$VENV_PY" ]]; then
    echo "[INFO] Erzeuge lokale venv in $VENV_DIR"
    python3 -m venv "$VENV_DIR"
    "$VENV_PY" -m pip install --upgrade pip >/dev/null
    "$VENV_PY" -m pip install -r "$SCRIPT_DIR/requirements.txt"
fi

if [[ ! -f "$MARKER_FILE" || ! -f "$NOPASSWD_MARKER" ]]; then
    if command -v python3 >/dev/null 2>&1; then
        if python3 - <<'PY'
import tkinter as tk
from tkinter import messagebox
root = tk.Tk()
root.withdraw()
answer = messagebox.askyesno(
    'Borg Backup GUI einrichten',
    'Die erste Einrichtung ist noch nicht vollstaendig abgeschlossen.\n\n'
    'Soll die fehlende Launcher-/Hintergrund-Konfiguration jetzt installiert werden?\n'
    'Danach startet die App automatisch neu.'
)
root.destroy()
raise SystemExit(0 if answer else 1)
PY
        then
            if [[ ! -f "$MARKER_FILE" ]]; then
                if ! "$INIT_SCRIPT"; then
                    python3 - <<'PY'
import tkinter as tk
from tkinter import messagebox
root = tk.Tk()
root.withdraw()
messagebox.showerror(
    'Borg Backup GUI',
    'Die Installation des Launchers ist fehlgeschlagen oder wurde abgebrochen.\n\n'
    'Bitte pruefe die Fehlermeldung im Terminal oder starte das Installationsskript manuell.'
)
root.destroy()
PY
                    exit 1
                fi
            fi
            if [[ ! -f "$NOPASSWD_MARKER" ]]; then
                if ! "$NOPASSWD_SCRIPT"; then
                    python3 - <<'PY'
import tkinter as tk
from tkinter import messagebox
root = tk.Tk()
root.withdraw()
messagebox.showerror(
    'Borg Backup GUI',
    'Die Einrichtung fuer passwortfreie Backups ist fehlgeschlagen oder wurde abgebrochen.\n\n'
    'Bitte pruefe die Fehlermeldung im Terminal oder starte das NOPASSWD-Skript manuell.'
)
root.destroy()
PY
                    exit 1
                fi
            fi
            exec "$0" "$@"
        fi
    fi
fi

# System-PyGObject fuer Tray-Zugriff
SYS_DIST="/usr/lib/python3/dist-packages"
if [[ -d "$SYS_DIST" ]]; then
    export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$SYS_DIST"
fi

# VS Code Snap-Umgebungsvariablen entfernen (Konflikte mit GTK)
unset GTK_PATH GTK_EXE_PREFIX GTK_IM_MODULE_FILE GDK_PIXBUF_MODULE_FILE GTK_MODULES LD_LIBRARY_PATH

# Neue App bevorzugen, alte als Fallback
if [[ -f "$APP" ]]; then
    exec "$VENV_PY" "$APP"
elif [[ -f "$APP_OLD" ]]; then
    exec "$VENV_PY" "$APP_OLD"
else
    echo "[FEHLER] Keine App-Datei gefunden." >&2
    exit 1
fi
