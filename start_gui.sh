#!/usr/bin/env bash
# Desktop entry point. System package: no runtime pip — deps come from apt + vendor/.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP="$SCRIPT_DIR/borg_backup_gui.py"

if [[ ! -f "$APP" ]]; then
    echo "[FEHLER] App-Datei nicht gefunden: $APP" >&2
    exit 1
fi

VENDOR="$SCRIPT_DIR/vendor"
if [[ -d "$VENDOR" ]]; then
    export PYTHONPATH="${VENDOR}${PYTHONPATH:+:$PYTHONPATH}"
fi

SYS_DIST="/usr/lib/python3/dist-packages"
if [[ -d "$SYS_DIST" ]]; then
    export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$SYS_DIST"
fi

unset GTK_PATH GTK_EXE_PREFIX GTK_IM_MODULE_FILE GDK_PIXBUF_MODULE_FILE GTK_MODULES LD_LIBRARY_PATH

# Prefer system python for packaged installs; allow checkout with .venv if present.
if [[ "${BORG_BACKUP_GUI_SYSTEM:-}" == "1" ]] || [[ "$SCRIPT_DIR" == /usr/* ]]; then
    PY=python3
elif [[ -x "$SCRIPT_DIR/.venv/bin/python" ]]; then
    PY="$SCRIPT_DIR/.venv/bin/python"
else
    PY=python3
fi

exec "$PY" "$APP" "$@"
