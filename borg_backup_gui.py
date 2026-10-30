#!/usr/bin/env python3
"""
Borg Backup GUI - Desktop-Oberfläche für den Backup-Dienst borg-backup-guid.

Die GUI läuft als normaler Benutzer. Backups, Wiederherstellungen und Zeitpläne
führt der root-Systemdienst aus; die GUI steuert ihn über einen Unix-Socket (polkit).
"""

__version__ = "3.0.0-rc3"

import datetime
import fcntl
import json
import os
import queue
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

for _vendor in (Path(__file__).resolve().parent / "vendor", Path("/usr/lib/borg-backup-gui/vendor")):
    if _vendor.is_dir() and str(_vendor) not in sys.path:
        sys.path.append(str(_vendor))

import borg_ipc
from borg_ipc import IpcClient, IpcError
from borg_model import (
    account_summary,
    account_usage_count,
    compute_next_run,
    destinations_for_job,
    enrich_source,
    ensure_sources,
    find_schedule_overlaps,
    format_schedule_summary,
    format_size,
    get_account,
    get_destination,
    get_job,
    list_backup_mounts,
    list_backup_volumes,
    list_expired_protocol_pdfs,
    list_protocol_pdfs,
    migrate_profiles_to_model,
    migrate_protocol_pdfs_into_subdir,
    new_id,
    protocol_dir,
    purge_old_protocol_pdfs,
    resolve_repo,
    suggest_excludes_for_path,
    sync_profiles_mirror,
    sync_sources_to_flat,
)
from borg_sysconfig import (
    assign_legacy_ssh_ports,
    config_fingerprint,
    extract_system_config,
    has_plain_secrets,
    mask_secrets,
    strip_user_secrets,
)

try:
    from PIL import Image, ImageDraw
except Exception:
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]

pystray = None  # Tray exclusively via Ayatana AppIndicator (no pystray dependency)

try:
    from PIL import ImageTk
except Exception:
    ImageTk = None  # type: ignore[assignment]

try:
    from fpdf import FPDF
except Exception:
    FPDF = None  # type: ignore[assignment,misc]

try:
    import gi

    gi.require_version("AyatanaAppIndicator3", "0.1")
    gi.require_version("Gtk", "3.0")
    from gi.repository import AyatanaAppIndicator3, GLib, Gtk
except Exception:
    AyatanaAppIndicator3 = None
    Gtk = None
    GLib = None

# ============================================================
# KONFIGURATION
# ============================================================
CONFIG_DIR = Path.home() / ".config" / "borg-backup-gui"
CONFIG_FILE = CONFIG_DIR / "config.json"
STATUS_FILE = CONFIG_DIR / "status.json"
ARCHIVE_SIZES_FILE = CONFIG_DIR / "archive_sizes.json"
INSTANCE_LOCK_FILE = CONFIG_DIR / "instance.lock"
INSTANCE_SOCKET_FILE = CONFIG_DIR / "instance.sock"
PROTOCOL_DIR = CONFIG_DIR / "protokolle"
ASSET_ICON_FILE = Path(__file__).resolve().parent / "assets" / "borg-backup-gui.svg"
DEFAULT_STORAGE = "uXXXXXX@uXXXXXX.your-storagebox.de:./backup"
INSTANCE_LOCK_HANDLE = None
SERVICE_NAME = "borg-backup-guid"
CANARY_FILE = "/var/lib/borg-backup-gui/canary-check.txt"
RESTORE_DOC_PATHS = (
    Path("/usr/share/doc/borg-backup-gui/RESTORE.md"),
    Path(__file__).resolve().parent / "docs" / "RESTORE.md",
)
AUTOSTART_DIR = Path.home() / ".config" / "autostart"
AUTOSTART_FILE = AUTOSTART_DIR / "borg-backup-gui.desktop"
SYSTEM_AUTOSTART_FILE = Path("/etc/xdg/autostart/borg-backup-gui.desktop")

# Live-Log darf nicht unbegrenzt wachsen (Tk-Text hält sonst GB im RAM).
LOG_MAX_LINES = 2000
LOG_TRIM_BATCH = 400
# Dateilisten-Zeilen aus borg --list nur alle N Einträge ins Log (Rest nur Live-Stats).
LOG_LIST_SAMPLE_EVERY = 250
DASHBOARD_THROTTLE_MS = 750

# Alte Config-Pfade (für Migration)
OLD_CONFIG_DIR = Path.home() / ".config" / "hetzner-borg-gui"
OLD_CONFIG_FILE = OLD_CONFIG_DIR / "config.json"
OLD_STATUS_FILE = OLD_CONFIG_DIR / "status.json"


def _migrate_old_config() -> None:
    """Kopiert alte Konfiguration ins neue Verzeichnis falls neu fehlt oder nur Platzhalter enthält."""
    if not OLD_CONFIG_FILE.exists():
        return  # Keine alte Config zum Migrieren

    should_migrate = False
    if not CONFIG_FILE.exists():
        should_migrate = True
    else:
        try:
            with open(CONFIG_FILE, "r") as file_handle:
                cur_data = json.load(file_handle)
            if "uXXXXXX" in str(cur_data.get("storage", "")):
                should_migrate = True
        except Exception:
            should_migrate = True

    if should_migrate:
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            import shutil as _shutil

            _shutil.copy2(str(OLD_CONFIG_FILE), str(CONFIG_FILE))
            if OLD_STATUS_FILE.exists() and not STATUS_FILE.exists():
                _shutil.copy2(str(OLD_STATUS_FILE), str(STATUS_FILE))
        except Exception:
            pass


def _activate_existing_instance():
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(1.0)
        client.connect(str(INSTANCE_SOCKET_FILE))
        client.sendall(b"SHOW\n")
        client.close()
        return True
    except OSError:
        return False


# ============================================================
# HAUPT-GUI-KLASSE
# ============================================================
class BorgBackupGUI:
    def __init__(self, master, start_hidden: bool = False):
        self.master = master
        self.start_hidden = start_hidden
        try:
            master.tk.call("tk", "appname", "BorgBackupGUI")
        except Exception:
            pass
        try:
            master.wm_class("BorgBackupGUI")
        except Exception:
            pass
        master.title(f"Borg Backup GUI v{__version__}")
        master.geometry("1100x780")
        self.ui = {
            "bg": "#0b1220",
            "surface": "#121a2b",
            "surface2": "#182338",
            "border": "#243149",
            "text": "#e6edf7",
            "muted": "#93a4bd",
            "accent": "#14b8a6",
            "danger": "#f87171",
            "ok": "#34d399",
            "font": ("IBM Plex Sans", 10),
            "font_bold": ("IBM Plex Sans", 10, "bold"),
            "font_mono": ("IBM Plex Mono", 9),
            "font_title": ("IBM Plex Sans", 13, "bold"),
        }
        self.style = ttk.Style()
        self.style.theme_use("clam")
        bg_color = self.ui["bg"]
        fg_color = self.ui["text"]
        acc_color = self.ui["accent"]
        btn_color = self.ui["surface2"]

        self.master.configure(bg=bg_color)

        # Dropdown-Menü Farben in Menüs (inkl Combobox Dropdown) global festlegen
        self.master.option_add("*TCombobox*Listbox.background", "#374151")
        self.master.option_add("*TCombobox*Listbox.foreground", "#f8fafc")
        self.master.option_add("*TCombobox*Listbox.selectBackground", "#0f766e")
        self.master.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        self.master.option_add("*Listbox.background", "#374151")
        self.master.option_add("*Listbox.foreground", "#f8fafc")
        self.master.option_add("*Listbox.selectBackground", "#0f766e")
        self.master.option_add("*Listbox.selectForeground", "#ffffff")

        self.style.configure(".", background=bg_color, foreground=fg_color)

        self.style.configure("TNotebook", background=bg_color, borderwidth=0)
        self.style.configure(
            "TNotebook.Tab",
            padding=[20, 8],
            font=("Segoe UI", 10, "bold"),
            background="#374151",
            foreground=fg_color,
            borderwidth=0,
        )
        self.style.map("TNotebook.Tab", background=[("selected", acc_color)], foreground=[("selected", "#ffffff")])

        self.style.configure("TFrame", background=bg_color)
        self.style.configure(
            "TLabel", background=bg_color, foreground=fg_color, font=self.ui.get("font", ("Segoe UI", 10))
        )
        self.style.configure(
            "Muted.TLabel",
            background=bg_color,
            foreground=self.ui.get("muted", "#93a4bd"),
            font=self.ui.get("font", ("Segoe UI", 10)),
        )
        self.style.configure(
            "Title.TLabel",
            background=bg_color,
            foreground=fg_color,
            font=self.ui.get("font_title", ("Segoe UI", 13, "bold")),
        )
        self.style.configure(
            "Accent.TButton",
            font=self.ui.get("font_bold", ("Segoe UI", 10, "bold")),
            padding=8,
            background=acc_color,
            foreground="#042f2e",
            borderwidth=0,
        )
        self.style.map("Accent.TButton", background=[("active", "#2dd4bf")])

        self.style.configure(
            "TButton",
            font=("Segoe UI", 10, "bold"),
            padding=8,
            background=btn_color,
            foreground=fg_color,
            borderwidth=0,
        )
        self.style.map("TButton", background=[("active", acc_color), ("disabled", "#475569")])

        self.style.configure(
            "TEntry", fieldbackground="#374151", foreground=fg_color, insertcolor=fg_color, padding=5, borderwidth=0
        )
        self.style.configure(
            "TSpinbox",
            fieldbackground="#374151",
            foreground=fg_color,
            background=btn_color,
            arrowcolor=fg_color,
            insertcolor=fg_color,
            padding=4,
        )
        self.style.map(
            "TSpinbox",
            fieldbackground=[("readonly", "#374151"), ("!disabled", "#374151")],
            foreground=[("readonly", fg_color), ("!disabled", fg_color)],
        )

        self.style.configure("TCombobox", fieldbackground="#374151", foreground=fg_color, background=btn_color)
        self.style.map(
            "TCombobox",
            fieldbackground=[("readonly", "#374151"), ("!readonly", "#374151")],
            foreground=[("readonly", fg_color), ("!readonly", fg_color)],
            selectbackground=[("readonly", acc_color)],
        )

        self.style.configure(
            "TLabelframe", background=bg_color, foreground=fg_color, borderwidth=1, bordercolor="#475569"
        )
        self.style.configure(
            "TLabelframe.Label", background=bg_color, foreground=acc_color, font=("Segoe UI", 11, "bold")
        )
        self.style.configure(
            "Treeview",
            background="#374151",
            foreground=fg_color,
            fieldbackground="#374151",
            borderwidth=0,
            rowheight=25,
        )
        self.style.map("Treeview", background=[("selected", acc_color)])
        self.style.configure(
            "Treeview.Heading", background=btn_color, foreground=fg_color, font=("Segoe UI", 10, "bold"), padding=5
        )

        self.config_data = self._load_config()
        self._init_profiles_from_config()
        self.status_data = self._load_status()
        self.archive_sizes_cache = self._load_archive_sizes_cache()

        self.log_widget = None
        self.restore_log = None
        self.ipc = IpcClient(timeout=30)
        self.ipc_slow = IpcClient(timeout=900)
        self.ipc_stop = threading.Event()
        self.daemon_online = False
        self.daemon_error = ""
        self.daemon_status: dict = {}
        self.daemon_fingerprint: str | None = None
        self.daemon_busy = False
        self._config_synced = False
        self._config_push_running = False
        self._config_push_again = False
        self._seen_finished_ids: set[str] = set()
        self.current_restore_id: str | None = None

        self.log_queue = queue.Queue()
        self.scheduled_job_id = None
        self.next_backup_dt = None
        self.next_scheduled_job_name = None
        self._schedule_volume_loading = False
        self._schedule_volume_prev = self.config_data.get("profile_name", "Standard")
        self.backup_running = False
        self.current_backup_job_name: str | None = None
        self.current_backup_started_at: datetime.datetime | None = None
        self.backup_files_seen = 0
        self.backup_last_item = ""
        self.backup_list_log_counter = 0
        self._dashboard_last_update_at = 0.0
        self.backup_task_queue: list = []
        self.backup_current_task: dict | None = None
        self.archive_list_refresh_pending = False
        self.tray_icon = None
        self.native_indicator = None
        self.native_indicator_thread = None
        self.native_indicator_supported = bool(AyatanaAppIndicator3 and Gtk and GLib)
        self.native_indicator_failed = False
        self.tray_supported = bool(self.native_indicator_supported and Image and ImageDraw)
        self.is_root = os.geteuid() == 0
        self.tray_runtime_available = self.tray_supported and not self.is_root
        self.tray_hint_shown = False
        self.notice_after_id = None
        self.archive_loading_token = 0
        self.tray_menu_signature = None
        self.tray_menu_last_refresh_at = None
        self.tray_icon_signature = None
        self.tray_native_menu_items = []
        self.tray_live_refresh_id = None
        self.tray_live_refresh_glib_id = None
        self.tray_running_animation_id = None
        self.tray_running_animation_phase = 0
        self.tray_ready = False
        self.window_icon_image = None
        self.instance_server = None
        self.instance_server_thread = None

        self._set_window_icon()

        self.footer = tk.Frame(master, bg="#111827")
        self.footer.pack(fill="x", side="bottom")
        self.version_footer_var = tk.StringVar(value=f"v{__version__}")
        self.version_footer_label = tk.Label(
            self.footer,
            textvariable=self.version_footer_var,
            anchor="e",
            bg="#111827",
            fg="#64748b",
            padx=12,
            pady=6,
            font=("IBM Plex Sans", 9),
        )
        self.version_footer_label.pack(side="right")
        self.notice_var = tk.StringVar(value="")
        self.notice_label = tk.Label(
            self.footer, textvariable=self.notice_var, anchor="w", bg="#111827", fg="#cbd5e1", padx=10, pady=6
        )
        self.notice_label.pack(side="left", fill="x", expand=True)

        self.daemon_banner = tk.Frame(master, bg="#7f1d1d")
        self.daemon_banner_var = tk.StringVar(value="")
        tk.Label(
            self.daemon_banner,
            textvariable=self.daemon_banner_var,
            bg="#7f1d1d",
            fg="#fee2e2",
            anchor="w",
            justify="left",
            padx=12,
            pady=8,
            wraplength=820,
        ).pack(side="left", fill="x", expand=True)
        self.daemon_start_btn = ttk.Button(
            self.daemon_banner, text="Dienst starten", command=self._start_daemon_service
        )
        self.daemon_start_btn.pack(side="right", padx=10, pady=6)

        self.notebook = ttk.Notebook(master)
        self.notebook.pack(fill="both", expand=True, padx=5, pady=5)

        self.tab_dashboard = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_dashboard, text="Übersicht")
        self._build_dashboard_tab()

        self.tab_backup = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_backup, text="Sicherung")
        self._build_backup_tab()

        self.tab_archive = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_archive, text="Archive")
        self._build_archive_tab()

        self.tab_restore = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_restore, text="Wiederherstellung")
        self._build_restore_tab()

        self.tab_schedule = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_schedule, text="Zeitplan")
        self._build_schedule_tab()

        self.tab_accounts = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_accounts, text="Konten")
        self._build_accounts_tab()

        self.tab_protocol = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_protocol, text="Protokolle")
        self._build_protocol_tab()

        self.master.after(100, self._process_log_queue)
        self._schedule_next_backup()
        self._update_dashboard_status()
        self.master.after(150, self._start_daemon_subscription)
        # Tray leicht verzögert starten, damit Tk/DBus/Panel stabil initialisiert sind.
        self.master.after(700, self._setup_tray_icon)
        self.master.after(8000, self._idle_archive_preload)
        self.master.after(60000, self._periodic_dashboard_refresh)
        self.master.protocol("WM_DELETE_WINDOW", self._on_close)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self.master.bind("<<ShowWindow>>", lambda e: self._show_window())
        try:
            self._setup_instance_server()
        except SystemExit:
            self.master.destroy()
            import sys

            sys.exit(0)
        self.master.bind("<<StartBackup>>", lambda e: self._start_backup())
        self.master.bind("<<TrayBackupToggle>>", lambda e: self._tray_backup_toggle())
        self.master.bind("<<QuitApp>>", lambda e: self._quit_app())
        if self.start_hidden and self.tray_runtime_available and self.config_data.get("tray_enabled", True):
            self.master.withdraw()
            self.master.after(8000, self._ensure_window_visible_if_tray_dead)
        else:
            self.master.after(120, self._ensure_window_visible)
        self.master.after(30000, self._periodic_tray_health_check)
        # Profil-Dropdown initialisieren
        self.master.after(500, self._refresh_profile_combo)
        self.master.after(600, self._refresh_archive_profile_combo)
        self.master.after(800, self._ensure_protocol_dir)
        self.master.after(1500, self._purge_old_protocol_pdfs)
        # Delayed auto-check; network calls are also cached in borg_updates (6h).
        self.master.after(120000, self._auto_check_app_update)
        self._update_prompt_pending = False

        if os.geteuid() == 0:
            messagebox.showwarning(
                "Hinweis",
                "Die GUI sollte als normaler Benutzer laufen.\n\n"
                "Backups führt der Systemdienst borg-backup-guid mit root-Rechten aus; "
                "die Oberfläche selbst braucht keine root-Rechte.",
            )

    def _on_close(self):
        tray_enabled = bool(self.config_data.get("tray_enabled", True))

        if self.tray_runtime_available and tray_enabled:
            self._save_config()
            self._hide_window_to_tray()
            return

        self._quit_app()

    def _load_config(self):
        _migrate_old_config()  # Einmalig: alte Config → neues Verzeichnis kopieren
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            if CONFIG_FILE.exists():
                with open(CONFIG_FILE, "r") as file_handle:
                    data = json.load(file_handle)
                    # Migration: Historische Eingaben wie "user@host:23/./backup" korrigieren,
                    # da der Port bereits über BORG_RSH gesetzt wird.
                    storage = str(data.get("storage", "")).strip()
                    if "://" not in storage and ":" in storage:
                        host_part, path_part = storage.split(":", 1)
                        if path_part.startswith("23/"):
                            data["storage"] = f"{host_part}:{path_part[3:]}"
                    return self._ensure_profile_keys(data)
        except Exception:
            pass

        return self._ensure_profile_keys(
            {
                "storage": DEFAULT_STORAGE,
                "ssh_key": "",
                "include_folders": ["/"],
                "exclude_folders": [
                    "/dev",
                    "/proc",
                    "/sys",
                    "/tmp",
                    "/run",
                    "/mnt",
                    "/media",
                    "/lost+found",
                    "/var/cache/apt/archives",
                    "/var/tmp",
                    "/home/*/.vagrant.d",
                    "/home/*/snap",
                    "/home/*/.local/share/Trash",
                ],
                "compression": "lz4",
                "encryption": "repokey",
                "borg_passphrase": "",
                "schedule_type": "manual",
                "schedule_interval": 3,
                "schedule_time": "03:15",
                "catchup_missed": True,
                "prune_enabled": False,
                "validate_enabled": True,
                "validate_interval": 3,
                "optimize_enabled": False,
                "optimize_interval": 3,
                "promptless_privilege": True,
                "tray_enabled": True,
                "tray_icon_style": "disk",
                "privilege_mode": "auto",
            }
        )

    @staticmethod
    def _ensure_profile_keys(data):
        """Stellt sicher, dass Profil-Keys vorhanden sind (abwärtskompatibel)."""
        data.setdefault("profile_type", "ssh")
        data.setdefault("profile_name", "Standard")
        data.setdefault("profiles", [])
        data.setdefault("s3_access_key", "")
        data.setdefault("s3_secret_key", "")
        data.setdefault("s3_endpoint_url", "https://fsn1.your-objectstorage.com")
        data.setdefault("s3_region", "fsn1")
        data.setdefault("local_path", "")
        data.setdefault("canary_enabled", True)
        data.setdefault("canary_last_check", "")
        data.setdefault("canary_last_result", "")
        data.setdefault("canary_expected_hash", "")
        data.setdefault("canary_verified_hash", "")
        data.setdefault("protocol_company_name", "")
        data.setdefault("protocol_server_name", os.uname().nodename)
        data.setdefault("protocol_info_text", "")
        data.setdefault("protocol_last_pdf", "")
        data.setdefault("protocol_retention_enabled", True)
        data.setdefault("protocol_retention_value", 3)
        data.setdefault("protocol_retention_unit", "months")
        data.setdefault("maintain_all_destinations", False)
        data.setdefault("auto_update_check", True)
        # Existing installs with a destination: don't force the first-run wizard again
        configured = bool(
            (data.get("storage") and "uXXXXXX" not in str(data.get("storage")))
            or data.get("local_path")
            or data.get("accounts")
        )
        if "setup_complete" not in data:
            data["setup_complete"] = configured
        if "repo_access_confirmed" not in data:
            data["repo_access_confirmed"] = configured
        data = migrate_profiles_to_model(data)
        ensure_sources(data)
        return data

    def _load_status(self):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            if STATUS_FILE.exists():
                with open(STATUS_FILE, "r") as file_handle:
                    data = json.load(file_handle)
                    data.setdefault("last_run_at", "")
                    data.setdefault("last_success_at", "")
                    data.setdefault("last_exit_code", None)
                    data.setdefault("last_error", "")
                    data.setdefault("last_duration_sec", None)
                    data.setdefault("last_files_count", 0)
                    data.setdefault("last_source_size", "")
                    data.setdefault("last_dedupe_size", "")
                    data.setdefault("last_item", "")
                    data.setdefault("jobs", {})
                    if not isinstance(data.get("jobs"), dict):
                        data["jobs"] = {}
                    return data
        except Exception:
            pass

        return {
            "last_run_at": "",
            "last_success_at": "",
            "last_exit_code": None,
            "last_error": "",
            "last_duration_sec": None,
            "last_files_count": 0,
            "last_source_size": "",
            "last_dedupe_size": "",
            "last_item": "",
            "jobs": {},
        }

    def _load_archive_sizes_cache(self) -> dict[str, str]:
        try:
            if ARCHIVE_SIZES_FILE.exists():
                with open(ARCHIVE_SIZES_FILE, "r") as file_handle:
                    data = json.load(file_handle)
                    if isinstance(data, dict):
                        return {str(k): str(v) for k, v in data.items()}
        except Exception:
            pass
        return {}

    def _save_archive_sizes_cache(self) -> None:
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with open(ARCHIVE_SIZES_FILE, "w") as file_handle:
                json.dump(self.archive_sizes_cache, file_handle, indent=2)
        except Exception:
            pass

    def _on_tab_changed(self, event=None):
        try:
            selected_tab = self.notebook.select()
            if selected_tab == str(self.tab_archive):
                pname = self.config_data.get("profile_name", "Standard")
                if pname in self.archive_cache:
                    self._display_archives_from_cache(pname)
                elif not self.backup_running and not self.tree.get_children():
                    self._load_archive_list()
            elif hasattr(self, "tab_protocol") and selected_tab == str(self.tab_protocol):
                self._purge_old_protocol_pdfs()
                self._refresh_protocol_pdf_list()
            elif hasattr(self, "tab_accounts") and selected_tab == str(self.tab_accounts):
                self._refresh_accounts_tree()
            elif selected_tab == str(self.tab_backup):
                self._refresh_destinations_tree()
        except Exception:
            pass

    def _idle_archive_preload(self) -> None:
        """Lädt im Leerlauf schonend die Archivliste vor, ohne Backups zu stören."""
        if not self.backup_running and not self.tree.get_children():
            pname = self.config_data.get("profile_name", "Standard")
            if pname in self.archive_cache:
                self._display_archives_from_cache(pname)
            else:
                self._load_archive_list()

    def _collect_config_from_ui(self):
        if hasattr(self, "storage_var"):
            self.config_data["storage"] = self.storage_var.get().strip()
        if hasattr(self, "s3_storage_var") and self.config_data.get("profile_type") == "s3":
            self.config_data["storage"] = self.s3_storage_var.get().strip()
        if hasattr(self, "local_path_var") and self.config_data.get("profile_type") == "local":
            self.config_data["local_path"] = self.local_path_var.get().strip()
            self.config_data["storage"] = self.local_path_var.get().strip()
        if hasattr(self, "ssh_key_var"):
            self.config_data["ssh_key"] = self.ssh_key_var.get().strip()
        if hasattr(self, "passphrase_var"):
            self.config_data["borg_passphrase"] = self.passphrase_var.get()
        if hasattr(self, "compression_var"):
            self.config_data["compression"] = self.compression_var.get().strip() or "lz4"
        # S3-spezifisch
        if hasattr(self, "s3_access_var"):
            self.config_data["s3_access_key"] = self.s3_access_var.get().strip()
        if hasattr(self, "s3_secret_var"):
            self.config_data["s3_secret_key"] = self.s3_secret_var.get()
        if hasattr(self, "s3_endpoint_var"):
            self.config_data["s3_endpoint_url"] = self.s3_endpoint_var.get().strip()
        if hasattr(self, "sources_tree"):
            self._collect_sources_from_ui()
        elif hasattr(self, "include_text"):
            includes = [line.strip() for line in self.include_text.get("1.0", tk.END).splitlines() if line.strip()]
            self.config_data["include_folders"] = includes or ["/"]
            if hasattr(self, "exclude_text"):
                excludes = [line.strip() for line in self.exclude_text.get("1.0", tk.END).splitlines() if line.strip()]
                self.config_data["exclude_folders"] = excludes
        if hasattr(self, "schedule_type_var"):
            self._sync_schedule_volume_from_ui()
        if hasattr(self, "tray_enabled_var"):
            self.config_data["tray_enabled"] = bool(self.tray_enabled_var.get())
        if hasattr(self, "tray_icon_style_var"):
            self.config_data["tray_icon_style"] = self.tray_icon_style_var.get().strip() or "disk"
        if hasattr(self, "protocol_company_var"):
            self.config_data["protocol_company_name"] = self.protocol_company_var.get().strip()
        if hasattr(self, "protocol_server_var"):
            self.config_data["protocol_server_name"] = self.protocol_server_var.get().strip()
        if hasattr(self, "protocol_info_var"):
            self.config_data["protocol_info_text"] = self.protocol_info_var.get().strip()
        if hasattr(self, "protocol_retention_enabled_var"):
            self.config_data["protocol_retention_enabled"] = bool(self.protocol_retention_enabled_var.get())
            try:
                self.config_data["protocol_retention_value"] = int(self.protocol_retention_value_var.get() or 3)
            except ValueError:
                self.config_data["protocol_retention_value"] = 3
            self.config_data["protocol_retention_unit"] = self.protocol_retention_unit_var.get().strip() or "months"
        if hasattr(self, "maintain_all_dest_var"):
            self.config_data["maintain_all_destinations"] = bool(self.maintain_all_dest_var.get())
        if hasattr(self, "auto_update_var"):
            self.config_data["auto_update_check"] = bool(self.auto_update_var.get())

    def _save_config(self, push: bool = True):
        try:
            self._collect_config_from_ui()
            if hasattr(self, "_sync_active_job_from_ui"):
                self._sync_active_job_from_ui()
            self._sync_flat_fields_to_model()
            sync_profiles_mirror(self.config_data)
            self._write_user_config()
        except Exception as exc:
            messagebox.showerror("Fehler", f"Konnte Konfiguration nicht speichern: {exc}")
            return
        if push:
            self._push_config_if_changed()

    def _write_user_config(self) -> None:
        """Persist GUI settings; once the service owns the config, secrets never touch ~/.config."""
        data = self.config_data
        if data.get("migrated_to_system"):
            data = strip_user_secrets(data)
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".json.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file_handle:
            json.dump(data, file_handle, indent=4)
        os.replace(tmp, CONFIG_FILE)

    FLAT_ACCOUNT_FIELDS = {
        "ssh_key": "ssh_key",
        "borg_passphrase": "passphrase",
        "s3_access_key": "s3_access_key",
        "s3_secret_key": "s3_secret_key",
        "s3_endpoint_url": "s3_endpoint_url",
        "s3_region": "s3_region",
    }

    def _primary_dest_and_account(self, job=None):
        job = job or self._active_job()
        if not job:
            return None, None
        dest_ids = list(job.get("destination_ids") or [])
        dest = get_destination(self.config_data, str(dest_ids[0])) if dest_ids else None
        if dest is None:
            return None, None
        return dest, get_account(self.config_data, str(dest.get("account_id") or ""))

    def _take_flat_snapshot(self) -> None:
        keys = ("storage", "local_path", *self.FLAT_ACCOUNT_FIELDS)
        self._flat_snapshot = {key: self.config_data.get(key, "") for key in keys}

    def _sync_flat_fields_to_model(self) -> None:
        """Write edited flat fields (Sicherung-Tab) into the active volume's primary target/account.

        Only fields the user changed since the last load are written, so edits made in the
        Konten tab are not overwritten by stale flat values.
        """
        snapshot = getattr(self, "_flat_snapshot", None)
        dest, account = self._primary_dest_and_account()
        if snapshot is None or dest is None or account is None:
            self._take_flat_snapshot()
            return

        def changed(key: str) -> bool:
            return self.config_data.get(key, "") != snapshot.get(key, "")

        if account.get("type") == "local":
            if changed("local_path") or changed("storage"):
                path = str(self.config_data.get("local_path") or self.config_data.get("storage") or "").strip()
                dest["storage"] = path
                dest["local_path"] = path
        elif changed("storage"):
            dest["storage"] = str(self.config_data.get("storage") or "").strip()
        for flat_key, account_key in self.FLAT_ACCOUNT_FIELDS.items():
            if changed(flat_key):
                account[account_key] = self.config_data.get(flat_key, "")
        self._take_flat_snapshot()

    def _save_status(self):
        """Status is owned by the service (/var/lib/borg-backup-gui); nothing to persist here."""

    # ============================================================
    # SYSTEMDIENST (borg-backup-guid)
    # ============================================================
    def _start_daemon_subscription(self) -> None:
        def on_event(event, data):
            self.master.after(0, lambda: self._on_daemon_event(event, data))

        def on_state(ok, message):
            self.master.after(0, lambda: self._set_daemon_online(ok, message))

        threading.Thread(
            target=borg_ipc.subscribe_forever,
            args=(on_event, self.ipc_stop),
            kwargs={"on_state": on_state},
            name="daemon-events",
            daemon=True,
        ).start()
        self.master.after(4000, self._check_daemon_reachable)

    def _check_daemon_reachable(self) -> None:
        if not self.daemon_online:
            self._set_daemon_online(False, self.daemon_error or "keine Verbindung")

    def _set_daemon_online(self, ok: bool, message: str = "") -> None:
        was_online = self.daemon_online
        self.daemon_online = ok
        self.daemon_error = "" if ok else message
        if ok:
            if self.daemon_banner.winfo_manager():
                self.daemon_banner.pack_forget()
            if not was_online:
                self._refresh_daemon_status()
                if not self._config_synced:
                    self._initial_config_sync()
            return
        socket_exists = os.path.exists(borg_ipc.DEFAULT_SOCKET)
        if "Authentifizierung" in message or "autorisiert" in message:
            text = f"Keine Berechtigung für den Backup-Dienst: {message}"
        elif socket_exists:
            text = f"Backup-Dienst antwortet nicht ({message})."
        else:
            text = (
                f"Der Backup-Dienst {SERVICE_NAME} läuft nicht. Ohne ihn finden keine Backups statt — "
                "auch keine geplanten."
            )
        self.daemon_banner_var.set(text)
        if not self.daemon_banner.winfo_manager():
            self.daemon_banner.pack(fill="x", side="top", before=self.notebook)
        if was_online:
            self.backup_running = False
            self.daemon_busy = False
            self._update_action_buttons()
            self._update_dashboard_status(force=True)

    def _start_daemon_service(self) -> None:
        def worker():
            try:
                proc = subprocess.run(
                    ["pkexec", "systemctl", "enable", "--now", SERVICE_NAME],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                ok, err = proc.returncode == 0, (proc.stderr or proc.stdout).strip()
            except (OSError, subprocess.TimeoutExpired) as exc:
                ok, err = False, str(exc)
            self.master.after(0, lambda: self._on_daemon_service_started(ok, err))

        self.daemon_start_btn.state(["disabled"])
        threading.Thread(target=worker, daemon=True).start()

    def _on_daemon_service_started(self, ok: bool, err: str) -> None:
        self.daemon_start_btn.state(["!disabled"])
        if ok:
            self._show_notice("Backup-Dienst gestartet.", level="success")
        else:
            self._show_notice(f"Dienst konnte nicht gestartet werden: {err or 'abgebrochen'}", level="error")

    def _daemon_call_async(self, method, params=None, on_done=None, *, slow=False):
        """Call the service in a worker thread; ``on_done(result, error)`` runs in the Tk thread."""
        client = self.ipc_slow if slow else self.ipc

        def worker():
            try:
                result, error = client.call(method, params or {}), None
            except IpcError as exc:
                result, error = None, exc
            except Exception as exc:
                result, error = None, IpcError("internal", str(exc))
            if on_done is not None:
                self.master.after(0, lambda: on_done(result, error))

        threading.Thread(target=worker, daemon=True).start()

    def _refresh_daemon_status(self) -> None:
        def done(result, error):
            if error is None and isinstance(result, dict):
                self._apply_daemon_status(result)

        self._daemon_call_async("get_status", on_done=done)

    def _on_daemon_event(self, event: str, data) -> None:
        if event == "status" and isinstance(data, dict):
            self._apply_daemon_status(data)
        elif event == "log" and isinstance(data, dict):
            channel = "restore" if data.get("channel") == "restore" else "backup"
            self.log_queue.put((channel, str(data.get("text", "")) + "\n"))
        elif event == "progress" and isinstance(data, dict):
            current = self.daemon_status.get("current") or {}
            channel = "restore" if current.get("kind") == "extract" else "backup"
            if channel == "backup":
                self.log_queue.put(("backup", str(data.get("line", "")) + "\n"))
            else:
                self._update_restore_progress(current)
        elif event == "job_finished" and isinstance(data, dict):
            self._on_daemon_job_finished(data)

    def _apply_daemon_status(self, snap: dict) -> None:
        self.daemon_status = snap
        current = snap.get("current") or None
        was_running = self.backup_running
        self.daemon_busy = current is not None
        self.backup_running = bool(current and current.get("kind") == "backup")
        if self.backup_running and isinstance(current, dict):
            self.current_backup_job_name = str(current.get("job") or "")
            self.current_backup_started_at = self._parse_iso_time(str(current.get("started_at") or ""))
            self.backup_current_task = {"label": current.get("task_label", "")}
            stats = current.get("stats") or {}
            self.status_data["last_files_count"] = stats.get("files_count") or 0
            self.status_data["last_source_size"] = stats.get("source_size") or ""
            self.status_data["last_dedupe_size"] = stats.get("dedupe_size") or ""
            self.status_data["last_item"] = stats.get("last_item") or ""
            if not was_running:
                self._reset_backup_progress()
        else:
            self.current_backup_job_name = None
            self.current_backup_started_at = None
            self.backup_current_task = None
            last = snap.get("last") or {}
            for key, value in last.items():
                if key.startswith("canary_"):
                    self.config_data[key] = value or ""
                elif key not in ("job", "archive", "trigger"):
                    self.status_data[key] = value
        self.status_data["jobs"] = snap.get("jobs") or {}

        next_runs = []
        for name, entry in (snap.get("jobs") or {}).items():
            when = self._parse_iso_time(str((entry or {}).get("next_run_at") or ""))
            if when:
                next_runs.append((name, when))
        if next_runs:
            self.next_scheduled_job_name, self.next_backup_dt = min(next_runs, key=lambda item: item[1])
        else:
            self.next_scheduled_job_name, self.next_backup_dt = None, None

        self._sync_mount_state(snap.get("mounts") or [])
        self._update_restore_progress(current if current and current.get("kind") == "extract" else None)
        self._update_action_buttons()
        self._refresh_schedule_overview()
        self._update_dashboard_status(force=True)
        self._schedule_tray_live_refresh(1000)

        fingerprint = snap.get("config_fingerprint")
        if (
            self._config_synced
            and fingerprint
            and self.daemon_fingerprint
            and fingerprint != self.daemon_fingerprint
            and not self._config_push_running
        ):
            self._reload_config_from_daemon()

    def _update_action_buttons(self) -> None:
        running = self.backup_running
        if hasattr(self, "backup_btn"):
            self.backup_btn.config(state="disabled" if running or not self.daemon_online else "normal")
        if hasattr(self, "stop_btn"):
            self.stop_btn.config(state="normal" if running else "disabled")
        if hasattr(self, "restore_start_btn"):
            restoring = bool((self.daemon_status.get("current") or {}).get("kind") == "extract")
            self.restore_start_btn.config(state="disabled" if restoring or not self.daemon_online else "normal")
            self.restore_cancel_btn.config(state="normal" if restoring else "disabled")

    def _on_daemon_job_finished(self, data: dict) -> None:
        item_id = str(data.get("id") or "")
        if item_id in self._seen_finished_ids:
            return
        self._seen_finished_ids.add(item_id)
        kind = data.get("kind")
        if kind == "backup":
            self._on_backup_finished(data.get("job") or "", data.get("status") or {})
        elif kind == "extract":
            info = data.get("status") or {}
            self.current_restore_id = None
            if info.get("ok"):
                text = f"Wiederherstellung von {info.get('archive')} nach {info.get('target')} abgeschlossen."
                self._append_restore_log(f"\n[OK] {text}\n")
                self._show_notice(text, level="success", timeout_ms=12000)
            else:
                text = f"Wiederherstellung fehlgeschlagen: {info.get('error') or info.get('exit_code')}"
                self._append_restore_log(f"\n[FEHLER] {text}\n")
                self._show_notice(text, level="error", timeout_ms=15000)
        elif kind == "delete":
            if data.get("ok"):
                self._show_notice(f"Archiv {data.get('archive')} gelöscht.", level="success")
                self.archive_cache.pop(self.config_data.get("profile_name", "Standard"), None)
                self._load_archive_list()
            else:
                self._show_notice(f"Löschen fehlgeschlagen: {data.get('error')}", level="error", timeout_ms=12000)
        elif kind == "init":
            if data.get("ok"):
                self._show_notice("Repository initialisiert.", level="success")
            else:
                self._show_notice(
                    f"Initialisierung fehlgeschlagen: {data.get('error')}", level="error", timeout_ms=15000
                )

    def _on_backup_finished(self, job_name: str, status: dict) -> None:
        exit_code = status.get("last_exit_code")
        error = str(status.get("last_error") or "")
        succeeded = bool(status.get("last_success_at")) and status.get("last_run_at", "") <= status.get(
            "last_success_at", ""
        )
        if succeeded and exit_code in (0, 1) and not error:
            self._show_notice(f"Backup „{job_name}“ erfolgreich beendet.", level="success", timeout_ms=10000)
        elif succeeded:
            self._show_notice(f"Backup „{job_name}“: {error}", level="warning", timeout_ms=14000)
        else:
            self._show_notice(f"Backup „{job_name}“ nicht erfolgreich: {error}", level="error", timeout_ms=15000)
        auto_pdf = getattr(self, "protocol_auto_var", None)
        if succeeded and auto_pdf is not None and auto_pdf.get():
            self.master.after(1500, lambda: self._generate_pdf_report(auto_generated=True))
        self._refresh_archive_cache()

    # --------------------------------------------------------- Konfiguration
    def _system_config_payload(self) -> dict:
        return extract_system_config(self.config_data)

    def _push_config_if_changed(self, on_done=None, force: bool = False) -> None:
        """Send the volume/target config to the service if it differs (polkit: configure)."""
        callbacks = getattr(self, "_push_callbacks", [])
        self._push_callbacks = callbacks
        if not self.daemon_online or not self._config_synced:
            if on_done:
                on_done(False, "Backup-Dienst nicht verbunden.")
            return
        payload = self._system_config_payload()
        if (
            not force
            and config_fingerprint(mask_secrets(payload)) == self.daemon_fingerprint
            and not has_plain_secrets(payload)
        ):
            if on_done:
                on_done(True, "")
            return
        if on_done:
            callbacks.append(on_done)
        if self._config_push_running:
            self._config_push_again = True
            return
        self._config_push_running = True
        self._daemon_call_async("save_config", {"config": payload}, on_done=self._on_config_pushed, slow=True)

    def _on_config_pushed(self, result, error) -> None:
        self._config_push_running = False
        callbacks, self._push_callbacks = list(getattr(self, "_push_callbacks", [])), []
        if error is None and isinstance(result, dict):
            self.daemon_fingerprint = result.get("fingerprint")
            self._adopt_system_config(result.get("config") or {}, reapply_profile=False)
            self._write_user_config()
            ok, message = True, ""
        else:
            ok = False
            message = error.message if error else "Unbekannter Fehler"
            if error is not None and error.code == "invalid":
                messagebox.showerror("Konfiguration ungültig", message)
            elif error is not None and error.code == "unauthorized":
                self._show_notice(
                    "Änderungen nicht an den Backup-Dienst übertragen: Authentifizierung abgebrochen.",
                    level="warning",
                    timeout_ms=12000,
                )
            else:
                self._show_notice(f"Konfiguration nicht übertragen: {message}", level="error", timeout_ms=12000)
        for callback in callbacks:
            callback(ok, message)
        if self._config_push_again:
            self._config_push_again = False
            self._push_config_if_changed()

    def _adopt_system_config(self, cfg: dict, *, reapply_profile: bool = True) -> None:
        """Take accounts/targets/volumes from the service (masked secrets) as source of truth."""
        if not cfg:
            return
        gui_sources = {
            str(job.get("name")): job.get("sources") for job in self.config_data.get("jobs") or [] if job.get("sources")
        }
        jobs = []
        for job in cfg.get("jobs") or []:
            job = dict(job)
            previous = {str(s.get("path")): s for s in gui_sources.get(job["name"]) or [] if isinstance(s, dict)}
            job["sources"] = [{**previous.get(src["path"], {}), **src} for src in job.get("sources") or []]
            jobs.append(job)
        self.config_data["accounts"] = list(cfg.get("accounts") or [])
        self.config_data["destinations"] = list(cfg.get("destinations") or [])
        self.config_data["jobs"] = jobs
        for key in ("canary_enabled", "repo_access_confirmed", "maintain_all_destinations"):
            if key in cfg:
                self.config_data[key] = bool(cfg[key])
        if jobs:
            self.config_data["setup_complete"] = True
        sync_profiles_mirror(self.config_data)
        active = self.config_data.get("profile_name")
        names = [j["name"] for j in jobs]
        if names and (reapply_profile or active not in names):
            self._apply_profile(active if active in names else names[0])
        else:
            dest, account = self._primary_dest_and_account()
            if account is not None:
                for flat_key, account_key in self.FLAT_ACCOUNT_FIELDS.items():
                    self.config_data[flat_key] = account.get(account_key, "")
                if dest is not None:
                    self.config_data["storage"] = dest.get("storage", "")
                    self.config_data["local_path"] = dest.get("local_path", "")
                self._sync_config_to_ui()
                self._take_flat_snapshot()
        if hasattr(self, "maintain_all_dest_var"):
            self.maintain_all_dest_var.set(bool(self.config_data.get("maintain_all_destinations")))
        self._refresh_accounts_tree()
        self._refresh_destinations_tree()
        self._refresh_schedule_volume_combo()
        self._refresh_archive_profile_combo()
        self._refresh_restore_job_combo()

    def _reload_config_from_daemon(self) -> None:
        def done(result, error):
            if error is None and isinstance(result, dict):
                self.daemon_fingerprint = result.get("fingerprint")
                self._adopt_system_config(result.get("config") or {})
                self._write_user_config()

        self._daemon_call_async("get_config", on_done=done)

    def _has_legacy_setup(self) -> bool:
        if self.config_data.get("migrated_to_system") or not self.config_data.get("setup_complete"):
            return False
        for dest in self.config_data.get("destinations") or []:
            repo = str(dest.get("storage") or dest.get("local_path") or "")
            if repo and "uXXXXXX" not in repo:
                return True
        return False

    def _initial_config_sync(self) -> None:
        def done(result, error):
            if error is not None or not isinstance(result, dict):
                self._show_notice(
                    f"Konfiguration des Dienstes nicht lesbar: {error.message if error else '?'}", level="error"
                )
                return
            daemon_cfg = result.get("config") or {}
            if not daemon_cfg.get("jobs"):
                if self._has_legacy_setup():
                    self._offer_legacy_import()
                    return
                self.daemon_fingerprint = result.get("fingerprint")
                self._config_synced = True
                self.config_data["migrated_to_system"] = True
                if not self.config_data.get("setup_complete"):
                    self._show_first_run_wizard()
                else:
                    self._push_config_if_changed()
                return
            self.daemon_fingerprint = result.get("fingerprint")
            self._config_synced = True
            self.config_data["migrated_to_system"] = True
            self._adopt_system_config(daemon_cfg)
            self._write_user_config()

        self._daemon_call_async("get_config", on_done=done)

    def _offer_legacy_import(self) -> None:
        names = ", ".join(str(j.get("name")) for j in self.config_data.get("jobs") or [])
        if not messagebox.askyesno(
            "Konfiguration übernehmen",
            "Ab Version 3 führt der Systemdienst borg-backup-guid alle Backups aus.\n\n"
            f"Sollen die vorhandenen Backup-Volumen ({names}) samt Zielen, SSH-Keys und "
            "Passphrasen jetzt in den Dienst übernommen werden?\n\n"
            "• Dafür ist einmalig das Administrator-Passwort nötig.\n"
            "• Zugangsdaten liegen danach nur noch root-geschützt unter /etc/borg-backup-gui.\n"
            "• Der Borg-Cache wird beim ersten Lauf neu aufgebaut (dauert einmalig länger).",
        ):
            self._show_notice(
                "Nicht übernommen: Solange der Dienst keine Konfiguration hat, laufen keine Backups.",
                level="warning",
                timeout_ms=15000,
            )
            return
        payload = dict(self.config_data)
        payload["accounts"] = [dict(a) for a in self.config_data.get("accounts") or []]
        assign_legacy_ssh_ports(payload)
        legacy_status = {}
        try:
            with open(STATUS_FILE, encoding="utf-8") as handle:
                legacy_status = json.load(handle)
        except (OSError, json.JSONDecodeError):
            pass

        def done(result, error):
            if error is not None or not isinstance(result, dict):
                messagebox.showerror(
                    "Übernahme fehlgeschlagen",
                    f"{error.message if error else 'Unbekannter Fehler'}\n\n"
                    "Die bisherige Konfiguration bleibt unverändert. Erneut versuchen über "
                    "Übersicht → „Mit Dienst synchronisieren“.",
                )
                return
            self.daemon_fingerprint = result.get("fingerprint")
            self._config_synced = True
            self.config_data["migrated_to_system"] = True
            self._adopt_system_config(result.get("config") or {})
            self._write_user_config()
            self._show_notice("Konfiguration in den Backup-Dienst übernommen.", level="success", timeout_ms=10000)
            self._refresh_daemon_status()

        self._daemon_call_async(
            "import_legacy",
            {"config": extract_system_config(payload), "status": legacy_status},
            on_done=done,
            slow=True,
        )

    def _manual_daemon_sync(self) -> None:
        if not self.daemon_online:
            self._show_notice("Backup-Dienst nicht verbunden.", level="warning")
            return
        if self._config_synced:
            self._save_config(push=False)
            self._push_config_if_changed(
                on_done=lambda ok, _m: ok and self._show_notice("Mit dem Dienst synchronisiert.", level="success"),
                force=True,
            )
        else:
            self._initial_config_sync()

    def _build_schedule_tab(self):
        tab = self.tab_schedule

        volume_frame = ttk.LabelFrame(tab, text="Backup-Volumen")
        volume_frame.pack(fill="x", padx=10, pady=10)
        ttk.Label(volume_frame, text="Zeitplan für:").grid(row=0, column=0, sticky="w", padx=5, pady=8)
        self.schedule_volume_var = tk.StringVar(value=self.config_data.get("profile_name", "Standard"))
        self.schedule_volume_combo = ttk.Combobox(
            volume_frame, textvariable=self.schedule_volume_var, state="readonly", width=40
        )
        self.schedule_volume_combo.grid(row=0, column=1, sticky="w", padx=5, pady=8)
        self.schedule_volume_combo.bind("<<ComboboxSelected>>", self._on_schedule_volume_selected)
        ttk.Label(
            volume_frame,
            text="Jedes Backup-Volumen (Profil) hat einen eigenen Zeitplan.",
            style="Muted.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=5, pady=(0, 8))

        sched_frame = ttk.LabelFrame(tab, text="Backup-Zeitplan")
        sched_frame.pack(fill="x", padx=10, pady=10)

        self.schedule_type_var = tk.StringVar(value=self.config_data.get("schedule_type", "manual"))

        ttk.Radiobutton(
            sched_frame,
            text="Nur manuell",
            variable=self.schedule_type_var,
            value="manual",
            command=self._on_schedule_fields_changed,
        ).grid(row=0, column=0, sticky="w", padx=5, pady=5)
        ttk.Radiobutton(
            sched_frame,
            text="Regelmäßiges Backup",
            variable=self.schedule_type_var,
            value="interval",
            command=self._on_schedule_fields_changed,
        ).grid(row=1, column=0, sticky="w", padx=5, pady=5)

        frame_interval = ttk.Frame(sched_frame)
        frame_interval.grid(row=1, column=1, sticky="w")
        ttk.Label(frame_interval, text="Intervall:").pack(side="left", padx=5)
        self.schedule_interval_var = tk.StringVar(value=str(self.config_data.get("schedule_interval", 3)))
        ttk.Spinbox(
            frame_interval,
            from_=1,
            to=168,
            textvariable=self.schedule_interval_var,
            width=5,
            command=self._on_schedule_fields_changed,
        ).pack(side="left")
        ttk.Label(frame_interval, text="Stunden").pack(side="left", padx=5)

        ttk.Radiobutton(
            sched_frame,
            text="Tägliches Backup",
            variable=self.schedule_type_var,
            value="daily",
            command=self._on_schedule_fields_changed,
        ).grid(row=2, column=0, sticky="w", padx=5, pady=5)

        frame_daily = ttk.Frame(sched_frame)
        frame_daily.grid(row=2, column=1, sticky="w")
        ttk.Label(frame_daily, text="Uhrzeit:").pack(side="left", padx=5)
        self.schedule_time_var = tk.StringVar(value=self.config_data.get("schedule_time", "03:15"))
        ttk.Entry(frame_daily, textvariable=self.schedule_time_var, width=8).pack(side="left")

        self.catchup_var = tk.BooleanVar(value=bool(self.config_data.get("catchup_missed", True)))
        ttk.Checkbutton(
            sched_frame,
            text="Verpasste Backups nachholen",
            variable=self.catchup_var,
            command=self._on_schedule_fields_changed,
        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=5, pady=10)

        overview_frame = ttk.LabelFrame(tab, text="Alle Volumen")
        overview_frame.pack(fill="both", expand=True, padx=10, pady=10)
        self.schedule_overview_var = tk.StringVar(value="")
        ttk.Label(overview_frame, textvariable=self.schedule_overview_var, justify="left").pack(
            anchor="w", padx=8, pady=6
        )
        self.schedule_overlap_var = tk.StringVar(value="")
        self.schedule_overlap_label = tk.Label(
            overview_frame,
            textvariable=self.schedule_overlap_var,
            justify="left",
            wraplength=760,
            bg="#0b1220",
            fg="#fbbf24",
            anchor="w",
        )
        self.schedule_overlap_label.pack(fill="x", padx=8, pady=(0, 8))

        maint_frame = ttk.LabelFrame(tab, text="Wartung nach dem Backup")
        maint_frame.pack(fill="x", padx=10, pady=10)

        self.prune_var = tk.BooleanVar(value=bool(self.config_data.get("prune_enabled", False)))
        ttk.Checkbutton(maint_frame, text="Ausdünnen: Nach jedem Backup", variable=self.prune_var).grid(
            row=0, column=0, sticky="w", padx=5, pady=5
        )

        self.validate_var = tk.BooleanVar(value=bool(self.config_data.get("validate_enabled", True)))
        ttk.Checkbutton(maint_frame, text="Validierung: Repositorydaten überprüfen", variable=self.validate_var).grid(
            row=1, column=0, sticky="w", padx=5, pady=5
        )

        frame_val = ttk.Frame(maint_frame)
        frame_val.grid(row=1, column=1, sticky="w")
        ttk.Label(frame_val, text="Intervall:").pack(side="left", padx=5)
        self.validate_interval_var = tk.StringVar(value=str(self.config_data.get("validate_interval", 3)))
        ttk.Spinbox(frame_val, from_=1, to=52, textvariable=self.validate_interval_var, width=5).pack(side="left")
        ttk.Label(frame_val, text="Wochen").pack(side="left", padx=5)

        self.optimize_var = tk.BooleanVar(value=bool(self.config_data.get("optimize_enabled", False)))
        ttk.Checkbutton(maint_frame, text="Optimierung: Repository optimieren", variable=self.optimize_var).grid(
            row=2, column=0, sticky="w", padx=5, pady=5
        )

        frame_opt = ttk.Frame(maint_frame)
        frame_opt.grid(row=2, column=1, sticky="w")
        ttk.Label(frame_opt, text="Intervall:").pack(side="left", padx=5)
        self.optimize_interval_var = tk.StringVar(value=str(self.config_data.get("optimize_interval", 3)))
        ttk.Spinbox(frame_opt, from_=1, to=52, textvariable=self.optimize_interval_var, width=5).pack(side="left")
        ttk.Label(frame_opt, text="Wochen").pack(side="left", padx=5)

        self.maintain_all_dest_var = tk.BooleanVar(value=bool(self.config_data.get("maintain_all_destinations", False)))
        ttk.Checkbutton(
            maint_frame,
            text="Wartung (Prune/Check/Compact) auch auf alle weiteren Backup-Ziele",
            variable=self.maintain_all_dest_var,
        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=5, pady=8)

        ttk.Button(tab, text="Einstellungen speichern & anwenden", command=self._on_schedule_change).pack(pady=10)
        self.master.after(0, self._refresh_schedule_volume_combo)

    def _build_dashboard_tab(self):
        tab = self.tab_dashboard
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        ttk.Label(tab, text="Backup Übersicht", font=("Arial", 16, "bold")).grid(
            row=0, column=0, sticky="w", padx=12, pady=(12, 6)
        )

        status_frame = ttk.LabelFrame(tab, text="Aktueller Status")
        status_frame.grid(row=1, column=0, sticky="nsew", padx=12, pady=8)
        status_frame.columnconfigure(1, weight=1)

        self.status_canvas = tk.Canvas(status_frame, width=52, height=52, bg="#2e2e2e", highlightthickness=0)
        self.status_canvas.grid(row=0, column=0, rowspan=4, padx=8, pady=8, sticky="n")
        self.status_light = self.status_canvas.create_oval(6, 6, 46, 46, fill="#6e6e6e", outline="")

        self.status_title_var = tk.StringVar(value="Noch kein Backup ausgeführt")
        ttk.Label(status_frame, textvariable=self.status_title_var, font=("Arial", 12, "bold")).grid(
            row=0, column=1, sticky="w", padx=6, pady=(8, 4)
        )

        self.last_backup_var = tk.StringVar(value="Letzter Lauf: noch nie")
        ttk.Label(status_frame, textvariable=self.last_backup_var).grid(row=1, column=1, sticky="w", padx=6, pady=2)

        self.next_backup_var = tk.StringVar(value="Nächstes geplantes Backup: deaktiviert")
        ttk.Label(status_frame, textvariable=self.next_backup_var).grid(row=2, column=1, sticky="w", padx=6, pady=2)

        self.last_error_var = tk.StringVar(value="Letzte Meldung: noch keine")
        ttk.Label(status_frame, textvariable=self.last_error_var, wraplength=760).grid(
            row=3, column=1, sticky="w", padx=6, pady=(2, 8)
        )

        self.canary_status_var = tk.StringVar(value="Canary: noch kein Check")
        ttk.Label(status_frame, textvariable=self.canary_status_var, font=("Arial", 9)).grid(
            row=4, column=1, sticky="w", padx=6, pady=(0, 6)
        )

        action_frame = ttk.LabelFrame(tab, text="Schnellaktionen")
        action_frame.grid(row=2, column=0, sticky="ew", padx=12, pady=8)
        ttk.Button(action_frame, text="Backup jetzt starten", command=self._start_backup).pack(
            side="left", padx=8, pady=8
        )
        ttk.Button(action_frame, text="Status aktualisieren", command=self._refresh_dashboard_status).pack(
            side="left", padx=8, pady=8
        )

        update_frame = ttk.LabelFrame(tab, text="Software-Update")
        update_frame.grid(row=3, column=0, sticky="ew", padx=12, pady=8)
        self.update_status_var = tk.StringVar(value=f"Version {__version__}")
        ttk.Label(update_frame, textvariable=self.update_status_var).pack(side="left", padx=8, pady=8)
        self.auto_update_var = tk.BooleanVar(value=bool(self.config_data.get("auto_update_check", True)))
        ttk.Checkbutton(update_frame, text="Automatisch prüfen", variable=self.auto_update_var).pack(
            side="left", padx=8, pady=8
        )
        ttk.Button(update_frame, text="Nach Update suchen", command=self._check_for_app_update).pack(
            side="left", padx=8, pady=8
        )
        ttk.Button(
            update_frame, text="Update installieren", style="Accent.TButton", command=self._install_app_update
        ).pack(side="left", padx=8, pady=8)

        stats_frame = ttk.LabelFrame(tab, text="Backup-Statistik")
        stats_frame.grid(row=4, column=0, sticky="ew", padx=12, pady=8)
        stats_frame.columnconfigure(1, weight=1)

        self.backup_files_var = tk.StringVar(value="Gefundene/verarbeitete Dateien: 0")
        self.backup_source_size_var = tk.StringVar(value="Quellgröße: noch keine Daten")
        self.backup_dedupe_size_var = tk.StringVar(value="Dedupliziert: noch keine Daten")
        self.backup_last_item_var = tk.StringVar(value="Zuletzt: noch keine Daten")

        ttk.Label(stats_frame, textvariable=self.backup_files_var).grid(
            row=0, column=0, sticky="w", padx=8, pady=(8, 2)
        )
        ttk.Label(stats_frame, textvariable=self.backup_source_size_var).grid(
            row=0, column=1, sticky="w", padx=8, pady=(8, 2)
        )
        ttk.Label(stats_frame, textvariable=self.backup_dedupe_size_var).grid(
            row=1, column=0, sticky="w", padx=8, pady=(2, 4)
        )
        ttk.Label(stats_frame, textvariable=self.backup_last_item_var, wraplength=720).grid(
            row=1, column=1, sticky="w", padx=8, pady=(2, 4)
        )
        ttk.Label(
            stats_frame,
            text="Hinweis: Quell- und Deduplizierungsgröße werden beim Abschluss des Backups final berechnet.",
            font=("Segoe UI", 8),
            foreground="#94a3b8",
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 6))

        tray_frame = ttk.LabelFrame(tab, text="Hintergrundsymbol in der Statusleiste")
        tray_frame.grid(row=5, column=0, sticky="ew", padx=12, pady=(0, 12))
        tray_frame.columnconfigure(3, weight=1)

        self.tray_enabled_var = tk.BooleanVar(value=bool(self.config_data.get("tray_enabled", True)))
        tray_check = ttk.Checkbutton(
            tray_frame,
            text="Tray-Symbol aktivieren (im Hintergrund laufen)",
            variable=self.tray_enabled_var,
            command=self._on_tray_settings_change,
        )
        tray_check.grid(row=0, column=0, padx=8, pady=8, sticky="w")

        ttk.Label(tray_frame, text="Icon-Stil:").grid(row=0, column=1, padx=(8, 4), pady=8, sticky="e")
        self.tray_icon_style_var = tk.StringVar(value=self.config_data.get("tray_icon_style", "disk"))
        tray_style_combo = ttk.Combobox(
            tray_frame,
            textvariable=self.tray_icon_style_var,
            values=["disk", "shield", "palm"],
            state="readonly",
            width=10,
        )
        tray_style_combo.grid(row=0, column=2, padx=4, pady=8, sticky="w")

        tray_apply_btn = ttk.Button(tray_frame, text="Tray anwenden", command=self._on_tray_settings_change)
        tray_apply_btn.grid(row=0, column=3, padx=8, pady=8, sticky="w")

        ttk.Label(
            tray_frame, text="Schliessen minimiert in den Tray. Beenden erfolgt über das Tray-Menü.", font=("Arial", 9)
        ).grid(row=1, column=0, columnspan=4, sticky="w", padx=8, pady=(0, 8))

        if not self.tray_supported:
            tray_check.state(["disabled"])
            tray_style_combo.state(["disabled"])
            tray_apply_btn.state(["disabled"])
            ttk.Label(
                tray_frame,
                text="Tray ist deaktiviert: AppIndicator (gir1.2-ayatanaappindicator3) und Pillow werden benötigt.",
                font=("Arial", 9),
            ).grid(row=2, column=0, columnspan=4, sticky="w", padx=8, pady=(0, 8))
        elif self.is_root:
            tray_check.state(["disabled"])
            tray_style_combo.state(["disabled"])
            tray_apply_btn.state(["disabled"])
            ttk.Label(
                tray_frame,
                text="Tray ist im root-Modus deaktiviert. Starte die GUI als normaler User.",
                font=("Arial", 9),
            ).grid(row=2, column=0, columnspan=4, sticky="w", padx=8, pady=(0, 8))

    # ============================================================
    # PROFIL-VERWALTUNG
    # ============================================================

    def _init_profiles_from_config(self):
        """Stellt sicher, dass profiles-Liste mindestens ein Profil enthält."""
        profiles = self.config_data.get("profiles", [])
        if not profiles:
            # Ein Profil aus den flachen Config-Keys bauen
            profile = {
                "name": "Standard",
                "type": self.config_data.get("profile_type", "ssh"),
                "storage": self.config_data.get("storage", ""),
                "ssh_key": self.config_data.get("ssh_key", ""),
                "passphrase": self.config_data.get("borg_passphrase", ""),
                "compression": self.config_data.get("compression", "lz4"),
                "include_folders": self.config_data.get("include_folders", ["/"]),
                "exclude_folders": self.config_data.get("exclude_folders", []),
                "schedule_type": self.config_data.get("schedule_type", "manual"),
                "schedule_interval": self.config_data.get("schedule_interval", 3),
                "schedule_time": self.config_data.get("schedule_time", "03:15"),
                "catchup_missed": self.config_data.get("catchup_missed", True),
                "prune_enabled": self.config_data.get("prune_enabled", False),
                "validate_enabled": self.config_data.get("validate_enabled", True),
                "validate_interval": self.config_data.get("validate_interval", 3),
                "optimize_enabled": self.config_data.get("optimize_enabled", False),
                "optimize_interval": self.config_data.get("optimize_interval", 3),
                "s3_access_key": self.config_data.get("s3_access_key", ""),
                "s3_secret_key": self.config_data.get("s3_secret_key", ""),
                "s3_endpoint_url": self.config_data.get("s3_endpoint_url", ""),
                "s3_region": self.config_data.get("s3_region", ""),
                "local_path": self.config_data.get("local_path", ""),
            }
            profiles.append(profile)
            self.config_data["profiles"] = profiles
            self.config_data["profile_name"] = "Standard"
            self.config_data["profile_type"] = "ssh"

    def _refresh_profile_combo(self):
        """Aktualisiert die Profile-Combo im Backup-Tab."""
        if not hasattr(self, "profile_combo"):
            return
        profiles = self.config_data.get("profiles", [])
        names = [p["name"] for p in profiles]
        self.profile_combo["values"] = names
        current = self.config_data.get("profile_name", "")
        if current in names:
            self.profile_combo.set(current)
        elif names:
            self.profile_combo.set(names[0])

    def _apply_profile(self, name):
        """Lädt ein Profil in die aktive Config und UI."""
        profiles = self.config_data.get("profiles", [])
        for p in profiles:
            if p["name"] == name:
                self.config_data["profile_name"] = p["name"]
                self.config_data["profile_type"] = p.get("type", "ssh")
                self.config_data["storage"] = p.get("storage", "")
                self.config_data["ssh_key"] = p.get("ssh_key", "")
                self.config_data["borg_passphrase"] = p.get("passphrase", "")
                self.config_data["compression"] = p.get("compression", "lz4")
                self.config_data["include_folders"] = p.get("include_folders", ["/"])
                self.config_data["exclude_folders"] = p.get("exclude_folders", [])
                if p.get("sources"):
                    self.config_data["sources"] = list(p.get("sources") or [])
                else:
                    self.config_data.pop("sources", None)
                ensure_sources(self.config_data)
                self.config_data["schedule_type"] = p.get("schedule_type", "manual")
                self.config_data["schedule_interval"] = p.get("schedule_interval", 3)
                self.config_data["schedule_time"] = p.get("schedule_time", "03:15")
                self.config_data["catchup_missed"] = p.get("catchup_missed", True)
                self.config_data["prune_enabled"] = p.get("prune_enabled", False)
                self.config_data["validate_enabled"] = p.get("validate_enabled", True)
                self.config_data["validate_interval"] = p.get("validate_interval", 3)
                self.config_data["optimize_enabled"] = p.get("optimize_enabled", False)
                self.config_data["optimize_interval"] = p.get("optimize_interval", 3)
                self.config_data["s3_access_key"] = p.get("s3_access_key", "")
                self.config_data["s3_secret_key"] = p.get("s3_secret_key", "")
                self.config_data["s3_endpoint_url"] = p.get("s3_endpoint_url", "")
                self.config_data["s3_region"] = p.get("s3_region", "")
                self.config_data["local_path"] = p.get("local_path", "")
                self._sync_config_to_ui()
                self._take_flat_snapshot()
                self._refresh_profile_combo()
                self._refresh_destinations_tree()
                return

    def _sync_config_to_ui(self):
        """Überträgt config_data in die UI-Variablen (nach Profilwechsel)."""
        if hasattr(self, "storage_var"):
            self.storage_var.set(self.config_data.get("storage", ""))
        if hasattr(self, "ssh_key_var"):
            self.ssh_key_var.set(self.config_data.get("ssh_key", ""))
        if hasattr(self, "passphrase_var"):
            self.passphrase_var.set(self.config_data.get("borg_passphrase", ""))
        if hasattr(self, "compression_var"):
            self.compression_var.set(self.config_data.get("compression", "lz4"))
        # S3/Local fields
        if hasattr(self, "s3_storage_var"):
            self.s3_storage_var.set(self.config_data.get("storage", ""))
        if hasattr(self, "s3_access_var"):
            self.s3_access_var.set(self.config_data.get("s3_access_key", ""))
        if hasattr(self, "s3_secret_var"):
            self.s3_secret_var.set(self.config_data.get("s3_secret_key", ""))
        if hasattr(self, "s3_endpoint_var"):
            self.s3_endpoint_var.set(self.config_data.get("s3_endpoint_url", ""))
        if hasattr(self, "local_path_var"):
            self.local_path_var.set(self.config_data.get("local_path", ""))
        # Backend-Felder ein-/ausblenden
        self._update_backend_fields()
        if hasattr(self, "schedule_type_var"):
            self.schedule_type_var.set(self.config_data.get("schedule_type", "manual"))
            self.schedule_interval_var.set(str(self.config_data.get("schedule_interval", 3)))
            self.schedule_time_var.set(self.config_data.get("schedule_time", "03:15"))
            self.catchup_var.set(bool(self.config_data.get("catchup_missed", True)))
            self.prune_var.set(bool(self.config_data.get("prune_enabled", False)))
            self.validate_var.set(bool(self.config_data.get("validate_enabled", True)))
            self.validate_interval_var.set(str(self.config_data.get("validate_interval", 3)))
            self.optimize_var.set(bool(self.config_data.get("optimize_enabled", False)))
            self.optimize_interval_var.set(str(self.config_data.get("optimize_interval", 3)))
        # Quellen / Excludes
        ensure_sources(self.config_data)
        if hasattr(self, "sources_tree"):
            self._refresh_sources_tree()
        elif hasattr(self, "include_text"):
            self.include_text.delete("1.0", tk.END)
            inc = self.config_data.get("include_folders", ["/"])
            self.include_text.insert(tk.END, "\n".join(inc))
            if hasattr(self, "exclude_text"):
                self.exclude_text.delete("1.0", tk.END)
                exc = self.config_data.get("exclude_folders", [])
                self.exclude_text.insert(tk.END, "\n".join(exc))
        if hasattr(self, "profile_combo"):
            self._refresh_profile_combo()

    def _update_current_profile_in_list(self):
        """Speichert aktuelle UI-Werte zurück ins aktive Profil."""
        profiles = self.config_data.get("profiles", [])
        name = self.config_data.get("profile_name", "")
        for i, p in enumerate(profiles):
            if p["name"] == name:
                profiles[i]["storage"] = self.config_data.get("storage", "")
                profiles[i]["ssh_key"] = self.config_data.get("ssh_key", "")
                profiles[i]["passphrase"] = self.config_data.get("borg_passphrase", "")
                profiles[i]["compression"] = self.config_data.get("compression", "lz4")
                profiles[i]["include_folders"] = self.config_data.get("include_folders", ["/"])
                profiles[i]["exclude_folders"] = self.config_data.get("exclude_folders", [])
                profiles[i]["sources"] = list(self.config_data.get("sources") or [])
                profiles[i]["schedule_type"] = self.config_data.get("schedule_type", "manual")
                profiles[i]["schedule_interval"] = self.config_data.get("schedule_interval", 3)
                profiles[i]["schedule_time"] = self.config_data.get("schedule_time", "03:15")
                profiles[i]["catchup_missed"] = self.config_data.get("catchup_missed", True)
                profiles[i]["prune_enabled"] = self.config_data.get("prune_enabled", False)
                profiles[i]["validate_enabled"] = self.config_data.get("validate_enabled", True)
                profiles[i]["validate_interval"] = self.config_data.get("validate_interval", 3)
                profiles[i]["optimize_enabled"] = self.config_data.get("optimize_enabled", False)
                profiles[i]["optimize_interval"] = self.config_data.get("optimize_interval", 3)
                profiles[i]["s3_access_key"] = self.config_data.get("s3_access_key", "")
                profiles[i]["s3_secret_key"] = self.config_data.get("s3_secret_key", "")
                profiles[i]["s3_endpoint_url"] = self.config_data.get("s3_endpoint_url", "")
                profiles[i]["s3_region"] = self.config_data.get("s3_region", "")
                profiles[i]["local_path"] = self.config_data.get("local_path", "")
                break

    def _on_profile_selected(self, event=None):
        """Wird aufgerufen, wenn der User ein Profil im Dropdown wählt."""
        name = self.profile_combo.get()
        if name:
            self._apply_profile(name)
            if hasattr(self, "schedule_volume_var"):
                self.schedule_volume_var.set(name)
                self._schedule_volume_prev = name
                job = get_job(self.config_data, name)
                if job:
                    self._schedule_fields_from_job(job)
            self._schedule_next_backup()
            self._refresh_schedule_volume_combo()

    def _show_profile_dialog(self):
        """Dialog zum Erstellen eines neuen Profils."""
        dialog = tk.Toplevel(self.master)
        dialog.title("Neues Profil")
        dialog.geometry("500x500")
        dialog.transient(self.master)
        dialog.grab_set()
        dialog.configure(bg="#1f2937")

        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill="both", expand=True)

        row = 0
        ttk.Label(frame, text="Profil-Name:").grid(row=row, column=0, sticky="e", padx=5, pady=5)
        name_var = tk.StringVar(value="Neues Profil")
        ttk.Entry(frame, textvariable=name_var, width=40).grid(row=row, column=1, padx=5, pady=5)

        row += 1
        ttk.Label(frame, text="Backend-Typ:").grid(row=row, column=0, sticky="e", padx=5, pady=5)
        type_var = tk.StringVar(value="ssh")
        type_combo = ttk.Combobox(
            frame, textvariable=type_var, values=["ssh", "s3", "local"], state="readonly", width=15
        )
        type_combo.grid(row=row, column=1, sticky="w", padx=5, pady=5)

        row += 1
        # Hinweis
        hint_var = tk.StringVar(value="")
        hint_label = ttk.Label(frame, textvariable=hint_var, wraplength=400)
        hint_label.grid(row=row, column=0, columnspan=2, sticky="w", padx=5, pady=5)

        def _on_type_change(*_args):
            t = type_var.get()
            hints = {
                "ssh": "SSH: user@host:./backup (Hetzner Storage Box, beliebiger Server)",
                "s3": "S3: Bucket-Name + Access Keys + Endpoint",
                "local": "Lokal: Pfad zu einem Verzeichnis auf der Festplatte",
            }
            hint_var.set(hints.get(t, ""))

        type_var.trace_add("write", _on_type_change)
        _on_type_change()

        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=row + 1, column=0, columnspan=2, pady=20)
        ttk.Button(btn_frame, text="Abbrechen", command=dialog.destroy).pack(side="left", padx=5)

        def _save():
            profiles = self.config_data.get("profiles", [])
            names = {p["name"] for p in profiles}
            name = name_var.get().strip()
            if not name:
                messagebox.showerror("Fehler", "Bitte einen Namen eingeben.")
                return
            if name in names:
                messagebox.showerror("Fehler", "Dieser Name existiert bereits.")
                return

            acc_id = new_id("acc")
            dest_id = new_id("dest")
            new_profile = {
                "name": name,
                "type": type_var.get(),
                "storage": "",
                "ssh_key": "",
                "passphrase": "",
                "compression": "lz4",
                "include_folders": ["/"],
                "exclude_folders": [],
                "schedule_type": "manual",
                "schedule_interval": 3,
                "schedule_time": "03:15",
                "catchup_missed": True,
                "prune_enabled": False,
                "validate_enabled": True,
                "validate_interval": 3,
                "optimize_enabled": False,
                "optimize_interval": 3,
                "s3_access_key": "",
                "s3_secret_key": "",
                "s3_endpoint_url": "",
                "s3_region": "",
                "local_path": "",
                "destination_ids": [dest_id],
            }
            profiles.append(new_profile)
            self.config_data["profiles"] = profiles
            accounts = list(self.config_data.get("accounts") or [])
            accounts.append(
                {
                    "id": acc_id,
                    "name": name,
                    "type": type_var.get(),
                    "ssh_key": "",
                    "passphrase": "",
                    "s3_access_key": "",
                    "s3_secret_key": "",
                    "s3_endpoint_url": "",
                    "s3_region": "",
                }
            )
            self.config_data["accounts"] = accounts
            destinations = list(self.config_data.get("destinations") or [])
            destinations.append(
                {
                    "id": dest_id,
                    "name": f"{name} Repo",
                    "account_id": acc_id,
                    "storage": "",
                    "local_path": "",
                }
            )
            self.config_data["destinations"] = destinations
            jobs = list(self.config_data.get("jobs") or [])
            jobs.append(
                {
                    "name": name,
                    "include_folders": ["/"],
                    "exclude_folders": [],
                    "compression": "lz4",
                    "schedule_type": "manual",
                    "schedule_interval": 3,
                    "schedule_time": "03:15",
                    "catchup_missed": True,
                    "prune_enabled": False,
                    "validate_enabled": True,
                    "validate_interval": 3,
                    "optimize_enabled": False,
                    "optimize_interval": 3,
                    "destination_ids": [dest_id],
                    "sources": [],
                }
            )
            self.config_data["jobs"] = jobs
            self._apply_profile(name)
            self._save_config()
            self._refresh_schedule_volume_combo()
            dialog.destroy()
            self._show_notice(f'Profil "{name}" erstellt.', level="success")

        ttk.Button(btn_frame, text="Erstellen", command=_save).pack(side="left", padx=5)
        self.master.wait_window(dialog)

    def _show_profile_dialog_edit(self):
        """Editier-Dialog für das aktive Profil."""
        name = self.config_data.get("profile_name", "")
        profiles = self.config_data.get("profiles", [])
        profile = None
        for p in profiles:
            if p["name"] == name:
                profile = p
                break
        if not profile:
            messagebox.showerror("Fehler", "Kein Profil ausgewählt.")
            return

        dialog = tk.Toplevel(self.master)
        dialog.title(f"Profil bearbeiten: {name}")
        dialog.geometry("500x450")
        dialog.transient(self.master)
        dialog.grab_set()
        dialog.configure(bg="#1f2937")

        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill="both", expand=True)

        row = 0
        ttk.Label(frame, text="Profil-Name:").grid(row=row, column=0, sticky="e", padx=5, pady=5)
        name_var = tk.StringVar(value=profile["name"])
        ttk.Entry(frame, textvariable=name_var, width=40).grid(row=row, column=1, padx=5, pady=5)

        row += 1
        ttk.Label(frame, text="Backend-Typ:").grid(row=row, column=0, sticky="e", padx=5, pady=5)
        type_var = tk.StringVar(value=profile.get("type", "ssh"))
        type_combo = ttk.Combobox(
            frame, textvariable=type_var, values=["ssh", "s3", "local"], state="readonly", width=15
        )
        type_combo.grid(row=row, column=1, sticky="w", padx=5, pady=5)

        row += 1
        ttk.Label(frame, text="Repository / Pfad:").grid(row=row, column=0, sticky="e", padx=5, pady=5)
        storage_var = tk.StringVar(value=profile.get("storage", ""))
        ttk.Entry(frame, textvariable=storage_var, width=40).grid(row=row, column=1, padx=5, pady=5)

        row += 1
        ttk.Label(frame, text="SSH-Key:").grid(row=row, column=0, sticky="e", padx=5, pady=5)
        ssh_key_var = tk.StringVar(value=profile.get("ssh_key", ""))
        ttk.Entry(frame, textvariable=ssh_key_var, width=40).grid(row=row, column=1, padx=5, pady=5)

        row += 1
        ttk.Label(frame, text="Passphrase:").grid(row=row, column=0, sticky="e", padx=5, pady=5)
        passphrase_var = tk.StringVar(value=profile.get("passphrase", ""))
        ttk.Entry(frame, textvariable=passphrase_var, width=40, show="*").grid(row=row, column=1, padx=5, pady=5)

        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=row + 1, column=0, columnspan=2, pady=20)
        ttk.Button(btn_frame, text="Abbrechen", command=dialog.destroy).pack(side="left", padx=5)

        def _save():
            new_name = name_var.get().strip()
            if not new_name:
                messagebox.showerror("Fehler", "Bitte einen Namen eingeben.", parent=dialog)
                return
            if new_name != name and get_job(self.config_data, new_name):
                messagebox.showerror("Fehler", "Dieser Name existiert bereits.", parent=dialog)
                return
            self._save_config(push=False)
            job = get_job(self.config_data, name)
            dest, account = self._primary_dest_and_account(job)
            if job is not None:
                job["name"] = new_name
            if account is not None:
                account["type"] = type_var.get()
                account["ssh_key"] = ssh_key_var.get().strip()
                account["passphrase"] = passphrase_var.get()
            if dest is not None:
                dest["storage"] = storage_var.get().strip()
                if type_var.get() == "local":
                    dest["local_path"] = dest["storage"]
            sync_profiles_mirror(self.config_data)
            self._apply_profile(new_name)
            self._save_config()
            self._refresh_schedule_volume_combo()
            self._refresh_archive_profile_combo()
            dialog.destroy()
            self._show_notice(f'Profil "{new_name}" aktualisiert.', level="success")

        ttk.Button(btn_frame, text="Speichern", command=_save).pack(side="left", padx=5)
        self.master.wait_window(dialog)

    def _delete_current_profile(self):
        """Löscht das aktive Profil."""
        profiles = self.config_data.get("profiles", [])
        name = self.config_data.get("profile_name", "")
        if len(profiles) <= 1:
            messagebox.showwarning("Hinweis", "Das letzte Profil kann nicht gelöscht werden.")
            return
        if not messagebox.askyesno("Löschen", f'Profil "{name}" wirklich löschen?'):
            return
        self._save_config(push=False)
        self.config_data["jobs"] = [j for j in self.config_data.get("jobs") or [] if j.get("name") != name]
        used = {str(d) for j in self.config_data["jobs"] for d in j.get("destination_ids") or []}
        self.config_data["destinations"] = [
            d for d in self.config_data.get("destinations") or [] if str(d.get("id")) in used
        ]
        sync_profiles_mirror(self.config_data)
        remaining = self.config_data.get("profiles") or []
        if remaining:
            self._apply_profile(remaining[0]["name"])
        self._save_config()
        self._refresh_schedule_volume_combo()
        self._refresh_archive_profile_combo()
        self._show_notice(f'Profil "{name}" gelöscht.', level="success")

    def _update_backend_fields(self):
        """Zeigt/versteckt SSH-, S3- und Local-Felder je nach Profil-Typ."""
        profile_type = self.config_data.get("profile_type", "ssh")
        if hasattr(self, "ssh_fields_frame"):
            self.ssh_fields_frame.grid_remove()
        if hasattr(self, "s3_fields_frame"):
            self.s3_fields_frame.grid_remove()
        if hasattr(self, "local_fields_frame"):
            self.local_fields_frame.grid_remove()
        if hasattr(self, "backend_hint_var"):
            hints = {
                "ssh": "SSH / Storage Box mit Port 23",
                "s3": "S3 Object Storage (z.B. Hetzner)",
                "local": "Lokales Laufwerk / USB-Festplatte",
            }
            self.backend_hint_var.set(f"Aktiver Typ: {hints.get(profile_type, profile_type)}")
        if profile_type == "ssh" and hasattr(self, "ssh_fields_frame"):
            self.ssh_fields_frame.grid()
        elif profile_type == "s3" and hasattr(self, "s3_fields_frame"):
            self.s3_fields_frame.grid()
        elif profile_type == "local" and hasattr(self, "local_fields_frame"):
            self.local_fields_frame.grid()

    def _refresh_archive_profile_combo(self):
        """Aktualisiert das Profil-Dropdown im Archiv-Tab."""
        if not hasattr(self, "archive_profile_combo"):
            return
        profiles = self.config_data.get("profiles", [])
        names = ["Alle Profile"] + [p["name"] for p in profiles]
        self.archive_profile_combo["values"] = names
        current = self.config_data.get("profile_name", "")
        # 'Alle Profile' nur setzen wenn es mehrere Profile gibt
        if current in names:
            self.archive_profile_combo.set(current)
        elif names:
            self.archive_profile_combo.set(names[0])

    def _on_archive_profile_change(self, event=None):
        """Wechselt das aktive Profil aus dem Archiv-Tab."""
        name = self.archive_profile_combo.get()
        if name == "Alle Profile":
            self._load_all_profiles_archives()
            return
        if name:
            self.config_data["profile_name"] = name
            if name in self.archive_cache:
                self._display_archives_from_cache(name)
            else:
                self._load_archive_list()

    def _load_all_profiles_archives(self):
        """Lädt Archive aller Backup-Volumen über den Dienst und zeigt sie kombiniert an."""
        jobs = [j for j in self.config_data.get("jobs") or [] if j.get("name")]
        if len(jobs) <= 1:
            self._load_archive_list()
            return
        self._set_archive_loading(True, "Lade Archive aller Profile...")
        self.tree.delete(*self.tree.get_children())
        self.archive_loading_token += 1
        token = self.archive_loading_token

        def _worker():
            all_archives = []
            errors = []
            for job in jobs:
                if token != self.archive_loading_token:
                    return
                try:
                    result = self.ipc_slow.call("list_archives", {"job": job["name"]})
                except IpcError as exc:
                    errors.append(f"{job['name']}: {exc.message}")
                    continue
                for arch in result.get("archives") or []:
                    all_archives.append((arch.get("name") or "?", arch.get("time") or "?", job["name"]))

            def _update_ui():
                if token != self.archive_loading_token:
                    return
                for name, time_str, job_name in all_archives:
                    size_hr = self.archive_sizes_cache.get(name, "—")
                    self.tree.insert("", tk.END, text=name, values=(size_hr, time_str, job_name))
                text = f"Archivliste geladen: {len(all_archives)} Archive (alle Profile)"
                if errors:
                    text += f" — {len(errors)} Fehler"
                    self._show_notice("; ".join(errors)[:300], level="warning", timeout_ms=12000)
                self._set_archive_loading(False, text)

            self.master.after(0, _update_ui)

        threading.Thread(target=_worker, daemon=True).start()

    def _display_archives_from_cache(self, profile_name):
        """Zeigt gecachte Archivliste an (ohne erneuten Borg-Zugriff)."""
        cached = self.archive_cache.get(profile_name, [])
        self.tree.delete(*self.tree.get_children())
        for name, size, time_str, ptype in cached:
            real_size = self.archive_sizes_cache.get(name, size)
            self.tree.insert("", tk.END, text=name, values=(real_size, time_str, ptype))
        self._apply_archive_filter()
        self.archive_status_var.set(f"Archivliste geladen: {len(cached)} Archive (Cache)")

    def _refresh_archive_cache(self):
        """Invalidiert den Cache und laedt im Hintergrund (nach Backup)."""
        self.archive_cache = {}
        self.cache_time = None
        self.master.after(2000, self._load_archive_list)

    def _apply_archive_filter(self, event=None):
        """Filtert die Archivliste nach eingegebenem Text."""
        text = self.archive_filter_var.get().lower().strip()
        if not text:
            return
        for item in self.tree.get_children():
            name = self.tree.item(item, "text").lower()
            if text not in name:
                self.tree.detach(item)

    def _clear_archive_filter(self):
        self.archive_filter_var.set("")
        pname = self.config_data.get("profile_name", "Standard")
        if pname in self.archive_cache:
            self._display_archives_from_cache(pname)

    def _toggle_mount(self):
        """Toggelt zwischen Mount und Unmount."""
        if self.mount_active:
            self._unmount_archive()
        else:
            self._mount_archive()

    def _open_mount_in_fm(self):
        """Oeffnet den Mount-Punkt im Dateimanager."""
        if not self.mount_active or not self.mount_point:
            return
        try:
            subprocess.Popen(["xdg-open", self.mount_point], start_new_session=True)
        except Exception:
            messagebox.showerror("Fehler", "Konnte Dateimanager nicht öffnen.")

    def _sync_mount_state(self, mounts: list) -> None:
        mine = [m for m in mounts if int(m.get("uid", -1)) == os.getuid()]
        if mine:
            self.mount_active = True
            self.mount_point = str(mine[-1].get("path") or "")
            self.mounted_archive_name = str(mine[-1].get("archive") or "")
        else:
            self.mount_active = False
            self.mounted_archive_name = ""
        if hasattr(self, "mount_btn"):
            self.mount_btn.config(text="Aushängen" if self.mount_active else "Ausgewähltes Archiv einhängen")
        if hasattr(self, "open_btn"):
            self.open_btn.config(state="normal" if self.mount_active else "disabled")

    def _unmount_archive(self):
        """Hängt das Archiv über den Dienst aus."""
        path = self.mount_point

        def done(_result, error):
            if error is not None:
                self._show_notice(f"Aushängen fehlgeschlagen: {error.message}", level="error")
            else:
                self._show_notice("Archiv ausgehängt.", level="success")
            self._refresh_daemon_status()

        self._daemon_call_async("umount", {"path": path}, on_done=done, slow=True)

    def _build_backup_tab(self):
        tab = self.tab_backup
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=2)
        tab.rowconfigure(1, weight=3)

        top_frame = ttk.Frame(tab)
        top_frame.grid(row=0, column=0, sticky="nsew", padx=10, pady=(10, 5))
        top_frame.columnconfigure(0, weight=1)
        top_frame.columnconfigure(1, weight=1)
        top_frame.rowconfigure(1, weight=1)

        # Profil-Auswahl
        profile_frame = ttk.Frame(top_frame)
        profile_frame.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        profile_frame.columnconfigure(1, weight=1)
        ttk.Label(profile_frame, text="Profil:", font=("Arial", 10, "bold")).grid(row=0, column=0, padx=(0, 5))
        self.profile_combo = ttk.Combobox(profile_frame, state="readonly", width=40)
        self.profile_combo.grid(row=0, column=1, sticky="w")
        self.profile_combo.bind("<<ComboboxSelected>>", self._on_profile_selected)
        ttk.Button(profile_frame, text="Neu", command=self._show_profile_dialog).grid(row=0, column=2, padx=(5, 2))
        ttk.Button(profile_frame, text="Bearbeiten", command=self._show_profile_dialog_edit).grid(
            row=0, column=3, padx=2
        )
        ttk.Button(profile_frame, text="Löschen", command=self._delete_current_profile).grid(
            row=0, column=4, padx=(2, 5)
        )

        left = ttk.Frame(top_frame)
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 10))
        left.columnconfigure(0, weight=1)

        server_frame = ttk.LabelFrame(left, text="Backend-Konfiguration")
        server_frame.grid(row=0, column=0, sticky="ew", pady=5)
        server_frame.columnconfigure(1, weight=1)

        # === SSH-Felder (sichtbar bei Typ ssh) ===
        self.ssh_fields_frame = ttk.Frame(server_frame)
        self.ssh_fields_frame.grid(row=0, column=0, columnspan=3, sticky="ew")
        self.ssh_fields_frame.columnconfigure(1, weight=1)

        ttk.Label(self.ssh_fields_frame, text="Borg Repository URL:").grid(row=0, column=0, sticky="e", padx=5, pady=2)
        self.storage_var = tk.StringVar(value=self.config_data.get("storage", DEFAULT_STORAGE))
        self.storage_entry = ttk.Entry(self.ssh_fields_frame, textvariable=self.storage_var, width=35)
        self.storage_entry.grid(row=0, column=1, sticky="ew", padx=5, pady=2)

        ttk.Label(self.ssh_fields_frame, text="SSH Key (Pfad, optional):").grid(
            row=1, column=0, sticky="e", padx=5, pady=2
        )
        default_ssh_keys = []
        for key in ["id_ed25519", "id_rsa", "id_ecdsa"]:
            key_path = os.path.expanduser(f"~/.ssh/{key}")
            if os.path.exists(key_path):
                default_ssh_keys.append(key_path)
        self.ssh_key_var = tk.StringVar(value=self.config_data.get("ssh_key", ""))
        if not self.ssh_key_var.get() and default_ssh_keys:
            self.ssh_key_var.set(default_ssh_keys[0])
        ssh_entry = ttk.Combobox(
            self.ssh_fields_frame, textvariable=self.ssh_key_var, values=default_ssh_keys, width=32
        )
        ssh_entry.grid(row=1, column=1, sticky="ew", padx=5, pady=2)
        ttk.Button(self.ssh_fields_frame, text="Auswählen", command=self._select_ssh_key).grid(
            row=1, column=2, padx=5, pady=2
        )

        # === S3-Felder (sichtbar bei Typ s3) ===
        self.s3_fields_frame = ttk.Frame(server_frame)
        self.s3_fields_frame.grid(row=1, column=0, columnspan=3, sticky="ew")
        self.s3_fields_frame.columnconfigure(1, weight=1)
        ttk.Label(self.s3_fields_frame, text="Bucket Name:").grid(row=0, column=0, sticky="e", padx=5, pady=2)
        self.s3_storage_var = tk.StringVar(value=self.config_data.get("storage", ""))
        ttk.Entry(self.s3_fields_frame, textvariable=self.s3_storage_var, width=35).grid(
            row=0, column=1, sticky="ew", padx=5, pady=2
        )
        ttk.Label(self.s3_fields_frame, text="S3 Access Key:").grid(row=1, column=0, sticky="e", padx=5, pady=2)
        self.s3_access_var = tk.StringVar(value=self.config_data.get("s3_access_key", ""))
        ttk.Entry(self.s3_fields_frame, textvariable=self.s3_access_var, width=35).grid(
            row=1, column=1, sticky="ew", padx=5, pady=2
        )
        ttk.Label(self.s3_fields_frame, text="S3 Secret Key:").grid(row=2, column=0, sticky="e", padx=5, pady=2)
        self.s3_secret_var = tk.StringVar(value=self.config_data.get("s3_secret_key", ""))
        ttk.Entry(self.s3_fields_frame, textvariable=self.s3_secret_var, width=35, show="*").grid(
            row=2, column=1, sticky="ew", padx=5, pady=2
        )
        ttk.Label(self.s3_fields_frame, text="Endpoint URL:").grid(row=3, column=0, sticky="e", padx=5, pady=2)
        self.s3_endpoint_var = tk.StringVar(
            value=self.config_data.get("s3_endpoint_url", "https://fsn1.your-objectstorage.com")
        )
        ttk.Entry(self.s3_fields_frame, textvariable=self.s3_endpoint_var, width=35).grid(
            row=3, column=1, sticky="ew", padx=5, pady=2
        )

        # === Lokale Felder (sichtbar bei Typ local) ===
        self.local_fields_frame = ttk.Frame(server_frame)
        self.local_fields_frame.grid(row=2, column=0, columnspan=3, sticky="ew")
        self.local_fields_frame.columnconfigure(1, weight=1)
        ttk.Label(self.local_fields_frame, text="Ordner:").grid(row=0, column=0, sticky="e", padx=5, pady=2)
        self.local_path_var = tk.StringVar(value=self.config_data.get("local_path", ""))
        ttk.Entry(self.local_fields_frame, textvariable=self.local_path_var, width=35).grid(
            row=0, column=1, sticky="ew", padx=5, pady=2
        )
        ttk.Button(self.local_fields_frame, text="Wählen", command=self._select_local_path).grid(
            row=0, column=2, padx=5, pady=2
        )

        # === Gemeinsame Felder ===
        common_frame = ttk.Frame(server_frame)
        common_frame.grid(row=3, column=0, columnspan=3, sticky="ew")
        common_frame.columnconfigure(1, weight=1)

        ttk.Label(common_frame, text="Passphrase:").grid(row=0, column=0, sticky="e", padx=5, pady=2)
        self.passphrase_var = tk.StringVar(value=self.config_data.get("borg_passphrase", ""))
        ttk.Entry(common_frame, textvariable=self.passphrase_var, width=35, show="*").grid(
            row=0, column=1, sticky="ew", padx=5, pady=2
        )
        ttk.Label(common_frame, text="(wird nur root-geschützt im Backup-Dienst gespeichert)", font=("Arial", 8)).grid(
            row=1, column=1, sticky="w", padx=5
        )

        ttk.Label(common_frame, text="Kompression:").grid(row=2, column=0, sticky="e", padx=5, pady=2)
        self.compression_var = tk.StringVar(value=self.config_data.get("compression", "lz4"))
        compression_combo = ttk.Combobox(
            common_frame,
            textvariable=self.compression_var,
            values=["none", "lz4", "zstd,3", "zstd,6", "zstd,9"],
            width=15,
            state="readonly",
        )
        compression_combo.grid(row=2, column=1, sticky="w", padx=5, pady=2)

        # Backend-Typ-Hinweis
        self.backend_hint_var = tk.StringVar(value="")
        ttk.Label(common_frame, textvariable=self.backend_hint_var, font=("Arial", 8), foreground="#0f766e").grid(
            row=3, column=1, sticky="w", padx=5
        )

        # Backup-Buttons (links unten unter Config)
        btn_frame = ttk.LabelFrame(left, text="Backup-Aktion")
        btn_frame.grid(row=1, column=0, sticky="ew", pady=(5, 0))
        self.backup_btn = ttk.Button(btn_frame, text="Backup jetzt ausführen", command=self._start_backup)
        self.backup_btn.pack(side="left", padx=5, pady=5)
        self.stop_btn = ttk.Button(btn_frame, text="Abbrechen", command=self._stop_backup)
        self.stop_btn.pack(side="left", padx=5, pady=5)
        self.stop_btn.config(state="disabled")

        right = ttk.Frame(top_frame)
        right.grid(row=1, column=1, sticky="nsew", padx=(10, 0))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=2)
        right.rowconfigure(1, weight=2)
        right.rowconfigure(2, weight=1)

        src_frame = ttk.LabelFrame(right, text="Quellen / Laufwerke")
        src_frame.grid(row=0, column=0, sticky="nsew", pady=(0, 5))
        src_frame.columnconfigure(0, weight=1)
        src_frame.rowconfigure(1, weight=1)
        src_toolbar = ttk.Frame(src_frame)
        src_toolbar.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 2))
        ttk.Button(src_toolbar, text="Laufwerk hinzufügen", command=self._show_disk_picker).pack(
            side="left", padx=(0, 4)
        )
        ttk.Button(src_toolbar, text="Entfernen", command=self._remove_selected_source).pack(side="left", padx=4)
        ttk.Button(src_toolbar, text="Auto-Excludes", command=self._apply_exclude_suggestions_selected).pack(
            side="left", padx=4
        )
        self.sources_tree = ttk.Treeview(
            src_frame,
            columns=("label", "path", "size", "excludes"),
            show="headings",
            height=5,
            selectmode="browse",
        )
        self.sources_tree.heading("label", text="Laufwerk")
        self.sources_tree.heading("path", text="Pfad")
        self.sources_tree.heading("size", text="Größe")
        self.sources_tree.heading("excludes", text="Excludes")
        self.sources_tree.column("label", width=120, stretch=False)
        self.sources_tree.column("path", width=140)
        self.sources_tree.column("size", width=70, stretch=False)
        self.sources_tree.column("excludes", width=70, stretch=False)
        self.sources_tree.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 6))
        self.sources_tree.bind("<<TreeviewSelect>>", self._on_source_selected)
        self._sources_selected_path = ""

        exc_frame = ttk.LabelFrame(right, text="Excludes für ausgewähltes Laufwerk")
        exc_frame.grid(row=1, column=0, sticky="nsew", pady=5)
        exc_frame.columnconfigure(0, weight=1)
        exc_frame.rowconfigure(1, weight=1)
        self.source_exclude_title = tk.StringVar(value="Kein Laufwerk ausgewählt")
        ttk.Label(exc_frame, textvariable=self.source_exclude_title, style="Muted.TLabel").grid(
            row=0, column=0, sticky="w", padx=6, pady=(6, 2)
        )
        self.source_exclude_list = tk.Listbox(
            exc_frame,
            height=6,
            bg=self.ui.get("surface", "#121a2b"),
            fg=self.ui.get("text", "#e6edf7"),
            selectbackground=self.ui.get("accent", "#14b8a6"),
            selectforeground="#042f2e",
            relief="flat",
            borderwidth=0,
            font=self.ui.get("font_mono", ("Courier", 9)),
            highlightthickness=0,
        )
        self.source_exclude_list.grid(row=1, column=0, sticky="nsew", padx=6, pady=4)
        exc_btns = ttk.Frame(exc_frame)
        exc_btns.grid(row=2, column=0, sticky="ew", padx=6, pady=(0, 6))
        ttk.Button(exc_btns, text="Pfad hinzufügen", command=self._add_exclude_to_selected_source).pack(
            side="left", padx=(0, 4)
        )
        ttk.Button(exc_btns, text="Entfernen", command=self._remove_selected_exclude).pack(side="left", padx=4)
        ttk.Button(exc_btns, text="Vorschläge laden", command=self._apply_exclude_suggestions_selected).pack(
            side="left", padx=4
        )

        dest_frame = ttk.LabelFrame(right, text="Backup-Ziele")
        dest_frame.grid(row=2, column=0, sticky="nsew", pady=5)
        dest_frame.columnconfigure(0, weight=1)
        self.dest_tree = ttk.Treeview(dest_frame, columns=("type", "account", "repo"), show="headings", height=3)
        self.dest_tree.heading("type", text="Typ")
        self.dest_tree.heading("account", text="Account")
        self.dest_tree.heading("repo", text="Repository")
        self.dest_tree.column("type", width=70, stretch=False)
        self.dest_tree.column("account", width=120, stretch=False)
        self.dest_tree.column("repo", width=220)
        self.dest_tree.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
        dest_btns = ttk.Frame(dest_frame)
        dest_btns.grid(row=1, column=0, sticky="ew", padx=5, pady=(0, 5))
        ttk.Button(dest_btns, text="Ziel hinzufügen", command=self._show_add_destination_dialog).pack(
            side="left", padx=(0, 4)
        )
        ttk.Button(dest_btns, text="Entfernen", command=self._remove_selected_destination).pack(side="left")
        ttk.Button(dest_btns, text="Repository initialisieren", command=self._init_selected_destination).pack(
            side="left", padx=4
        )
        self.master.after(200, self._refresh_sources_tree)
        self.master.after(250, self._refresh_destinations_tree)

        log_frame = ttk.LabelFrame(tab, text="Live-Log")
        log_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_widget = tk.Text(
            log_frame, state="disabled", bg="#1e1e1e", fg="#00ff00", font=("Courier", 9), wrap=tk.WORD
        )
        self.log_widget.grid(row=0, column=0, sticky="nsew", padx=2, pady=2)
        log_scroll = ttk.Scrollbar(log_frame, command=self.log_widget.yview)
        log_scroll.grid(row=0, column=1, sticky="ns", padx=2, pady=2)
        self.log_widget.configure(yscrollcommand=log_scroll.set)

    def _build_archive_tab(self):
        tab = self.tab_archive
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)

        top = ttk.Frame(tab)
        top.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 5))
        ttk.Label(top, text="Profil:").pack(side="left", padx=(0, 5))
        self.archive_profile_combo = ttk.Combobox(top, state="readonly", width=35)
        self.archive_profile_combo.pack(side="left", padx=(0, 10))
        self.archive_profile_combo.bind("<<ComboboxSelected>>", self._on_archive_profile_change)

        self.load_btn = ttk.Button(top, text="Archivliste laden", command=self._load_archive_list)
        self.load_btn.pack(side="left", padx=2)
        self.archive_status_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.archive_status_var).pack(side="left", padx=8)
        self.archive_progress = ttk.Progressbar(top, mode="indeterminate", length=140)
        self.archive_progress.pack(side="left", padx=5)
        self.archive_progress.pack_forget()

        # Filter
        filter_frame = ttk.Frame(tab)
        filter_frame.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 5))
        ttk.Label(filter_frame, text="Filter:").pack(side="left", padx=(0, 5))
        self.archive_filter_var = tk.StringVar(value="")
        self.archive_filter_entry = ttk.Entry(filter_frame, textvariable=self.archive_filter_var, width=30)
        self.archive_filter_entry.pack(side="left", padx=(0, 5))
        self.archive_filter_entry.bind("<KeyRelease>", self._apply_archive_filter)
        ttk.Button(filter_frame, text="✕", command=self._clear_archive_filter, width=3).pack(side="left")

        self.tree = ttk.Treeview(tab, columns=("size", "time", "type"), show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="Archivname")
        self.tree.heading("size", text="Größe")
        self.tree.heading("time", text="Datum")
        self.tree.heading("type", text="Profil")
        self.tree.column("#0", width=280)
        self.tree.column("size", width=120)
        self.tree.column("time", width=160)
        self.tree.column("type", width=80)
        self.tree.grid(row=2, column=0, sticky="nsew", padx=10, pady=5)

        scrollbar = ttk.Scrollbar(tab, orient=tk.VERTICAL, command=self.tree.yview)
        scrollbar.grid(row=2, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)

        btm = ttk.Frame(tab)
        btm.grid(row=3, column=0, sticky="ew", padx=10, pady=5)
        self.mount_btn = ttk.Button(btm, text="Ausgewähltes Archiv einhängen", command=self._toggle_mount)
        self.mount_btn.pack(side="left", padx=2)
        self.open_btn = ttk.Button(btm, text="In Dateien öffnen", command=self._open_mount_in_fm, state="disabled")
        self.open_btn.pack(side="left", padx=2)
        ttk.Button(btm, text="Wiederherstellen …", command=self._restore_selected_archive).pack(side="left", padx=2)
        ttk.Button(btm, text="Ausgewähltes Archiv löschen", command=self._delete_archive).pack(side="left", padx=2)
        ttk.Label(
            tab,
            text="Einhängen, Wiederherstellen und Löschen erfordern das Administrator-Passwort.",
            style="Muted.TLabel",
        ).grid(row=4, column=0, sticky="w", padx=10, pady=(0, 8))

        # Cache & Mount-State
        self.archive_cache = {}  # profile_name -> list of tuples
        self.cache_time = None
        self.mount_active = False
        self.mount_point = ""
        self.mounted_archive_name = ""

    def _build_restore_tab(self):
        tab = self.tab_restore
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(9, weight=1)

        ttk.Label(tab, text="Wiederherstellung", style="Title.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", padx=12, pady=(12, 2)
        )
        ttk.Label(
            tab,
            text="Dateien oder ganze Verzeichnisse aus einem Archiv zurückholen. Der Dienst stellt mit "
            "root-Rechten wieder her (Besitzer und Rechte bleiben erhalten); dafür ist das "
            "Administrator-Passwort nötig.",
            style="Muted.TLabel",
            wraplength=860,
        ).grid(row=1, column=0, columnspan=3, sticky="w", padx=12, pady=(0, 10))

        ttk.Label(tab, text="Backup-Volumen:").grid(row=2, column=0, sticky="e", padx=8, pady=4)
        self.restore_job_var = tk.StringVar(value=self.config_data.get("profile_name", ""))
        self.restore_job_combo = ttk.Combobox(tab, textvariable=self.restore_job_var, state="readonly", width=40)
        self.restore_job_combo.grid(row=2, column=1, sticky="w", padx=4, pady=4)
        self.restore_job_combo.bind("<<ComboboxSelected>>", lambda _e: self._load_restore_archives())

        ttk.Label(tab, text="Archiv:").grid(row=3, column=0, sticky="e", padx=8, pady=4)
        self.restore_archive_var = tk.StringVar()
        self.restore_archive_combo = ttk.Combobox(tab, textvariable=self.restore_archive_var, width=60)
        self.restore_archive_combo.grid(row=3, column=1, sticky="ew", padx=4, pady=4)
        ttk.Button(tab, text="Archive laden", command=self._load_restore_archives).grid(
            row=3, column=2, sticky="w", padx=8, pady=4
        )

        ttk.Label(tab, text="Nur diese Pfade:").grid(row=4, column=0, sticky="ne", padx=8, pady=4)
        self.restore_paths_text = tk.Text(
            tab,
            height=3,
            bg="#374151",
            fg="#f8fafc",
            insertbackground="#f8fafc",
            relief="flat",
            font=self.ui.get("font_mono", ("Courier", 9)),
        )
        self.restore_paths_text.grid(row=4, column=1, sticky="ew", padx=4, pady=4)
        ttk.Label(
            tab,
            text="Ein Pfad pro Zeile, z. B. /home/max/Dokumente.\nLeer = komplettes Archiv.",
            style="Muted.TLabel",
        ).grid(row=4, column=2, sticky="nw", padx=8, pady=4)

        ttk.Label(tab, text="Zielverzeichnis:").grid(row=5, column=0, sticky="e", padx=8, pady=4)
        self.restore_path_var = tk.StringVar(value=str(Path.home() / "Wiederherstellung"))
        ttk.Entry(tab, textvariable=self.restore_path_var).grid(row=5, column=1, sticky="ew", padx=4, pady=4)
        ttk.Button(tab, text="Ziel wählen", command=self._select_restore_target).grid(
            row=5, column=2, sticky="w", padx=8, pady=4
        )
        self.restore_warning_var = tk.StringVar(value="")
        tk.Label(
            tab, textvariable=self.restore_warning_var, bg=self.ui["bg"], fg="#fbbf24", justify="left", anchor="w"
        ).grid(row=6, column=1, columnspan=2, sticky="w", padx=4)
        self.restore_path_var.trace_add("write", lambda *_a: self._update_restore_warning())

        actions = ttk.Frame(tab)
        actions.grid(row=7, column=1, columnspan=2, sticky="w", padx=4, pady=8)
        self.restore_start_btn = ttk.Button(
            actions, text="Wiederherstellung starten", style="Accent.TButton", command=self._start_restore
        )
        self.restore_start_btn.pack(side="left", padx=(0, 6))
        self.restore_cancel_btn = ttk.Button(actions, text="Abbrechen", command=self._cancel_restore, state="disabled")
        self.restore_cancel_btn.pack(side="left", padx=6)
        ttk.Button(actions, text="Anleitung Komplett-Wiederherstellung", command=self._open_restore_doc).pack(
            side="left", padx=6
        )

        self.restore_progress_var = tk.StringVar(value="")
        progress_row = ttk.Frame(tab)
        progress_row.grid(row=8, column=0, columnspan=3, sticky="ew", padx=12)
        progress_row.columnconfigure(1, weight=1)
        self.restore_progress = ttk.Progressbar(progress_row, mode="determinate", maximum=100, length=260)
        self.restore_progress.grid(row=0, column=0, sticky="w")
        ttk.Label(progress_row, textvariable=self.restore_progress_var, style="Muted.TLabel").grid(
            row=0, column=1, sticky="w", padx=8
        )

        self.restore_log = tk.Text(tab, state="disabled", bg="#1e1e1e", fg="#00ff00", height=10, font=("Courier", 9))
        self.restore_log.grid(row=9, column=0, columnspan=3, sticky="nsew", padx=12, pady=(6, 12))
        self.master.after(400, self._refresh_restore_job_combo)

    def _refresh_restore_job_combo(self) -> None:
        if not hasattr(self, "restore_job_combo"):
            return
        names = [str(j.get("name")) for j in self.config_data.get("jobs") or [] if j.get("name")]
        self.restore_job_combo["values"] = names
        if self.restore_job_var.get() not in names:
            self.restore_job_var.set(names[0] if names else "")

    def _update_restore_warning(self) -> None:
        target = os.path.normpath(self.restore_path_var.get().strip() or ".")
        if target == "/":
            self.restore_warning_var.set(
                "Achtung: Ziel „/“ überschreibt Systemdateien des laufenden Systems. Für eine Komplett-"
                "Wiederherstellung die Anleitung verwenden (Live-System)."
            )
        elif target in ("/etc", "/usr", "/var", "/boot", "/home"):
            self.restore_warning_var.set(f"Achtung: Dateien unter {target} werden überschrieben.")
        else:
            self.restore_warning_var.set("")

    def _load_restore_archives(self) -> None:
        job = self.restore_job_var.get().strip()
        if not job:
            return
        self.restore_progress_var.set("Archive werden geladen …")

        def done(result, error):
            if error is not None:
                self.restore_progress_var.set("")
                self._show_notice(f"Archivliste: {error.message}", level="error")
                return
            names = [str(a.get("name")) for a in (result or {}).get("archives") or [] if a.get("name")]
            names.reverse()
            self.restore_archive_combo["values"] = names
            if names and self.restore_archive_var.get() not in names:
                self.restore_archive_var.set(names[0])
            self.restore_progress_var.set(f"{len(names)} Archive verfügbar")

        self._daemon_call_async("list_archives", {"job": job}, on_done=done, slow=True)

    def _restore_selected_archive(self) -> None:
        selected = self.tree.selection()
        if not selected:
            messagebox.showerror("Fehler", "Kein Archiv ausgewählt.")
            return
        values = self.tree.item(selected[0], "values")
        job = str(values[2]) if len(values) > 2 and get_job(self.config_data, str(values[2])) else ""
        self.restore_job_var.set(job or self.config_data.get("profile_name", ""))
        self.restore_archive_var.set(self.tree.item(selected[0], "text"))
        self.notebook.select(self.tab_restore)

    def _open_restore_doc(self) -> None:
        for path in RESTORE_DOC_PATHS:
            if path.is_file():
                try:
                    subprocess.Popen(["xdg-open", str(path)], start_new_session=True)
                    return
                except OSError:
                    break
        try:
            subprocess.Popen(
                ["xdg-open", "https://github.com/AniGerm/Borg-Backup-GUI/blob/main/docs/RESTORE.md"],
                start_new_session=True,
            )
        except OSError as exc:
            messagebox.showerror("Anleitung", str(exc))

    def _update_restore_progress(self, current) -> None:
        if not hasattr(self, "restore_progress"):
            return
        if not current:
            if self.current_restore_id is None:
                self.restore_progress["value"] = 0
            return
        stats = current.get("stats") or {}
        percent = stats.get("percent")
        if isinstance(percent, (int, float)):
            self.restore_progress["value"] = float(percent)
            self.restore_progress_var.set(f"{percent:.1f} % — {str(stats.get('last_item') or '')[:90]}")
        else:
            self.restore_progress_var.set(current.get("task_label") or "Wiederherstellung läuft …")

    # ============================================================
    # PROTOKOLL-TAB (PDF-Protokoll)
    # ============================================================
    def _build_protocol_tab(self):
        tab = self.tab_protocol
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(6, weight=1)

        row = 0
        ttk.Label(tab, text="DSGVO-konformes Backup-Protokoll", font=("Arial", 14, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", padx=12, pady=(12, 6)
        )

        row += 1
        cfg_frame = ttk.LabelFrame(tab, text="Absender / Mandant")
        cfg_frame.grid(row=row, column=0, columnspan=2, sticky="ew", padx=12, pady=5)
        cfg_frame.columnconfigure(1, weight=1)

        ttk.Label(cfg_frame, text="Firmenname (Auftraggeber):").grid(row=0, column=0, sticky="e", padx=5, pady=4)
        self.protocol_company_var = tk.StringVar(value=self.config_data.get("protocol_company_name", ""))
        ttk.Entry(cfg_frame, textvariable=self.protocol_company_var, width=50).grid(
            row=0, column=1, sticky="ew", padx=5, pady=4
        )

        ttk.Label(cfg_frame, text="Servername (Mandant):").grid(row=1, column=0, sticky="e", padx=5, pady=4)
        self.protocol_server_var = tk.StringVar(value=self.config_data.get("protocol_server_name", os.uname().nodename))
        ttk.Entry(cfg_frame, textvariable=self.protocol_server_var, width=50).grid(
            row=1, column=1, sticky="ew", padx=5, pady=4
        )

        ttk.Label(cfg_frame, text="Infotext (optional):").grid(row=2, column=0, sticky="ne", padx=5, pady=4)
        self.protocol_info_var = tk.StringVar(value=self.config_data.get("protocol_info_text", ""))
        ttk.Entry(cfg_frame, textvariable=self.protocol_info_var, width=50).grid(
            row=2, column=1, sticky="ew", padx=5, pady=4
        )

        row += 1
        btn_frame = ttk.Frame(tab)
        btn_frame.grid(row=row, column=0, columnspan=2, sticky="ew", padx=12, pady=8)

        self.protocol_btn = ttk.Button(
            btn_frame, text="PDF-Protokoll jetzt erstellen", command=self._generate_pdf_report
        )
        self.protocol_btn.pack(side="left", padx=5)

        self.protocol_open_btn = ttk.Button(
            btn_frame, text="Zuletzt erstelltes öffnen", command=self._open_last_protocol, state="disabled"
        )
        self.protocol_open_btn.pack(side="left", padx=5)

        self.protocol_auto_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(btn_frame, text="Auto nach Backup", variable=self.protocol_auto_var).pack(
            side="left", padx=(15, 5)
        )

        self.protocol_status_var = tk.StringVar(value="")
        ttk.Label(btn_frame, textvariable=self.protocol_status_var, font=("Arial", 9)).pack(side="left", padx=10)

        row += 1
        retain = ttk.LabelFrame(tab, text="Aufbewahrung")
        retain.grid(row=row, column=0, columnspan=2, sticky="ew", padx=12, pady=5)
        self.protocol_retention_enabled_var = tk.BooleanVar(
            value=bool(self.config_data.get("protocol_retention_enabled", True))
        )
        ttk.Checkbutton(
            retain, text="Automatisch löschen älter als", variable=self.protocol_retention_enabled_var
        ).pack(side="left", padx=8, pady=6)
        self.protocol_retention_value_var = tk.StringVar(value=str(self.config_data.get("protocol_retention_value", 3)))
        ttk.Spinbox(retain, from_=1, to=100, textvariable=self.protocol_retention_value_var, width=5).pack(
            side="left", padx=4, pady=6
        )
        self.protocol_retention_unit_var = tk.StringVar(
            value=str(self.config_data.get("protocol_retention_unit", "months"))
        )
        ttk.Combobox(
            retain,
            textvariable=self.protocol_retention_unit_var,
            values=["days", "months", "years"],
            width=10,
            state="readonly",
        ).pack(side="left", padx=4, pady=6)
        ttk.Button(retain, text="Anwenden", style="Accent.TButton", command=self._apply_protocol_retention).pack(
            side="left", padx=10, pady=6
        )

        row += 1
        list_frame = ttk.LabelFrame(tab, text="Erstellte Protokolle")
        list_frame.grid(row=row, column=0, columnspan=2, sticky="nsew", padx=12, pady=5)
        list_frame.columnconfigure(0, weight=1)
        self.protocol_tree = ttk.Treeview(list_frame, columns=("date", "name", "size"), show="headings", height=6)
        self.protocol_tree.heading("date", text="Datum")
        self.protocol_tree.heading("name", text="Datei")
        self.protocol_tree.heading("size", text="Größe")
        self.protocol_tree.column("date", width=140, stretch=False)
        self.protocol_tree.column("name", width=320)
        self.protocol_tree.column("size", width=80, stretch=False)
        self.protocol_tree.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
        pdf_btns = ttk.Frame(list_frame)
        pdf_btns.grid(row=1, column=0, sticky="ew", padx=5, pady=(0, 6))
        ttk.Button(pdf_btns, text="Öffnen", command=self._open_selected_protocol_pdf).pack(side="left", padx=(0, 4))
        ttk.Button(pdf_btns, text="Ordner", command=self._reveal_selected_protocol_pdf).pack(side="left", padx=4)
        ttk.Button(pdf_btns, text="Löschen", command=self._delete_selected_protocol_pdf).pack(side="left", padx=4)
        ttk.Button(pdf_btns, text="Aktualisieren", command=self._refresh_protocol_pdf_list).pack(side="left", padx=4)

        row += 1
        preview_frame = ttk.LabelFrame(tab, text="Vorschau / Letzte Meldungen")
        preview_frame.grid(row=row, column=0, columnspan=2, sticky="nsew", padx=12, pady=5)
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)

        self.protocol_preview = tk.Text(
            preview_frame, state="disabled", bg="#1e1e1e", fg="#e2e8f0", font=("Courier", 9), wrap=tk.WORD, height=14
        )
        self.protocol_preview.grid(row=0, column=0, sticky="nsew", padx=3, pady=3)

        scroll = ttk.Scrollbar(preview_frame, command=self.protocol_preview.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.protocol_preview.configure(yscrollcommand=scroll.set)

        self._update_protocol_tab_status()
        self._refresh_protocol_pdf_list()

    def _update_protocol_tab_status(self):
        last_pdf = self.config_data.get("protocol_last_pdf", "")
        if last_pdf and os.path.exists(last_pdf):
            self.protocol_open_btn.config(state="normal")
            self.protocol_status_var.set(f"Letztes PDF: {last_pdf}")
        else:
            self.protocol_open_btn.config(state="disabled")
            self.protocol_status_var.set("Noch kein Protokoll erstellt")

    def _open_last_protocol(self):
        last_pdf = self.config_data.get("protocol_last_pdf", "")
        if last_pdf and os.path.exists(last_pdf):
            import subprocess

            try:
                subprocess.Popen(["xdg-open", last_pdf])
            except Exception:
                messagebox.showinfo("Info", f"PDF gespeichert unter:\n{last_pdf}")

    def _append_protocol_preview(self, text):
        self.protocol_preview.configure(state="normal")
        self.protocol_preview.insert(tk.END, text)
        self._trim_text_widget(self.protocol_preview, max_lines=500, trim_batch=100)
        self.protocol_preview.see(tk.END)
        self.protocol_preview.configure(state="disabled")

    def _generate_pdf_report(self, auto_generated=False):
        """Erstellt ein DSGVO-konformes PDF-Protokoll mit allen Backup-Daten.

        Args:
            auto_generated: True = automatisch nach Backup (keine Popups)
        """
        # Sofort Log in Vorschau schreiben – immer sichtbar
        mode_str = "Auto" if auto_generated else "Manuell"
        self._append_protocol_preview(f"\n[{mode_str}] PDF-Protokoll wird erstellt...\n")

        if FPDF is None:
            msg = "fpdf2 ist nicht verfügbar – die Installation ist unvollständig."
            self._append_protocol_preview(f"[FEHLER] {msg}\n")
            if not auto_generated:
                messagebox.showerror("Fehler", msg)
            return

        company = self.protocol_company_var.get().strip() if hasattr(self, "protocol_company_var") else ""
        server = self.protocol_server_var.get().strip() if hasattr(self, "protocol_server_var") else ""
        info = self.protocol_info_var.get().strip() if hasattr(self, "protocol_info_var") else ""
        hostname = os.uname().nodename

        # Fallback wenn Felder leer sind
        if not company:
            company = f"Unbekannter Auftraggeber ({hostname})"
            if not auto_generated:
                self._append_protocol_preview("[HINWEIS] Firmenname nicht gesetzt – verwende Platzhalter\n")
        if not server:
            server = hostname

        now = datetime.datetime.now()
        timestamp_str = now.strftime("%d.%m.%Y %H:%M:%S")
        pdf_dir = protocol_dir(CONFIG_DIR)
        pdf_path = str(pdf_dir / f"backup-protokoll_{hostname}_{now:%Y%m%d_%H%M%S}.pdf")

        # Daten sammeln
        repo = self._borg_repo()
        status = self.status_data
        canary_result = self.config_data.get("canary_last_result", "")
        canary_check = self.config_data.get("canary_last_check", "")
        canary_expected_hash = self.config_data.get("canary_expected_hash", "")
        canary_verified_hash = self.config_data.get("canary_verified_hash", "")
        last_success = status.get("last_success_at", "")
        last_exit = status.get("last_exit_code", "")
        last_duration = status.get("last_duration_sec", "")
        last_files = status.get("last_files_count", 0)
        last_source = status.get("last_source_size", "")
        last_dedupe = status.get("last_dedupe_size", "")
        last_error = status.get("last_error", "")
        schedule_type = self.config_data.get("schedule_type", "manual")
        compression = self.config_data.get("compression", "lz4")

        try:
            # DejaVuSans als Unicode-Font registrieren (unterstützt Umlaute, Sonderzeichen)
            DEJAVU = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
            DEJAVU_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            pdf = FPDF(orientation="P", unit="mm", format="A4")
            pdf.add_font("DV", "", DEJAVU)
            pdf.add_font("DV", "B", DEJAVU_BOLD)
            # Knappes unteres Margin: sonst rutscht die Fußzeile allein auf eine Leerseite
            pdf.set_auto_page_break(auto=True, margin=12)
            pdf.add_page()

            # === HEADER ===
            pdf.set_font("DV", "B", 18)
            pdf.set_text_color(15, 35, 55)
            pdf.cell(0, 10, "Backup-Protokoll", new_x="LMARGIN", new_y="NEXT", align="C")
            pdf.set_font("DV", "", 9)
            pdf.set_text_color(100, 100, 100)
            pdf.cell(
                0, 6, f"Erstellt am {timestamp_str} | Version {__version__}", new_x="LMARGIN", new_y="NEXT", align="C"
            )
            pdf.ln(6)

            # === Trenner ===
            pdf.set_draw_color(15, 110, 110)
            pdf.set_line_width(0.6)
            pdf.line(10, pdf.get_y(), 200, pdf.get_y())
            pdf.ln(6)

            # === MANDANTEN-DATEN ===
            pdf.set_font("DV", "B", 13)
            pdf.set_text_color(15, 110, 110)
            pdf.cell(0, 8, "1. Auftraggeber / Mandant", new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("DV", "", 10)
            pdf.set_text_color(30, 30, 30)

            data_lines = [
                ("Firmenname (Auftraggeber):", company),
                ("Servername (Mandant):", server),
                ("Rechnername (Hostname):", hostname),
            ]
            if info:
                data_lines.append(("Infotext:", info))

            for label, value in data_lines:
                pdf.set_font("DV", "B", 10)
                pdf.cell(60, 7, label, new_x="END")
                pdf.set_font("DV", "", 10)
                pdf.cell(0, 7, value, new_x="LMARGIN", new_y="NEXT")
            pdf.ln(4)

            # === BACKUP-KONFIGURATION ===
            pdf.set_font("DV", "B", 13)
            pdf.set_text_color(15, 110, 110)
            pdf.cell(0, 8, "2. Backup-Konfiguration", new_x="LMARGIN", new_y="NEXT")
            pdf.set_text_color(30, 30, 30)

            cfg_lines = [
                ("Backup-Ziel (Repository):", repo),
                ("Kompressionsmethode:", compression),
                ("Zeitplan-Typ:", schedule_type),
                ("Ausführung:", f"Systemdienst {SERVICE_NAME} (root, systemd)"),
                ("Verschlüsselung:", self.config_data.get("encryption", "repokey")),
            ]
            for label, value in cfg_lines:
                pdf.set_font("DV", "B", 10)
                pdf.cell(60, 7, label, new_x="END")
                pdf.set_font("DV", "", 10)
                pdf.cell(0, 7, str(value), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(4)

            # === LETZTES BACKUP ===
            pdf.set_font("DV", "B", 13)
            pdf.set_text_color(15, 110, 110)
            pdf.cell(0, 8, "3. Letztes Backup-Ergebnis", new_x="LMARGIN", new_y="NEXT")
            pdf.set_text_color(30, 30, 30)

            if last_success:
                fmt_success = self._format_time(last_success)
                duration_str = f"{int(last_duration)} s" if last_duration else "n/a"
                backup_lines = [
                    ("Ausführungsdatum:", fmt_success),
                    ("Exit-Code:", str(last_exit) if last_exit is not None else "n/a"),
                    ("Dauer:", duration_str),
                    ("Verarbeitete Dateien:", str(last_files)),
                    ("Quellgröße:", last_source or "n/a"),
                    ("Deduplizierte Größe:", last_dedupe or "n/a"),
                ]
                if last_error:
                    backup_lines.append(("Letzter Fehler:", last_error))
            else:
                backup_lines = [("Ausführungsdatum:", "Noch kein Backup ausgeführt")]

            for label, value in backup_lines:
                pdf.set_font("DV", "B", 10)
                pdf.cell(60, 7, label, new_x="END")
                pdf.set_font("DV", "", 10)
                pdf.cell(0, 7, str(value), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(4)

            # === CANARY-CHECK ===
            pdf.set_font("DV", "B", 13)
            pdf.set_text_color(15, 110, 110)
            pdf.cell(0, 8, "4. Canary-Integritätsprüfung (Restore-Test)", new_x="LMARGIN", new_y="NEXT")
            pdf.set_text_color(30, 30, 30)

            if canary_result == "ok":
                canary_status = "BESTANDEN - Integrität des Backups erfolgreich verifiziert"
                canary_icon = "\u2713"  # ✓ CHECK MARK
                pdf.set_text_color(0, 128, 0)
            elif canary_result == "fail":
                canary_status = "FEHLGESCHLAGEN - Canary-Datei nicht gefunden oder Hash-Fehler"
                canary_icon = "\u2718"  # ✘ HEAVY BALLOT X
                pdf.set_text_color(200, 0, 0)
            else:
                canary_status = "NOCH NICHT GEPRÜFT - Bitte zunächst ein Backup ausführen"
                canary_icon = "\u2610"  # ☐ BALLOT BOX
                pdf.set_text_color(180, 150, 0)

            pdf.set_font("DV", "B", 11)
            pdf.cell(0, 7, f"Ergebnis: [{canary_icon}] {canary_status}", new_x="LMARGIN", new_y="NEXT")
            pdf.set_text_color(30, 30, 30)
            pdf.set_font("DV", "", 10)

            canary_lines = [
                ("Canary-Datei:", CANARY_FILE.lstrip("/")),
                ("Letzte Prüfung:", canary_check or "noch nie"),
                ("Prüfverfahren:", "SHA256-Hash via borg extract --stdout"),
            ]
            if canary_expected_hash:
                canary_lines.append(("Erwarteter Hash:", canary_expected_hash))
            if canary_verified_hash:
                canary_lines.append(("Verifizierter Hash:", canary_verified_hash))
            for label, value in canary_lines:
                pdf.set_font("DV", "B", 10)
                pdf.cell(60, 7, label, new_x="END")
                pdf.set_font("DV", "", 10)
                pdf.cell(0, 7, str(value), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(4)

            # === RECHTSVERBINDLICHKEIT / DSGVO ===
            pdf.set_font("DV", "B", 13)
            pdf.set_text_color(15, 110, 110)
            pdf.cell(0, 8, "5. Rechtsverbindlichkeit / DSGVO-Konformität", new_x="LMARGIN", new_y="NEXT")
            pdf.set_text_color(30, 30, 30)

            pdf.set_font("DV", "", 9)
            dsgvo_text = (
                "Dieses Protokoll dient als Nachweis für die ordnungsgemäße Durchführung "
                "von Backups im Sinne der Datenschutz-Grundverordnung (DSGVO), Art. 32 "
                "(Sicherheit der Verarbeitung). Der Canary-Check bestätigt, dass die "
                "gesicherten Daten vollständig und unverändert aus dem Archiv extrahiert "
                "werden können (Restore-Test). Die SHA256-Prüfsumme stellt die "
                "kryptografische Integrität sicher."
            )
            pdf.multi_cell(0, 5, dsgvo_text)
            pdf.ln(3)

            pdf.set_font("DV", "", 9)
            pdf.set_text_color(80, 80, 80)
            pdf.cell(
                0,
                5,
                "Dieses Dokument wurde automatisch erstellt und ist ohne Unterschrift gültig.",
                new_x="LMARGIN",
                new_y="NEXT",
            )
            pdf.cell(
                0,
                5,
                f"Erstellungsdatum: {timestamp_str} | Software: Borg Backup GUI v{__version__}",
                new_x="LMARGIN",
                new_y="NEXT",
            )

            # Fußzeile fest am unteren Rand der aktuellen Seite — kein Auto-Umbruch mehr
            # (sonst entsteht oft eine fast leere zweite Seite nur mit dieser Zeile).
            pdf.set_auto_page_break(auto=False)
            pdf.set_y(-14)
            pdf.set_font("DV", "", 8)
            pdf.set_text_color(140, 140, 140)
            pdf.cell(
                0,
                5,
                f"PDF generiert am {timestamp_str} von Borg Backup GUI v{__version__}",
                align="C",
            )

            # === SPEICHERN ===
            pdf.output(pdf_path)
            self.config_data["protocol_last_pdf"] = pdf_path
            self._collect_config_from_ui()
            self._save_config()

            # Vorschau aktualisieren
            self._append_protocol_preview(f"    PDF gespeichert: {os.path.basename(pdf_path)}\n")
            self._append_protocol_preview(f"    Auftraggeber: {company}\n")
            self._append_protocol_preview(f"    Mandant:      {server}\n")
            self._append_protocol_preview(f"    Canary:       {canary_icon} {canary_status}\n")
            self._append_protocol_preview(f"    Letztes Backup: {self._format_time(last_success)}\n")
            self._append_protocol_preview(f"    Dateien: {last_files} | Quelle {last_source}\n")
            if info:
                self._append_protocol_preview(f"    Infotext: {info}\n")
            self._append_protocol_preview(f"[OK] PDF-Protokoll erstellt ({mode_str})\n")

            self.protocol_status_var.set(f"PDF gespeichert: {pdf_path}")
            self.protocol_open_btn.config(state="normal")
            if not auto_generated:
                self._show_notice("PDF-Protokoll erfolgreich erstellt.", level="success")

        except Exception as exc:
            err_msg = f"PDF-Erstellung fehlgeschlagen: {exc}"
            self._append_protocol_preview(f"[FEHLER] {err_msg}\n")
            if not auto_generated:
                messagebox.showerror("Fehler", err_msg)

    def _append_backup_log(self, text):
        self.log_queue.put(("backup", text))

    def _append_restore_log(self, text):
        self.log_queue.put(("restore", text))

    @staticmethod
    def _is_borg_file_list_line(text: str) -> bool:
        """Erkennt borg --list Dateizeilen (A/M/U/... path), die den RAM sprengen würden."""
        line = text.strip()
        if not line:
            return False
        if line.startswith(("/", "./", "../")):
            return True
        return len(line) > 2 and line[0] in "AMUCdE-" and line[1] == " "

    @staticmethod
    def _trim_text_widget(widget, max_lines=LOG_MAX_LINES, trim_batch=LOG_TRIM_BATCH):
        """Schneidet älteste Zeilen ab, sobald das Log zu groß wird."""
        if widget is None:
            return
        try:
            end_index = widget.index("end-1c")
            line_count = int(float(end_index.split(".")[0]))
        except Exception:
            return
        if line_count <= max_lines:
            return
        drop = max(trim_batch, line_count - max_lines)
        try:
            widget.delete("1.0", f"{drop + 1}.0")
        except Exception:
            pass

    def _should_show_in_backup_log(self, text: str) -> bool:
        """Dateilisten nur stichprobenartig ins Live-Log; Rest läuft über Live-Stats."""
        if not self._is_borg_file_list_line(text):
            return True
        self.backup_list_log_counter += 1
        counter = int(self.backup_list_log_counter)
        return counter == 1 or (counter % LOG_LIST_SAMPLE_EVERY == 0)

    def _process_log_queue(self):
        while True:
            try:
                target, text = self.log_queue.get_nowait()
            except queue.Empty:
                break

            if target == "backup" and self.log_widget:
                self._update_backup_progress(text)
                if self._should_show_in_backup_log(text):
                    self.log_widget.configure(state="normal")
                    if self._is_borg_file_list_line(text):
                        sample = f"[… {self.backup_list_log_counter} Dateien gelistet, zuletzt: {text.strip()[:110]}]\n"
                        self.log_widget.insert(tk.END, sample)
                    else:
                        self.log_widget.insert(tk.END, text)
                    self._trim_text_widget(self.log_widget)
                    self.log_widget.see(tk.END)
                    self.log_widget.configure(state="disabled")
            elif target == "restore" and self.restore_log:
                self.restore_log.configure(state="normal")
                self.restore_log.insert(tk.END, text)
                self._trim_text_widget(self.restore_log)
                self.restore_log.see(tk.END)
                self.restore_log.configure(state="disabled")

        # Fallback-Takt: hält die Tray-Livewerte und Dashboard-Laufzeit im Sekundentakt frisch,
        # auch wenn ein einzelner Timer-Callback verspätet kommt.
        if self.backup_running:
            now = datetime.datetime.now()
            if self.tray_menu_last_refresh_at is None or (now - self.tray_menu_last_refresh_at).total_seconds() >= 1.0:
                self.tray_menu_last_refresh_at = now
                if self.tray_runtime_available and self.config_data.get("tray_enabled", True):
                    self._update_tray_menu_labels()
                self._update_dashboard_status(force=True)
        else:
            self.tray_menu_last_refresh_at = None

        self.master.after(100, self._process_log_queue)

    def _clear_notice(self):
        self.notice_after_id = None
        if hasattr(self, "notice_var"):
            self.notice_var.set("")
        if hasattr(self, "notice_label"):
            self.notice_label.configure(bg="#111827", fg="#cbd5e1")

    def _show_notice(self, text, level="info", timeout_ms=7000):
        palette = {
            "info": ("#0f172a", "#e2e8f0"),
            "success": ("#14532d", "#dcfce7"),
            "warning": ("#78350f", "#fef3c7"),
            "error": ("#7f1d1d", "#fee2e2"),
        }
        bg, fg = palette.get(level, palette["info"])
        self.notice_var.set(text)
        self.notice_label.configure(bg=bg, fg=fg)
        if self.notice_after_id:
            self.master.after_cancel(self.notice_after_id)
            self.notice_after_id = None
        if timeout_ms:
            self.notice_after_id = self.master.after(timeout_ms, self._clear_notice)

    def _reset_backup_progress(self):
        self.backup_files_seen = 0
        self.backup_last_item = ""
        self.backup_list_log_counter = 0
        if hasattr(self, "backup_files_var"):
            self.backup_files_var.set("Gefundene/verarbeitete Dateien: 0")
        if hasattr(self, "backup_source_size_var"):
            self.backup_source_size_var.set("Quellgröße: wird beim Abschluss berechnet")
        if hasattr(self, "backup_dedupe_size_var"):
            self.backup_dedupe_size_var.set("Dedupliziert: wird beim Abschluss berechnet")
        if hasattr(self, "backup_last_item_var"):
            self.backup_last_item_var.set("Zuletzt: arbeite...")
        self.status_data["last_files_count"] = 0
        self.status_data["last_source_size"] = ""
        self.status_data["last_dedupe_size"] = ""
        self.status_data["last_item"] = ""

    def _set_live_stats(self, files_count=None, source_size=None, dedupe_size=None, last_item=None):
        changed = False

        if files_count is not None:
            self.status_data["last_files_count"] = int(files_count)
            changed = True
        if source_size:
            self.status_data["last_source_size"] = source_size
            changed = True
        if dedupe_size:
            self.status_data["last_dedupe_size"] = dedupe_size
            changed = True
        if last_item:
            self.status_data["last_item"] = last_item
            changed = True

        if changed and not self.backup_running:
            self._save_status()

    def _restore_stats_from_status(self):
        if self.backup_running:
            # Während ein Backup läuft, Live-Werte beibehalten
            files_count = self.backup_files_seen or self.status_data.get("last_files_count", 0)
            if hasattr(self, "backup_files_var"):
                self.backup_files_var.set(f"Gefundene/verarbeitete Dateien: {files_count}")
            if hasattr(self, "backup_last_item_var"):
                last_item = self.backup_last_item or self.status_data.get("last_item", "")
                self.backup_last_item_var.set(f"Zuletzt: {last_item[:110]}" if last_item else "Zuletzt: arbeite...")
            if hasattr(self, "backup_source_size_var"):
                src = self.status_data.get("last_source_size", "")
                self.backup_source_size_var.set(
                    f"Quellgröße: {src}" if src else "Quellgröße: wird beim Abschluss berechnet"
                )
            if hasattr(self, "backup_dedupe_size_var"):
                ded = self.status_data.get("last_dedupe_size", "")
                self.backup_dedupe_size_var.set(
                    f"Dedupliziert: {ded}" if ded else "Dedupliziert: wird beim Abschluss berechnet"
                )
            return

        files_count = self.status_data.get("last_files_count", 0)
        source_size = self.status_data.get("last_source_size", "") or "noch keine Daten"
        dedupe_size = self.status_data.get("last_dedupe_size", "") or "noch keine Daten"
        last_item = self.status_data.get("last_item", "") or "noch keine Daten"

        if hasattr(self, "backup_files_var"):
            self.backup_files_var.set(f"Gefundene/verarbeitete Dateien: {files_count}")
        if hasattr(self, "backup_source_size_var"):
            self.backup_source_size_var.set(f"Quellgröße: {source_size}")
        if hasattr(self, "backup_dedupe_size_var"):
            self.backup_dedupe_size_var.set(f"Dedupliziert: {dedupe_size}")
        if hasattr(self, "backup_last_item_var"):
            self.backup_last_item_var.set(f"Zuletzt: {last_item}")

    def _stats_missing_from_status(self):
        if not self.status_data.get("last_success_at"):
            return False

        return any(
            [
                not self.status_data.get("last_files_count"),
                not self.status_data.get("last_source_size"),
                not self.status_data.get("last_dedupe_size"),
                not self.status_data.get("last_item"),
            ]
        )

    def _refresh_dashboard_status(self):
        self._refresh_daemon_status()
        self._update_dashboard_status(force=True)

    def _update_backup_progress(self, text):
        line = text.strip()
        if not line:
            return

        progress_match = re.match(
            r"^([0-9.,]+\s*[KMGTPE]?i?B)\s+O\s+([0-9.,]+\s*[KMGTPE]?i?B)\s+C\s+([0-9.,]+\s*[KMGTPE]?i?B)\s+D\s+(\d+)\s+N\s+(.+)$",
            line,
        )
        if progress_match:
            if hasattr(self, "backup_source_size_var"):
                self.backup_source_size_var.set(f"Quellgröße: {progress_match.group(1)}")
            if hasattr(self, "backup_dedupe_size_var"):
                self.backup_dedupe_size_var.set(f"Dedupliziert: {progress_match.group(3)}")
            if hasattr(self, "backup_files_var"):
                self.backup_files_var.set(f"Gefundene/verarbeitete Dateien: {progress_match.group(4)}")
            if hasattr(self, "backup_last_item_var"):
                self.backup_last_item_var.set(f"Zuletzt: {progress_match.group(5)[:110]}")
            self._set_live_stats(
                files_count=progress_match.group(4),
                source_size=progress_match.group(1),
                dedupe_size=progress_match.group(3),
                last_item=progress_match.group(5)[:110],
            )
            self._update_dashboard_status()
            return

        # Erkenne einzelne Dateien aus borg create --list (A /path, M /path, U /path etc.)
        is_list_line = self._is_borg_file_list_line(line)
        if is_list_line:
            self.backup_files_seen += 1
            clean_item = line[2:].strip() if line[0] in "AMUCdE-" and line[1] == " " else line
            if hasattr(self, "backup_files_var"):
                self.backup_files_var.set(f"Gefundene/verarbeitete Dateien: {self.backup_files_seen}")
            self.backup_last_item = clean_item
            if hasattr(self, "backup_last_item_var"):
                self.backup_last_item_var.set(f"Zuletzt: {clean_item[:110]}")
            self._set_live_stats(files_count=self.backup_files_seen, last_item=clean_item[:110])
            # Pro Datei das ganze Dashboard neu zu zeichnen frisst CPU/RAM – drosseln.
            self._update_dashboard_status()
            return

        match_files = re.search(r"(?:Number of files|Files)\s*:\s*(\d+)", line)
        if match_files and hasattr(self, "backup_files_var"):
            self.backup_files_var.set(f"Gefundene/verarbeitete Dateien: {match_files.group(1)}")
            self._set_live_stats(files_count=match_files.group(1))

        match_source = re.search(r"(?:Original size|This archive)\s*:\s*([0-9.,]+\s*[KMGTPE]?i?B)", line)
        if match_source and hasattr(self, "backup_source_size_var"):
            self.backup_source_size_var.set(f"Quellgröße: {match_source.group(1)}")
            self._set_live_stats(source_size=match_source.group(1))

        match_dedupe = re.search(r"Deduplicated size\s*:\s*([0-9.,]+\s*[KMGTPE]?i?B)", line)
        if match_dedupe and hasattr(self, "backup_dedupe_size_var"):
            self.backup_dedupe_size_var.set(f"Dedupliziert: {match_dedupe.group(1)}")
            self._set_live_stats(dedupe_size=match_dedupe.group(1))

        match_archive_totals = re.search(
            r"This archive:\s+([0-9.,]+\s*[KMGTPE]?i?B)\s+([0-9.,]+\s*[KMGTPE]?i?B)\s+([0-9.,]+\s*[KMGTPE]?i?B)", line
        )
        if match_archive_totals:
            if hasattr(self, "backup_source_size_var"):
                self.backup_source_size_var.set(f"Quellgröße: {match_archive_totals.group(1)}")
            if hasattr(self, "backup_dedupe_size_var"):
                self.backup_dedupe_size_var.set(f"Dedupliziert: {match_archive_totals.group(3)}")
            self._set_live_stats(source_size=match_archive_totals.group(1), dedupe_size=match_archive_totals.group(3))

        self._update_dashboard_status(force=True)

    def _select_ssh_key(self):
        filename = filedialog.askopenfilename(title="SSH Key auswählen", initialdir=os.path.expanduser("~/.ssh"))
        if filename:
            self.ssh_key_var.set(filename)

    def _select_local_path(self):
        path = filedialog.askdirectory(title="Repository-Pfad wählen")
        if path:
            self.local_path_var.set(path)
            self.config_data["local_path"] = path

    def _select_restore_target(self):
        path = filedialog.askdirectory(title="Zielverzeichnis fuer Restore")
        if path:
            self.restore_path_var.set(path)

    def _borg_repo(self):
        """Repository des primären Ziels des aktiven Volumens (nur Anzeige / Protokoll)."""
        dest, account = self._primary_dest_and_account()
        if dest is None or account is None:
            return str(self.config_data.get("storage", ""))
        return resolve_repo(account, dest)

    def _parse_iso_time(self, value):
        if not value:
            return None
        try:
            return datetime.datetime.fromisoformat(value)
        except ValueError:
            return None

    def _format_time(self, value):
        dt = self._parse_iso_time(value)
        if not dt:
            return "-"
        return dt.strftime("%d.%m.%Y %H:%M:%S")

    def _sync_autostart_override(self) -> None:
        """User override for system XDG autostart: disable via Hidden=true when tray is off."""
        tray_enabled = bool(self.config_data.get("tray_enabled", True))
        try:
            if tray_enabled:
                if AUTOSTART_FILE.is_file():
                    AUTOSTART_FILE.unlink()
                return
            AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
            AUTOSTART_FILE.write_text(
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Name=Borg Backup GUI\n"
                "Exec=borg-backup-gui --tray\n"
                "Hidden=true\n"
                "X-GNOME-Autostart-enabled=false\n",
                encoding="utf-8",
            )
        except OSError as exc:
            self._show_notice(f"Autostart-Override fehlgeschlagen: {exc}", level="warning")

    def _on_tray_settings_change(self):
        if not self.tray_supported:
            self._show_notice(
                "Tray nicht verfügbar. Bitte AppIndicator und Pillow (python3-pil) installieren.", level="warning"
            )
            return
        self._save_config()
        self._sync_autostart_override()
        self._setup_tray_icon(force_restart=True)
        self._update_dashboard_status()
        self._show_notice("Tray-Einstellungen übernommen.", level="success")

    def _setup_tray_icon(self, force_restart=False):
        if not self.tray_runtime_available:
            return

        tray_enabled = bool(self.config_data.get("tray_enabled", True))
        if force_restart:
            self._stop_tray_icon()

        if tray_enabled and self.tray_icon is None:
            self._start_tray_icon()
        if not tray_enabled and self.tray_icon is not None:
            self._stop_tray_icon()

    def _start_tray_icon(self):
        if not self.tray_runtime_available or self.tray_icon is not None:
            return

        self.tray_ready = False
        self.tray_icon_signature = None

        if self.native_indicator_supported and not self.native_indicator_failed and self._start_native_indicator():
            self.master.after(2000, self._ensure_tray_ready_or_fallback)
            self._schedule_tray_live_refresh(1000)
            return

        self.tray_ready = False
        self._show_notice(
            "Tray nicht verfügbar (AppIndicator fehlt). Bitte gir1.2-ayatanaappindicator3 installieren.",
            level="warning",
            timeout_ms=10000,
        )

    def _ensure_tray_ready_or_fallback(self):
        if self.tray_ready or not self.tray_runtime_available:
            return

        if self.native_indicator is not None:
            indicator = self.native_indicator
            self.native_indicator = None
            self.native_indicator_failed = True
            try:
                if GLib is not None:

                    def _deactivate():
                        try:
                            indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.PASSIVE)
                        except Exception:
                            pass
                        return False

                    GLib.idle_add(_deactivate)
                else:
                    indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.PASSIVE)
            except Exception:
                pass

        if self.tray_icon is None:
            self._start_tray_icon()

    def _tray_status_lines(self):
        last_success = self.status_data.get("last_success_at", "")
        last_success_text = self._format_time(last_success)
        if last_success_text == "-":
            last_success_text = "noch nie"

        if self.next_backup_dt:
            next_backup_text = self.next_backup_dt.strftime("%d.%m.%Y %H:%M")
        else:
            next_backup_text = "deaktiviert"

        return (f"Zuletzt: {last_success_text}", f"Geplant: {next_backup_text}")

    def _tray_backup_action_label(self):
        return "Backup abbrechen" if self.backup_running else "Backup jetzt starten"

    def _format_runtime(self, seconds):
        if seconds is None:
            return "noch keine Daten"

        total_seconds = max(0, int(seconds))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def _tray_live_summary_lines(self):
        if self.backup_running:
            runtime = None
            if self.current_backup_started_at:
                runtime = (datetime.datetime.now() - self.current_backup_started_at).total_seconds()

            task_label = self.backup_current_task.get("label", "") if self.backup_current_task else ""
            files_count = self.status_data.get("last_files_count", self.backup_files_seen)
            source_size = self.status_data.get("last_source_size", "") or "wird geladen..."
            dedupe_size = self.status_data.get("last_dedupe_size", "") or "wird geladen..."
            last_item = self.status_data.get("last_item", self.backup_last_item) or "wird geladen..."

            return (
                "Live: Backup läuft",
                f"Laufzeit: {self._format_runtime(runtime)}",
                f"Fortschritt: {files_count} Dateien | Quelle {source_size} | Dedupe {dedupe_size}",
                f"Aktuell: {task_label or last_item[:100] or 'arbeitet...'}",
            )

        status_title = self.status_title_var.get() if hasattr(self, "status_title_var") else "Borg Backup GUI"
        last_success = self._format_time(self.status_data.get("last_success_at", ""))
        if last_success == "-":
            last_success = "noch nie"

        next_backup = self.next_backup_dt.strftime("%d.%m.%Y %H:%M") if self.next_backup_dt else "deaktiviert"
        last_error = self.status_data.get("last_error", "").strip() or "keine Meldung"

        return (
            f"Live: {status_title}",
            f"Letzter Lauf: {last_success}",
            f"Geplant: {next_backup}",
            f"Status: {last_error[:110]}",
        )

    def _tray_info_text(self, index):
        return lambda _item: self._tray_live_summary_lines()[index]

    def _tray_backup_action_text(self, _item=None):
        return self._tray_backup_action_label()

    def _build_native_indicator_menu(self):
        summary_lines = self._tray_live_summary_lines()
        menu = Gtk.Menu()

        status_item_1 = Gtk.MenuItem(label=summary_lines[0])
        status_item_1.set_sensitive(False)
        menu.append(status_item_1)

        status_item_2 = Gtk.MenuItem(label=summary_lines[1])
        status_item_2.set_sensitive(False)
        menu.append(status_item_2)

        status_item_3 = Gtk.MenuItem(label=summary_lines[2])
        status_item_3.set_sensitive(False)
        menu.append(status_item_3)

        status_item_4 = Gtk.MenuItem(label=summary_lines[3])
        status_item_4.set_sensitive(False)
        menu.append(status_item_4)

        menu.append(Gtk.SeparatorMenuItem())

        show_item = Gtk.MenuItem(label="Fenster anzeigen")
        show_item.connect("activate", lambda *_: self.master.after(0, self._show_window))
        menu.append(show_item)

        backup_item = Gtk.MenuItem(label=self._tray_backup_action_label())
        backup_item.connect("activate", lambda *_: self.master.after(0, self._tray_backup_toggle))
        menu.append(backup_item)

        menu.append(Gtk.SeparatorMenuItem())

        quit_item = Gtk.MenuItem(label="Beenden")
        quit_item.connect("activate", lambda *_: self.master.after(0, self._quit_app))
        menu.append(quit_item)

        menu.show_all()
        self.tray_native_menu_items = [status_item_1, status_item_2, status_item_3, status_item_4, backup_item]
        return menu

    def _update_tray_menu_labels(self):
        summary_lines = self._tray_live_summary_lines()
        action_label = self._tray_backup_action_label()

        if self.native_indicator is not None and self.tray_native_menu_items and GLib is not None:
            items = list(self.tray_native_menu_items)
            labels = list(summary_lines) + [action_label]

            def _do_labels():
                try:
                    for item, label in zip(items[:4], labels[:4]):
                        item.set_label(label)
                    if len(items) >= 5:
                        items[4].set_label(labels[4])
                except Exception:
                    pass
                return False

            GLib.idle_add(_do_labels)

        self.tray_menu_signature = (*summary_lines, action_label)

    def _schedule_tray_live_refresh(self, delay_ms=1000):
        if self.tray_live_refresh_id is not None:
            try:
                self.master.after_cancel(self.tray_live_refresh_id)
            except Exception:
                pass
            self.tray_live_refresh_id = None

        if self.tray_live_refresh_glib_id is not None and GLib is not None:
            try:
                GLib.source_remove(self.tray_live_refresh_glib_id)
            except Exception:
                pass
            self.tray_live_refresh_glib_id = None

        delay_ms = max(250, int(delay_ms))

        if self.native_indicator is not None and GLib is not None:
            try:
                self.tray_live_refresh_glib_id = GLib.timeout_add(delay_ms, self._periodic_tray_live_refresh)
                return
            except Exception:
                self.tray_live_refresh_glib_id = None

        self.tray_live_refresh_id = self.master.after(delay_ms, self._periodic_tray_live_refresh)

    def _periodic_tray_live_refresh(self):
        self.tray_live_refresh_id = None
        self.tray_live_refresh_glib_id = None

        if not self.tray_runtime_available or not self.config_data.get("tray_enabled", True):
            return False

        self._update_tray_menu_labels()

        if self.backup_running:
            self._schedule_tray_live_refresh(1000)
        else:
            self._schedule_tray_live_refresh(15000)

        return False

    def _start_native_indicator(self):
        if self.native_indicator is not None:
            return True

        try:
            indicator = AyatanaAppIndicator3.Indicator.new(
                "borg_backup_gui", "", AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS
            )
            self.native_indicator = indicator

            if Gtk.main_level() == 0:
                self.native_indicator_thread = threading.Thread(target=Gtk.main, daemon=True)
                self.native_indicator_thread.start()

            initial_color, _ = self._compute_status_traffic_light()
            if GLib is not None:
                menu = self._build_native_indicator_menu()
                title = self.status_title_var.get() if hasattr(self, "status_title_var") else "Borg Backup GUI"
                status_kind = self._tray_icon_status_kind(initial_color)
                rgb = tuple(int(initial_color[i : i + 2], 16) for i in (1, 3, 5))
                startup_image = self._build_tray_icon_image((*rgb, 255), status_kind, self.tray_running_animation_phase)

                def _activate_indicator():
                    try:
                        icon_path = self._tray_icon_file_path()
                        startup_image.save(icon_path, "PNG")
                        self._tray_icon_path = icon_path
                        indicator.set_icon(icon_path)
                        indicator.set_menu(menu)
                        indicator.set_title(title)
                        indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
                        self.tray_ready = True
                    except Exception:
                        self.tray_ready = False
                        self.native_indicator = None
                        self.native_indicator_failed = True
                        self.master.after(0, self._start_tray_icon)
                    self._update_tray_status_icon(initial_color)
                    return False

                GLib.idle_add(_activate_indicator)
            else:
                indicator.set_menu(self._build_native_indicator_menu())
                indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
                self.tray_ready = True
                self._update_tray_status_icon(initial_color)

            return True
        except Exception as exc:
            self.native_indicator = None
            self.tray_ready = False
            self.native_indicator_failed = True
            self._show_notice(
                f"Native AppIndicator konnte nicht gestartet werden: {exc}", level="warning", timeout_ms=10000
            )
            return False

    def _stop_tray_icon(self):
        self.tray_ready = False
        self.native_indicator_failed = False
        self.tray_icon_signature = None
        if self.tray_live_refresh_id is not None:
            try:
                self.master.after_cancel(self.tray_live_refresh_id)
            except Exception:
                pass
            self.tray_live_refresh_id = None
        if self.tray_live_refresh_glib_id is not None and GLib is not None:
            try:
                GLib.source_remove(self.tray_live_refresh_glib_id)
            except Exception:
                pass
            self.tray_live_refresh_glib_id = None
        if self.native_indicator is not None:
            indicator = self.native_indicator
            self.native_indicator = None
            try:
                if GLib is not None:

                    def _deactivate_native():
                        try:
                            indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.PASSIVE)
                        except Exception:
                            pass
                        return False

                    GLib.idle_add(_deactivate_native)
                else:
                    indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.PASSIVE)
            except Exception:
                pass

        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
            self.tray_icon = None

    def _tray_menu_show_window(self, icon, item):
        self.master.event_generate("<<ShowWindow>>", when="tail")

    def _tray_backup_toggle(self):
        if self.backup_running:
            self._stop_backup()
            self._show_notice("Backup wird abgebrochen...", level="warning")
        else:
            self._start_backup()

    def _tray_menu_backup_toggle(self, icon=None, item=None):
        self.master.event_generate("<<TrayBackupToggle>>", when="tail")

    def _tray_menu_start_backup(self, icon, item):
        self.master.event_generate("<<StartBackup>>", when="tail")

    def _tray_menu_quit(self, icon, item):
        self.master.event_generate("<<QuitApp>>", when="tail")

    def _show_window(self):
        self.master.deiconify()
        self.master.lift()
        self.master.focus_force()

    def _ensure_window_visible(self):
        try:
            if str(self.master.state()) == "withdrawn":
                self.master.deiconify()
            self.master.lift()
        except Exception:
            pass

    def _hide_window_to_tray(self):
        self.master.withdraw()
        if not self.tray_hint_shown:
            self.tray_hint_shown = True
            self._show_notice("Die App läuft jetzt im Hintergrund. Öffnen über das Tray-Symbol.", level="info")
        # Sicherstellen, dass das Fenster wieder erscheint, falls der Tray stirbt
        self.master.after(10000, self._ensure_window_visible_if_tray_dead)

    def _ensure_window_visible_if_tray_dead(self):
        if self.master.state() == "withdrawn":
            if not self.tray_ready or (self.tray_icon is None and self.native_indicator is None):
                self._show_window()

    def _quit_app(self):
        try:
            self._save_config()
            self._save_status()
        finally:
            self._cleanup_instance_server()
            self._sync_running_tray_animation()
            self.master.destroy()
            self._stop_tray_icon()

    def _acquire_instance_lock(self) -> bool:
        """Exclusive file lock so a second GUI cannot run if the socket path fails."""
        global INSTANCE_LOCK_HANDLE
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            handle = open(INSTANCE_LOCK_FILE, "a+", encoding="utf-8")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()))
            handle.flush()
            INSTANCE_LOCK_HANDLE = handle
            return True
        except OSError:
            return False

    def _setup_instance_server(self):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            if INSTANCE_SOCKET_FILE.exists():
                try:
                    if _activate_existing_instance():
                        raise SystemExit("Bestehende Instanz aktiviert")
                except SystemExit:
                    raise
                except Exception:
                    pass
                try:
                    INSTANCE_SOCKET_FILE.unlink()
                except OSError:
                    pass

            if not self._acquire_instance_lock():
                # Another process holds the lock — try to focus it, then exit.
                try:
                    _activate_existing_instance()
                except Exception:
                    pass
                raise SystemExit("Andere Instanz läuft bereits")

            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                server.bind(str(INSTANCE_SOCKET_FILE))
            except OSError:
                server.close()
                raise SystemExit("Konnte Instance-Socket nicht binden (andere Instanz?)")
            server.listen(1)
            self.instance_server = server

            def serve():
                while self.instance_server is server:
                    try:
                        conn, _ = server.accept()
                    except OSError:
                        break
                    try:
                        message = conn.recv(64).decode("utf-8", errors="ignore").strip()
                    except OSError:
                        message = ""
                    finally:
                        try:
                            conn.close()
                        except OSError:
                            pass

                    if message == "SHOW":
                        self.master.after(0, self._show_window)

            self.instance_server_thread = threading.Thread(target=serve, daemon=True)
            self.instance_server_thread.start()
        except SystemExit:
            raise
        except OSError as exc:
            raise SystemExit(f"Instance-Server fehlgeschlagen: {exc}") from exc

    def _cleanup_instance_server(self):
        if self.instance_server is not None:
            try:
                self.instance_server.close()
            except OSError:
                pass
            self.instance_server = None

        try:
            if INSTANCE_SOCKET_FILE.exists():
                INSTANCE_SOCKET_FILE.unlink()
        except OSError:
            pass

    def _tray_icon_status_kind(self, color_hex):
        if self.backup_running:
            return "running"
        if color_hex == "#28a745":
            return "ok"
        if color_hex == "#dc3545":
            return "error"
        if color_hex == "#e0a800":
            return "warning"
        return "idle"

    def _build_tray_icon_image(self, status_color, status_kind="idle", animation_phase=0):
        size = 64
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)

        bg = (35, 35, 35, 245)
        draw.rounded_rectangle((4, 4, 60, 60), radius=14, fill=bg)

        if status_kind == "running":
            draw.rounded_rectangle((6, 6, 58, 58), radius=13, outline=status_color, width=4)

        style = self.config_data.get("tray_icon_style", "disk")
        fg = (245, 245, 245, 255)
        muted = (180, 180, 180, 255)

        if style == "shield":
            draw.polygon([(32, 12), (48, 18), (45, 38), (32, 51), (19, 38), (16, 18)], fill=fg)
            draw.polygon([(32, 18), (42, 22), (40, 35), (32, 43), (24, 35), (22, 22)], fill=bg)
        elif style == "palm":
            draw.rectangle((30, 26, 34, 50), fill=muted)
            draw.polygon([(32, 26), (14, 22), (24, 16)], fill=fg)
            draw.polygon([(32, 26), (50, 22), (40, 16)], fill=fg)
            draw.polygon([(32, 24), (18, 10), (30, 14)], fill=fg)
            draw.polygon([(32, 24), (46, 10), (34, 14)], fill=fg)
            draw.polygon([(32, 22), (24, 8), (40, 8)], fill=fg)
        else:
            draw.rounded_rectangle((14, 19, 50, 43), radius=5, fill=fg)
            draw.rectangle((19, 25, 45, 33), fill=bg)
            draw.rectangle((19, 36, 45, 39), fill=muted)

        # Deutlich sichtbare Statusmarkierung: farbiger Unterstrich plus Badge.
        draw.rounded_rectangle((10, 52, 54, 58), radius=3, fill=status_color)

        r = 9
        sx, sy = 49, 15
        draw.ellipse((sx - r - 2, sy - r - 2, sx + r + 2, sy + r + 2), fill=(17, 24, 39, 255))
        draw.ellipse((sx - r, sy - r, sx + r, sy + r), fill=status_color)
        if status_kind == "running":
            if animation_phase % 2 == 0:
                draw.polygon([(28, 12), (28, 26), (40, 19)], fill=(255, 255, 255, 235))
            else:
                draw.rounded_rectangle((26, 12, 31, 26), radius=2, fill=(255, 255, 255, 235))
                draw.rounded_rectangle((34, 12, 39, 26), radius=2, fill=(255, 255, 255, 235))
        return image

    def _set_window_icon(self):
        if not Image or not ImageTk:
            return

        try:
            icon_image = self._build_tray_icon_image((34, 197, 94, 255), "ok")
            self.window_icon_image = ImageTk.PhotoImage(icon_image)
            self.master.iconphoto(True, self.window_icon_image)
        except Exception:
            pass

    def _set_native_indicator_icon_from_image(self, img, description):
        """Saves the image to a unique temp file and calls set_icon() via
        GLib.idle_add – exactly the approach pystray uses internally."""
        if self.native_indicator is None or GLib is None:
            return
        try:
            icon_path = self._tray_icon_file_path()
            img.save(icon_path, "PNG")
            self._tray_icon_path = icon_path

            indicator = self.native_indicator

            def _do_update():
                try:
                    indicator.set_icon(icon_path)
                except Exception:
                    pass
                return False  # run once only

            GLib.idle_add(_do_update)
        except Exception:
            pass

    def _tray_icon_file_path(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        return CONFIG_DIR / "tray-icon.png"

    def _sync_running_tray_animation(self):
        if self.tray_running_animation_id is not None:
            try:
                self.master.after_cancel(self.tray_running_animation_id)
            except Exception:
                pass
            self.tray_running_animation_id = None
        self.tray_running_animation_phase = 0

    def _periodic_tray_health_check(self):
        """Prüft regelmässig, ob das Tray-Icon noch lebt und startet es ggf. neu."""
        # Immer neu planen, unabhängig vom aktuellen Zustand
        self.master.after(30000, self._periodic_tray_health_check)

        if not self.tray_runtime_available:
            return

        tray_enabled = bool(self.config_data.get("tray_enabled", True))
        if not tray_enabled:
            self.tray_ready = False
            return

        # Prüfe pystray Tray
        if self.tray_icon is not None:
            try:
                if hasattr(self.tray_icon, "update_menu"):
                    self.tray_icon.update_menu()
                else:
                    _ = self.tray_icon.icon
            except Exception:
                try:
                    self.tray_icon.stop()
                except Exception:
                    pass
                self.tray_icon = None
                self.tray_ready = False
                self._show_notice("Tray-Symbol wurde neu gestartet.", level="info", timeout_ms=5000)
                self._start_tray_icon()
                self._restore_window_if_hidden()
            return

        # Prüfe nativen Indicator
        if self.native_indicator is not None:
            try:
                if Gtk is not None and Gtk.main_level() == 0:
                    raise RuntimeError("Gtk main loop not running")
            except Exception:
                self.native_indicator = None
                self.native_indicator_failed = True
                self.tray_ready = False
                self._show_notice("Tray-Symbol wurde neu gestartet.", level="info", timeout_ms=5000)
                self._start_tray_icon()
                self._restore_window_if_hidden()
            return

        # Kein Tray-Icon, aber Tray sollte aktiv sein → neu starten
        self.tray_ready = False
        self._start_tray_icon()
        self._restore_window_if_hidden()

    def _restore_window_if_hidden(self):
        try:
            if str(self.master.state()) == "withdrawn":
                self._show_window()
        except Exception:
            pass

    def _tick_running_tray_animation(self):
        self.tray_running_animation_id = None
        self.tray_running_animation_phase = 0

    def _update_tray_status_icon(self, color_hex, animation_phase=None):
        status_kind = self._tray_icon_status_kind(color_hex)
        if animation_phase is None:
            animation_phase = self.tray_running_animation_phase if status_kind == "running" else 0
        title = self.status_title_var.get() if hasattr(self, "status_title_var") else "Borg Backup GUI"
        icon_signature = (
            bool(self.native_indicator),
            color_hex,
            status_kind,
            animation_phase,
            self.config_data.get("tray_icon_style", "disk"),
            title,
        )

        if icon_signature == self.tray_icon_signature:
            return

        if self.native_indicator is not None:
            try:
                rgb = tuple(int(color_hex[i : i + 2], 16) for i in (1, 3, 5))
                img = self._build_tray_icon_image((*rgb, 255), status_kind, animation_phase)
                self._set_native_indicator_icon_from_image(img, title)
            except Exception:
                pass
            self.tray_icon_signature = icon_signature
            return

        if self.tray_icon is None or not self.tray_supported:
            return

        try:
            rgb = tuple(int(color_hex[i : i + 2], 16) for i in (1, 3, 5))
            self.tray_icon.icon = self._build_tray_icon_image((*rgb, 255), status_kind, animation_phase)
            self.tray_icon.title = title
        except Exception:
            pass
        self.tray_icon_signature = icon_signature

    def _compute_status_traffic_light(self):
        now = datetime.datetime.now()
        last_success = self._parse_iso_time(self.status_data.get("last_success_at", ""))
        exit_code = self.status_data.get("last_exit_code", None)

        color = "#6e6e6e"
        title = "Noch kein Backup ausgeführt"

        if self.backup_running:
            color = "#2d7ff9"
            title = "Backup läuft gerade"
        elif exit_code == 0 and last_success:
            age_hours = (now - last_success).total_seconds() / 3600.0
            if age_hours <= 24:
                color = "#28a745"
                title = "Backup aktuell und erfolgreich"
            elif age_hours <= 48:
                color = "#e0a800"
                title = "Backup erfolgreich, aber nicht mehr tagesaktuell"
            else:
                color = "#dc3545"
                title = "Letztes erfolgreiches Backup ist zu alt"
        elif exit_code == 1 and last_success:
            age_hours = (now - last_success).total_seconds() / 3600.0
            if age_hours <= 24:
                color = "#e0a800"
                title = "Backup mit Warnungen abgeschlossen"
            elif age_hours <= 48:
                color = "#e0a800"
                title = "Backup mit Warnungen, aber nicht mehr tagesaktuell"
            else:
                color = "#dc3545"
                title = "Letztes Backup hatte Warnungen und ist zu alt"
        elif exit_code is not None:
            color = "#dc3545"
            title = "Letztes Backup fehlgeschlagen"

        return color, title

    def _job_last_success(self, job_name: str) -> datetime.datetime | None:
        jobs_status = self.status_data.get("jobs") or {}
        if isinstance(jobs_status, dict):
            entry = jobs_status.get(job_name) or {}
            if isinstance(entry, dict) and entry.get("last_success_at"):
                parsed = self._parse_iso_time(str(entry.get("last_success_at")))
                if parsed:
                    return parsed
        # Legacy fallback for the active/default volume only
        if job_name == self.config_data.get("profile_name"):
            return self._parse_iso_time(self.status_data.get("last_success_at", ""))
        return None

    def _set_job_last_success(self, job_name: str, when: datetime.datetime) -> None:
        if not job_name:
            return
        jobs_status = self.status_data.setdefault("jobs", {})
        if not isinstance(jobs_status, dict):
            jobs_status = {}
            self.status_data["jobs"] = jobs_status
        entry = jobs_status.setdefault(job_name, {})
        if not isinstance(entry, dict):
            entry = {}
            jobs_status[job_name] = entry
        entry["last_success_at"] = when.isoformat(timespec="seconds")

    def _schedule_fields_from_job(self, job: dict) -> None:
        self._schedule_volume_loading = True
        try:
            if hasattr(self, "schedule_type_var"):
                self.schedule_type_var.set(str(job.get("schedule_type", "manual")))
                self.schedule_interval_var.set(str(int(job.get("schedule_interval", 3) or 3)))
                self.schedule_time_var.set(str(job.get("schedule_time", "03:15")))
                self.catchup_var.set(bool(job.get("catchup_missed", True)))
                self.prune_var.set(bool(job.get("prune_enabled", False)))
                self.validate_var.set(bool(job.get("validate_enabled", True)))
                self.validate_interval_var.set(str(int(job.get("validate_interval", 3) or 3)))
                self.optimize_var.set(bool(job.get("optimize_enabled", False)))
                self.optimize_interval_var.set(str(int(job.get("optimize_interval", 3) or 3)))
        finally:
            self._schedule_volume_loading = False

    def _sync_schedule_volume_from_ui(self) -> None:
        if not hasattr(self, "schedule_type_var") or not hasattr(self, "schedule_volume_var"):
            return
        name = (self.schedule_volume_var.get() or "").strip()
        if not name:
            return
        job = get_job(self.config_data, name)
        if job is None:
            for profile in self.config_data.get("profiles") or []:
                if isinstance(profile, dict) and profile.get("name") == name:
                    job = profile
                    break
        if job is None:
            return
        try:
            interval = int(self.schedule_interval_var.get() or 3)
        except ValueError:
            interval = 3
        try:
            validate_interval = int(self.validate_interval_var.get() or 3)
        except ValueError:
            validate_interval = 3
        try:
            optimize_interval = int(self.optimize_interval_var.get() or 3)
        except ValueError:
            optimize_interval = 3
        job["schedule_type"] = self.schedule_type_var.get()
        job["schedule_interval"] = interval
        job["schedule_time"] = self.schedule_time_var.get().strip() or "03:15"
        job["catchup_missed"] = bool(self.catchup_var.get())
        job["prune_enabled"] = bool(self.prune_var.get())
        job["validate_enabled"] = bool(self.validate_var.get())
        job["validate_interval"] = validate_interval
        job["optimize_enabled"] = bool(self.optimize_var.get())
        job["optimize_interval"] = optimize_interval
        if name == self.config_data.get("profile_name"):
            self.config_data["schedule_type"] = job["schedule_type"]
            self.config_data["schedule_interval"] = job["schedule_interval"]
            self.config_data["schedule_time"] = job["schedule_time"]
            self.config_data["catchup_missed"] = job["catchup_missed"]
            self.config_data["prune_enabled"] = job["prune_enabled"]
            self.config_data["validate_enabled"] = job["validate_enabled"]
            self.config_data["validate_interval"] = job["validate_interval"]
            self.config_data["optimize_enabled"] = job["optimize_enabled"]
            self.config_data["optimize_interval"] = job["optimize_interval"]

    def _refresh_schedule_volume_combo(self) -> None:
        if not hasattr(self, "schedule_volume_combo"):
            return
        volumes = list_backup_volumes(self.config_data)
        names = [str(v.get("name")) for v in volumes]
        self.schedule_volume_combo["values"] = names
        current = (self.schedule_volume_var.get() if hasattr(self, "schedule_volume_var") else "") or ""
        active = self.config_data.get("profile_name", "")
        if current not in names:
            current = active if active in names else (names[0] if names else "")
        if current:
            self.schedule_volume_var.set(current)
            job = get_job(self.config_data, current) or next(
                (v for v in volumes if v.get("name") == current),
                None,
            )
            if job:
                self._schedule_fields_from_job(job)
        self._refresh_schedule_overview()

    def _on_schedule_volume_selected(self, _event=None) -> None:
        if self._schedule_volume_loading:
            return
        previous = getattr(self, "_schedule_volume_prev", None)
        selected = (self.schedule_volume_var.get() or "").strip()
        if previous and previous != selected:
            # Keep previous selection long enough to flush UI into that job
            self.schedule_volume_var.set(previous)
            self._sync_schedule_volume_from_ui()
            self.schedule_volume_var.set(selected)
        self._schedule_volume_prev = selected
        job = get_job(self.config_data, selected)
        if job is None:
            for profile in self.config_data.get("profiles") or []:
                if isinstance(profile, dict) and profile.get("name") == selected:
                    job = profile
                    break
        if job:
            self._schedule_fields_from_job(job)
        self._refresh_schedule_overview()

    def _on_schedule_fields_changed(self) -> None:
        if self._schedule_volume_loading:
            return
        self._refresh_schedule_overview(include_unsaved=True)

    def _collect_volume_next_runs(self, *, include_unsaved: bool = False) -> list[tuple[str, datetime.datetime]]:
        now = datetime.datetime.now()
        next_runs: list[tuple[str, datetime.datetime]] = []
        selected = (self.schedule_volume_var.get() if hasattr(self, "schedule_volume_var") else "") or ""
        for volume in list_backup_volumes(self.config_data):
            name = str(volume.get("name") or "")
            if not name:
                continue
            schedule_type = str(volume.get("schedule_type", "manual"))
            schedule_interval = int(volume.get("schedule_interval", 3) or 3)
            schedule_time = str(volume.get("schedule_time", "03:15"))
            catchup = bool(volume.get("catchup_missed", True))
            if include_unsaved and name == selected and hasattr(self, "schedule_type_var"):
                schedule_type = self.schedule_type_var.get()
                try:
                    schedule_interval = int(self.schedule_interval_var.get() or 3)
                except ValueError:
                    schedule_interval = 3
                schedule_time = self.schedule_time_var.get().strip() or "03:15"
                catchup = bool(self.catchup_var.get())
            nxt = compute_next_run(
                schedule_type=schedule_type,
                schedule_interval=schedule_interval,
                schedule_time=schedule_time,
                catchup_missed=catchup,
                last_success=self._job_last_success(name),
                now=now,
                backup_running=bool(self.backup_running),
            )
            if nxt is not None:
                next_runs.append((name, nxt))
        return next_runs

    def _refresh_schedule_overview(self, *, include_unsaved: bool = False) -> None:
        if not hasattr(self, "schedule_overview_var"):
            return
        lines: list[str] = []
        next_runs = {name: dt for name, dt in self._collect_volume_next_runs(include_unsaved=include_unsaved)}
        selected = (self.schedule_volume_var.get() if hasattr(self, "schedule_volume_var") else "") or ""
        for volume in list_backup_volumes(self.config_data):
            name = str(volume.get("name") or "")
            display = dict(volume)
            if include_unsaved and name == selected and hasattr(self, "schedule_type_var"):
                display["schedule_type"] = self.schedule_type_var.get()
                display["schedule_interval"] = self.schedule_interval_var.get()
                display["schedule_time"] = self.schedule_time_var.get()
            summary = format_schedule_summary(display)
            nxt = next_runs.get(name)
            nxt_text = nxt.strftime("%d.%m.%Y %H:%M") if nxt else "—"
            marker = " ●" if name == selected else ""
            lines.append(f"{name}{marker}: {summary}  →  nächstes {nxt_text}")
        self.schedule_overview_var.set("\n".join(lines) if lines else "Keine Backup-Volumen konfiguriert.")

        overlaps = find_schedule_overlaps(list(next_runs.items()), window_minutes=60)
        if overlaps:
            parts = [
                f"„{a}“ und „{b}“ starten innerhalb von {delta} Min."
                if delta > 0
                else f"„{a}“ und „{b}“ starten zur gleichen Zeit."
                for a, b, delta in overlaps[:3]
            ]
            self.schedule_overlap_var.set(
                "Warnung: Überschneidende Zeitpläne können die Performance stark beeinträchtigen.\n"
                + " ".join(parts)
                + (" …" if len(overlaps) > 3 else "")
            )
        else:
            self.schedule_overlap_var.set("")

    def _on_schedule_change(self):
        self._sync_schedule_volume_from_ui()
        self._save_config()
        self._schedule_next_backup()
        self._refresh_schedule_overview()
        overlaps = find_schedule_overlaps(self._collect_volume_next_runs(), window_minutes=60)
        if overlaps:
            self._show_notice(
                "Zeitplan gespeichert — Achtung: Überschneidungen können die Performance beeinträchtigen.",
                level="warning",
                timeout_ms=10000,
            )
        else:
            self._show_notice("Zeitplan und Wartungseinstellungen gespeichert.", level="success")

    def _schedule_next_backup(self):
        """Zeitpläne gehören dem Systemdienst; die GUI zeigt nur den Status an."""
        if hasattr(self, "scheduled_job_id") and self.scheduled_job_id:
            try:
                self.master.after_cancel(self.scheduled_job_id)
            except Exception:
                pass
            self.scheduled_job_id = None
        self._refresh_schedule_overview()
        if self.daemon_online:
            self._refresh_daemon_status()

    def _scheduled_backup_trigger(self):
        # Kompatibilität: alte Timer-Callbacks werden verworfen.
        self.scheduled_job_id = None

    def _periodic_dashboard_refresh(self):
        self._update_dashboard_status()
        self.master.after(60000, self._periodic_dashboard_refresh)

    def _update_dashboard_status(self, force=False):
        now_mono = time.monotonic()
        if not force and (now_mono - self._dashboard_last_update_at) * 1000 < DASHBOARD_THROTTLE_MS:
            return
        self._dashboard_last_update_at = now_mono

        last_run = self._parse_iso_time(self.status_data.get("last_run_at", ""))
        last_error = self.status_data.get("last_error", "")

        color, title = self._compute_status_traffic_light()

        self.status_canvas.itemconfig(self.status_light, fill=color)
        self.status_title_var.set(title)
        self._update_tray_status_icon(color)
        self._sync_running_tray_animation()
        self._update_tray_menu_labels()
        if self.backup_running and self.current_backup_started_at:
            runtime = int((datetime.datetime.now() - self.current_backup_started_at).total_seconds())
            self.last_backup_var.set(f"Letzter Lauf: läuft seit {runtime // 60} min {runtime % 60} s")
        else:
            formatted_last_run = self._format_time(self.status_data.get("last_run_at", ""))
            if formatted_last_run == "-":
                self.last_backup_var.set("Letzter Lauf: noch nie")
            else:
                self.last_backup_var.set(f"Letzter Lauf: {formatted_last_run}")

        if self.next_backup_dt:
            job_label = self.next_scheduled_job_name or ""
            suffix = f" ({job_label})" if job_label else ""
            self.next_backup_var.set(f"Nächstes geplantes Backup{suffix}: {self.next_backup_dt:%d.%m.%Y %H:%M}")
        else:
            self.next_backup_var.set("Nächstes geplantes Backup: deaktiviert")

        if self.backup_running:
            files_count = self.status_data.get("last_files_count", 0)
            source_size = self.status_data.get("last_source_size", "")
            dedupe_size = self.status_data.get("last_dedupe_size", "")
            progress_parts = [f"{files_count} Dateien"] if files_count else []
            if source_size:
                progress_parts.append(f"Quelle {source_size}")
            if dedupe_size:
                progress_parts.append(f"Dedupe {dedupe_size}")
            detail = " | ".join(progress_parts) if progress_parts else "arbeite..."
            self.last_error_var.set(f"Letzte Meldung: Backup läuft... {detail}")
        elif last_error:
            self.last_error_var.set(f"Letzte Meldung: {last_error}")
        elif last_run:
            duration = self.status_data.get("last_duration_sec", None)
            if isinstance(duration, (int, float)):
                self.last_error_var.set(f"Letzte Meldung: Erfolgreich ({int(duration)} s)")
            else:
                self.last_error_var.set("Letzte Meldung: Erfolgreich")
        else:
            self.last_error_var.set("Letzte Meldung: noch keine")

        # Canary-Status aktualisieren
        if hasattr(self, "canary_status_var"):
            canary_result = self.config_data.get("canary_last_result", "")
            canary_check = self.config_data.get("canary_last_check", "")
            if canary_result == "ok":
                self.canary_status_var.set(f"✅ Canary: OK ({canary_check[:16]})")
            elif canary_result == "fail":
                self.canary_status_var.set(f"❌ Canary: FEHLER ({canary_check[:16]})")
            else:
                self.canary_status_var.set("⏳ Canary: noch kein Check")

        self._restore_stats_from_status()

    def _start_backup(self, from_scheduler=False, job_name: str | None = None):
        # Zeitpläne führt ausschließlich der Systemdienst aus.
        if from_scheduler:
            return
        if self.backup_running or self.daemon_busy:
            self._show_notice("Es läuft bereits ein Auftrag im Backup-Dienst.", level="info")
            return
        if not self.daemon_online:
            self._show_notice(
                f"Backup-Dienst {SERVICE_NAME} nicht verbunden — bitte starten.",
                level="warning",
                timeout_ms=10000,
            )
            return

        target_job = (job_name or self.config_data.get("profile_name") or "Standard").strip()
        if job_name and job_name != self.config_data.get("profile_name"):
            self._apply_profile(job_name)
        if not get_job(self.config_data, target_job):
            messagebox.showerror("Backup", f"Backup-Volumen „{target_job}“ nicht gefunden.")
            return

        self.archive_loading_token += 1
        self._save_config(push=False)
        self.notebook.select(self.tab_backup)
        self._reset_backup_progress()
        self._append_backup_log(f"\n>>> Übergebe Backup „{target_job}“ an {SERVICE_NAME}…\n")

        def after_push(ok: bool, message: str) -> None:
            if not ok:
                self._show_notice(
                    f"Konfiguration nicht an den Dienst übertragen: {message or 'abgebrochen'}",
                    level="error",
                    timeout_ms=12000,
                )
                return

            def done(result, error) -> None:
                if error is not None:
                    messagebox.showerror("Backup", error.message)
                    return
                if isinstance(result, dict):
                    self.current_run_id = str(result.get("id") or "")
                self.current_backup_job_name = target_job
                self._append_backup_log(f"[OK] Auftrag gestartet (ID {self.current_run_id}).\n")
                self._refresh_daemon_status()

            self._daemon_call_async("run_job", {"job": target_job}, on_done=done)

        self._push_config_if_changed(on_done=after_push, force=True)

    def _stop_backup(self) -> None:
        current = self.daemon_status.get("current") or {}
        run_id = str(current.get("id") or getattr(self, "current_run_id", "") or "")
        if not run_id:
            self._show_notice("Kein laufender Auftrag zum Abbrechen.", level="info")
            return

        def done(result, error) -> None:
            if error is not None:
                self._show_notice(f"Abbruch fehlgeschlagen: {error.message}", level="error")
                return
            self._append_backup_log("\n[INFO] Abbruch an den Backup-Dienst gesendet.\n")
            self._refresh_daemon_status()

        self._daemon_call_async("cancel", {"id": run_id}, on_done=done)

    def _load_archive_list(self):
        if not self.daemon_online:
            self._set_archive_loading(False, "Backup-Dienst nicht verbunden")
            self._show_notice("Archivliste: Backup-Dienst nicht verbunden.", level="warning")
            return
        if self.backup_running:
            self.archive_list_refresh_pending = True
            self._show_notice("Archivliste wird nach dem laufenden Backup neu geladen.", level="info")
            return

        job_name = self.config_data.get("profile_name", "Standard")
        self.archive_loading_token += 1
        token = self.archive_loading_token
        self._set_archive_loading(True, "Archivliste wird geladen...")
        self.tree.delete(*self.tree.get_children())
        self.archive_cache.pop(job_name, None)
        self.cache_time = None

        def done(result, error):
            if token != self.archive_loading_token:
                return
            if error is not None:
                self._set_archive_loading(False, "Fehler beim Laden")
                self._show_notice(f"Archivliste: {error.message}", level="error")
                return
            archives = list((result or {}).get("archives") or [])
            profile_type = self.config_data.get("profile_type", "ssh")
            cache_list = []
            for archive in archives:
                name = str(archive.get("name") or "?")
                time_str = str(archive.get("time") or "?")
                size_hr = self.archive_sizes_cache.get(name, "—")
                self.tree.insert("", tk.END, text=name, values=(size_hr, time_str, profile_type))
                cache_list.append((name, size_hr, time_str, profile_type))
            self.archive_cache[job_name] = cache_list
            self.cache_time = datetime.datetime.now()
            self._apply_archive_filter()
            self._set_archive_loading(False, f"Archivliste geladen: {len(cache_list)} Archive")

        self._daemon_call_async("list_archives", {"job": job_name}, on_done=done, slow=True)

    def _set_archive_loading(self, is_loading, text=""):
        if hasattr(self, "archive_status_var"):
            self.archive_status_var.set(text)
        if hasattr(self, "archive_progress"):
            if is_loading:
                if not self.archive_progress.winfo_manager():
                    self.archive_progress.pack(side="left", padx=5)
                self.archive_progress.start(10)
            else:
                self.archive_progress.stop()
                if self.archive_progress.winfo_manager():
                    self.archive_progress.pack_forget()

    def _populate_archive_sizes_async(self, rows, token, profile_name=None):
        import time as _time

        # Nur Zeilen abfragen, deren Größe noch nicht bekannt ist
        missing_rows = [
            (iid, name, ptype)
            for (iid, name, size, ptype) in rows
            if name not in self.archive_sizes_cache or self.archive_sizes_cache[name] == "wird geladen..."
        ]
        total = len(missing_rows)
        if total == 0:
            self.master.after(0, lambda: self._set_archive_loading(False, f"Archivliste geladen: {len(rows)} Archive"))
            return

        for index, (item_id, archive_name, _ptype) in enumerate(missing_rows, start=1):
            if token != self.archive_loading_token or self.backup_running:
                return
            size_label = self._query_archive_size_label(archive_name)
            if token != self.archive_loading_token or self.backup_running:
                return
            if size_label and size_label != "n/a":
                self.archive_sizes_cache[archive_name] = size_label
                self._save_archive_sizes_cache()
            self.master.after(
                0,
                lambda iid=item_id, size=size_label, idx=index, count=total, current_token=token, pn=profile_name: (
                    self._update_archive_row_size(iid, size, idx, count, current_token, pn)
                ),
            )
            # Sanfte Pause, damit Backups und SSH-Verbindung nicht überlastet werden
            _time.sleep(0.3)

    def _update_archive_row_size(self, item_id, size_label, index, total, token, profile_name=None):
        if token != self.archive_loading_token or not self.tree.exists(item_id):
            return

        values = list(self.tree.item(item_id, "values"))
        if len(values) >= 2:
            values[0] = size_label
            self.tree.item(item_id, values=tuple(values))

        if index >= total:
            self._set_archive_loading(False, f"Archivliste geladen: {total} Archive")
            self._show_notice(f"Archivliste geladen: {total} Archive.", level="success")
        else:
            self._set_archive_loading(True, f"Archivgrößen werden geladen ({index}/{total})...")

    def _query_archive_size_label(self, archive_name):
        if self.backup_running or not self.daemon_online:
            return "n/a"
        job = self.config_data.get("profile_name", "Standard")
        try:
            result = self.ipc_slow.call("archive_info", {"job": job, "archive": archive_name})
        except IpcError:
            return "n/a"
        size = (result or {}).get("original_size") or (result or {}).get("size")
        if isinstance(size, (int, float)):
            return self._human_size(size)
        if isinstance(size, str) and size.strip():
            return size.strip()
        return "n/a"

    def _human_size(self, size_bytes):
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size_bytes < 1024:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024
        return f"{size_bytes:.1f} PB"

    def _mount_archive(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showerror("Fehler", "Kein Archiv ausgewählt.")
            return
        name = self.tree.item(selected[0], "text")
        job = self.config_data.get("profile_name", "Standard")
        self._show_notice(f"Hänge Archiv {name} ein …", level="info")

        def done(result, error):
            if error is not None:
                messagebox.showerror("Mount", error.message)
                return
            path = str((result or {}).get("path") or "")
            self.mount_active = True
            self.mount_point = path
            self.mounted_archive_name = name
            if hasattr(self, "mount_btn"):
                self.mount_btn.config(text="Aushängen")
            if hasattr(self, "open_btn"):
                self.open_btn.config(state="normal")
            self._show_notice(f"Archiv gemountet auf {path}", level="success", timeout_ms=5000)
            self._refresh_daemon_status()

        self._daemon_call_async("mount", {"job": job, "archive": name}, on_done=done, slow=True)

    def _delete_archive(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showerror("Fehler", "Kein Archiv ausgewählt.")
            return
        name = self.tree.item(selected[0], "text")
        if not messagebox.askyesno("Löschen", f"Archiv '{name}' wirklich löschen?"):
            return
        job = self.config_data.get("profile_name", "Standard")
        self._append_backup_log(f"Lösche Archiv {name}...\n")

        def done(result, error):
            if error is not None:
                messagebox.showerror("Löschen", error.message)
                return
            self._show_notice(f"Löschen von {name} an den Dienst übergeben.", level="info")
            self._refresh_daemon_status()

        self._daemon_call_async("delete_archive", {"job": job, "archive": name}, on_done=done, slow=True)

    def _start_restore(self):
        if not self.daemon_online:
            self._show_notice("Backup-Dienst nicht verbunden.", level="warning")
            return
        archive = self.restore_archive_var.get().strip()
        target = self.restore_path_var.get().strip()
        job = self.restore_job_var.get().strip() or self.config_data.get("profile_name", "Standard")
        if not archive or not target:
            messagebox.showerror("Fehler", "Archivname und Zielverzeichnis angeben.")
            return
        if not target.startswith("/"):
            messagebox.showerror("Fehler", "Zielverzeichnis muss ein absoluter Pfad sein.")
            return
        if target == "/" and not messagebox.askyesno(
            "Warnung",
            "Ziel „/“ überschreibt Systemdateien des laufenden Systems.\n\nWirklich fortfahren?",
        ):
            return
        paths = []
        if hasattr(self, "restore_paths_text"):
            paths = [line.strip() for line in self.restore_paths_text.get("1.0", tk.END).splitlines() if line.strip()]
        self._append_restore_log(f"Starte Restore von {archive} nach {target}...\n")
        self.restore_progress["value"] = 0
        self.restore_progress_var.set("Wiederherstellung wird gestartet …")

        def done(result, error):
            if error is not None:
                self.restore_progress_var.set("")
                messagebox.showerror("Wiederherstellung", error.message)
                return
            if isinstance(result, dict):
                self.current_restore_id = str(result.get("id") or "")
            self._update_action_buttons()
            self._refresh_daemon_status()

        self._daemon_call_async(
            "extract",
            {"job": job, "archive": archive, "target": target, "paths": paths},
            on_done=done,
            slow=True,
        )

    def _cancel_restore(self) -> None:
        current = self.daemon_status.get("current") or {}
        run_id = str(self.current_restore_id or current.get("id") or "")
        if not run_id:
            return

        def done(result, error):
            if error is not None:
                self._show_notice(f"Abbruch fehlgeschlagen: {error.message}", level="error")
                return
            self._append_restore_log("\n[INFO] Abbruch gesendet.\n")
            self._refresh_daemon_status()

        self._daemon_call_async("cancel", {"id": run_id}, on_done=done)

    def _build_accounts_tab(self) -> None:
        tab = self.tab_accounts
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        ttk.Label(tab, text="Accounts", style="Title.TLabel").grid(row=0, column=0, sticky="w", padx=12, pady=(12, 6))
        ttk.Label(
            tab,
            text="Wiederverwendbare Zugangsdaten für SSH, S3 und lokale Ziele.",
            style="Muted.TLabel",
        ).grid(row=0, column=0, sticky="e", padx=12, pady=(12, 6))

        self.accounts_tree = ttk.Treeview(tab, columns=("type", "summary", "used"), show="headings", height=12)
        self.accounts_tree.heading("type", text="Typ")
        self.accounts_tree.heading("summary", text="Details")
        self.accounts_tree.heading("used", text="Ziele")
        self.accounts_tree.column("type", width=80, stretch=False)
        self.accounts_tree.column("summary", width=420)
        self.accounts_tree.column("used", width=70, stretch=False)
        self.accounts_tree.grid(row=1, column=0, sticky="nsew", padx=12, pady=6)

        btns = ttk.Frame(tab)
        btns.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 12))
        ttk.Button(btns, text="Neu", command=lambda: self._show_account_dialog()).pack(side="left", padx=(0, 4))
        ttk.Button(btns, text="Bearbeiten", command=self._edit_selected_account).pack(side="left", padx=4)
        ttk.Button(btns, text="Löschen", command=self._delete_selected_account).pack(side="left", padx=4)
        ttk.Button(btns, text="Aktualisieren", command=self._refresh_accounts_tree).pack(side="left", padx=4)
        self.master.after(300, self._refresh_accounts_tree)

    def _refresh_accounts_tree(self) -> None:
        if not hasattr(self, "accounts_tree"):
            return
        for item in self.accounts_tree.get_children():
            self.accounts_tree.delete(item)
        for account in self.config_data.get("accounts", []):
            self.accounts_tree.insert(
                "",
                tk.END,
                iid=str(account.get("id")),
                values=(
                    account.get("type", ""),
                    f"{account.get('name', '')} · {account_summary(account)}",
                    account_usage_count(self.config_data, str(account.get("id"))),
                ),
            )

    def _edit_selected_account(self) -> None:
        sel = self.accounts_tree.selection() if hasattr(self, "accounts_tree") else ()
        if not sel:
            messagebox.showinfo("Accounts", "Bitte einen Account auswählen.")
            return
        account = get_account(self.config_data, sel[0])
        if account:
            self._show_account_dialog(edit=account)

    def _delete_selected_account(self) -> None:
        sel = self.accounts_tree.selection() if hasattr(self, "accounts_tree") else ()
        if not sel:
            return
        acc_id = sel[0]
        used = account_usage_count(self.config_data, acc_id)
        if used:
            messagebox.showwarning(
                "Accounts",
                f"Account wird von {used} Ziel(en) verwendet und kann nicht gelöscht werden.",
            )
            return
        if not messagebox.askyesno("Löschen", "Account wirklich löschen?"):
            return
        self.config_data["accounts"] = [a for a in self.config_data.get("accounts", []) if a.get("id") != acc_id]
        self._save_config()
        self._refresh_accounts_tree()

    def _show_account_dialog(self, edit: dict | None = None, on_saved=None):
        dialog = tk.Toplevel(self.master)
        dialog.title("Account bearbeiten" if edit else "Neuer Account")
        dialog.geometry("520x420")
        dialog.transient(self.master)
        dialog.configure(bg=self.ui["bg"])
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        name_var = tk.StringVar(value=(edit or {}).get("name", ""))
        type_var = tk.StringVar(value=(edit or {}).get("type", "ssh"))
        ssh_var = tk.StringVar(value=(edit or {}).get("ssh_key", ""))
        pass_var = tk.StringVar(value=(edit or {}).get("passphrase", ""))
        s3_access = tk.StringVar(value=(edit or {}).get("s3_access_key", ""))
        s3_secret = tk.StringVar(value=(edit or {}).get("s3_secret_key", ""))
        s3_endpoint = tk.StringVar(value=(edit or {}).get("s3_endpoint_url", "https://fsn1.your-objectstorage.com"))
        s3_region = tk.StringVar(value=(edit or {}).get("s3_region", "fsn1"))

        ttk.Label(frame, text="Name").grid(row=0, column=0, sticky="e", padx=5, pady=4)
        ttk.Entry(frame, textvariable=name_var).grid(row=0, column=1, sticky="ew", padx=5, pady=4)
        ttk.Label(frame, text="Typ").grid(row=1, column=0, sticky="e", padx=5, pady=4)
        type_box = ttk.Combobox(frame, textvariable=type_var, values=["ssh", "s3", "local"], state="readonly")
        type_box.grid(row=1, column=1, sticky="w", padx=5, pady=4)

        ssh_frame = ttk.Frame(frame)
        s3_frame = ttk.Frame(frame)
        local_frame = ttk.Frame(frame)
        for sub in (ssh_frame, s3_frame, local_frame):
            sub.columnconfigure(1, weight=1)

        ttk.Label(ssh_frame, text="SSH Key").grid(row=0, column=0, sticky="e", padx=5, pady=4)
        ttk.Entry(ssh_frame, textvariable=ssh_var).grid(row=0, column=1, sticky="ew", padx=5, pady=4)
        ttk.Label(ssh_frame, text="Passphrase").grid(row=1, column=0, sticky="e", padx=5, pady=4)
        ttk.Entry(ssh_frame, textvariable=pass_var, show="*").grid(row=1, column=1, sticky="ew", padx=5, pady=4)

        ttk.Label(s3_frame, text="Access Key").grid(row=0, column=0, sticky="e", padx=5, pady=4)
        ttk.Entry(s3_frame, textvariable=s3_access).grid(row=0, column=1, sticky="ew", padx=5, pady=4)
        ttk.Label(s3_frame, text="Secret Key").grid(row=1, column=0, sticky="e", padx=5, pady=4)
        ttk.Entry(s3_frame, textvariable=s3_secret, show="*").grid(row=1, column=1, sticky="ew", padx=5, pady=4)
        ttk.Label(s3_frame, text="Endpoint").grid(row=2, column=0, sticky="e", padx=5, pady=4)
        ttk.Entry(s3_frame, textvariable=s3_endpoint).grid(row=2, column=1, sticky="ew", padx=5, pady=4)
        ttk.Label(s3_frame, text="Region").grid(row=3, column=0, sticky="e", padx=5, pady=4)
        ttk.Entry(s3_frame, textvariable=s3_region).grid(row=3, column=1, sticky="ew", padx=5, pady=4)
        ttk.Label(s3_frame, text="Passphrase").grid(row=4, column=0, sticky="e", padx=5, pady=4)
        ttk.Entry(s3_frame, textvariable=pass_var, show="*").grid(row=4, column=1, sticky="ew", padx=5, pady=4)

        ttk.Label(local_frame, text="Passphrase").grid(row=0, column=0, sticky="e", padx=5, pady=4)
        ttk.Entry(local_frame, textvariable=pass_var, show="*").grid(row=0, column=1, sticky="ew", padx=5, pady=4)
        ttk.Label(local_frame, text="Lokale Accounts brauchen nur die Repo-Passphrase.", style="Muted.TLabel").grid(
            row=1, column=0, columnspan=2, sticky="w", padx=5, pady=4
        )

        fields_host = ttk.Frame(frame)
        fields_host.grid(row=2, column=0, columnspan=2, sticky="ew", pady=8)
        fields_host.columnconfigure(0, weight=1)

        def _show_type_fields(*_args):
            for child in fields_host.winfo_children():
                child.grid_forget()
            current = type_var.get()
            target = ssh_frame if current == "ssh" else s3_frame if current == "s3" else local_frame
            target.grid(row=0, column=0, sticky="ew")

        type_box.bind("<<ComboboxSelected>>", _show_type_fields)
        _show_type_fields()

        def _save():
            name = name_var.get().strip()
            if not name:
                messagebox.showerror("Fehler", "Name fehlt.", parent=dialog)
                return
            payload = {
                "id": (edit or {}).get("id") or new_id("acc"),
                "name": name,
                "type": type_var.get().strip() or "ssh",
                "ssh_key": ssh_var.get().strip(),
                "passphrase": pass_var.get(),
                "s3_access_key": s3_access.get().strip(),
                "s3_secret_key": s3_secret.get(),
                "s3_endpoint_url": s3_endpoint.get().strip(),
                "s3_region": s3_region.get().strip(),
            }
            accounts = list(self.config_data.get("accounts", []))
            if edit:
                accounts = [payload if a.get("id") == edit.get("id") else a for a in accounts]
            else:
                accounts.append(payload)
            self.config_data["accounts"] = accounts
            self._save_config()
            self._refresh_accounts_tree()
            dialog.destroy()
            if on_saved:
                on_saved(payload)

        ttk.Button(frame, text="Speichern", style="Accent.TButton", command=_save).grid(
            row=3, column=1, sticky="e", padx=5, pady=10
        )
        ttk.Button(frame, text="Abbrechen", command=dialog.destroy).grid(row=3, column=0, sticky="w", padx=5, pady=10)

    def _active_job(self):
        name = self.config_data.get("profile_name", "Standard")
        job = get_job(self.config_data, name)
        if job:
            return job
        jobs = self.config_data.get("jobs") or []
        return jobs[0] if jobs else None

    def _sync_active_job_from_ui(self) -> None:
        job = self._active_job()
        if not job:
            return
        if hasattr(self, "sources_tree"):
            self._collect_sources_from_ui()
            job["sources"] = list(self.config_data.get("sources") or [])
            sync_sources_to_flat(job)
        elif hasattr(self, "include_text"):
            includes = [line.strip() for line in self.include_text.get("1.0", tk.END).splitlines() if line.strip()]
            job["include_folders"] = includes or ["/"]
            if hasattr(self, "exclude_text"):
                excludes = [line.strip() for line in self.exclude_text.get("1.0", tk.END).splitlines() if line.strip()]
                job["exclude_folders"] = excludes
        if hasattr(self, "compression_var"):
            job["compression"] = self.compression_var.get().strip() or "lz4"
        # Schedule/Wartung: nur über _sync_schedule_volume_from_ui (pro Volumen)

    def _refresh_destinations_tree(self) -> None:
        if not hasattr(self, "dest_tree"):
            return
        for item in self.dest_tree.get_children():
            self.dest_tree.delete(item)
        job = self._active_job()
        if not job:
            return
        for dest in destinations_for_job(self.config_data, job):
            account = get_account(self.config_data, str(dest.get("account_id", ""))) or {}
            self.dest_tree.insert(
                "",
                tk.END,
                iid=str(dest.get("id")),
                values=(
                    account.get("type", ""),
                    account.get("name", ""),
                    dest.get("storage") or dest.get("local_path") or "",
                ),
            )

    def _remove_selected_destination(self) -> None:
        if not hasattr(self, "dest_tree"):
            return
        sel = self.dest_tree.selection()
        if not sel:
            return
        dest_id = sel[0]
        job = self._active_job()
        if not job:
            return
        ids = [d for d in job.get("destination_ids", []) if d != dest_id]
        if not ids:
            messagebox.showwarning("Ziele", "Mindestens ein Ziel muss bleiben.")
            return
        job["destination_ids"] = ids
        self._save_config()
        self._refresh_destinations_tree()

    def _init_selected_destination(self) -> None:
        """Ask the backup service to run ``borg init`` for the selected destination."""
        if not hasattr(self, "dest_tree"):
            return
        if not self.daemon_online:
            self._show_notice("Backup-Dienst nicht verbunden.", level="warning")
            return
        sel = self.dest_tree.selection()
        if not sel:
            messagebox.showinfo("Repository", "Bitte zuerst ein Backup-Ziel auswählen.")
            return
        dest_id = str(sel[0])
        dest = get_destination(self.config_data, dest_id)
        if not dest:
            messagebox.showerror("Repository", "Ausgewähltes Ziel wurde nicht gefunden.")
            return
        repo = str(dest.get("storage") or dest.get("local_path") or "").strip()
        label = str(dest.get("name") or repo or dest_id)
        if not messagebox.askyesno(
            "Repository initialisieren",
            f"Soll das Repository für „{label}“ jetzt initialisiert werden?\n\n"
            f"{repo or '(kein Pfad)'}\n\n"
            "Dafür ist ggf. das Administrator-Passwort nötig (polkit).",
        ):
            return
        self._show_notice(f"Initialisiere Repository „{label}“ …", level="info")

        def done(result, error):
            if error is not None:
                messagebox.showerror("Repository", error.message)
                return
            self._show_notice(
                f"Initialisierung von „{label}“ gestartet.",
                level="info",
                timeout_ms=8000,
            )

        self._daemon_call_async("init_repo", {"dest_id": dest_id}, on_done=done, slow=True)

    def _show_add_destination_dialog(self) -> None:
        dialog = tk.Toplevel(self.master)
        dialog.title("Ziel hinzufügen")
        dialog.geometry("560x360")
        dialog.transient(self.master)
        dialog.configure(bg=self.ui["bg"])
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        mode_var = tk.StringVar(value="existing")
        ttk.Radiobutton(frame, text="Bestehenden Account nutzen", variable=mode_var, value="existing").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=4
        )
        ttk.Radiobutton(frame, text="Neuen Account anlegen", variable=mode_var, value="new").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=4
        )

        accounts = self.config_data.get("accounts", [])
        account_labels = [f"{a.get('name')} ({a.get('type')})" for a in accounts]
        account_map = {f"{a.get('name')} ({a.get('type')})": a for a in accounts}
        account_var = tk.StringVar(value=account_labels[0] if account_labels else "")
        ttk.Label(frame, text="Account").grid(row=2, column=0, sticky="e", padx=5, pady=6)
        account_combo = ttk.Combobox(frame, textvariable=account_var, values=account_labels, state="readonly")
        account_combo.grid(row=2, column=1, sticky="ew", padx=5, pady=6)

        name_var = tk.StringVar(value="")
        storage_var = tk.StringVar(value="")
        ttk.Label(frame, text="Ziel-Name").grid(row=3, column=0, sticky="e", padx=5, pady=6)
        ttk.Entry(frame, textvariable=name_var).grid(row=3, column=1, sticky="ew", padx=5, pady=6)
        ttk.Label(frame, text="Repo / Pfad").grid(row=4, column=0, sticky="e", padx=5, pady=6)
        ttk.Entry(frame, textvariable=storage_var).grid(row=4, column=1, sticky="ew", padx=5, pady=6)

        def _create_with_account(account: dict):
            dest_name = name_var.get().strip() or f"{account.get('name')} Ziel"
            storage = storage_var.get().strip()
            if not storage:
                messagebox.showerror("Fehler", "Repository/Pfad fehlt.", parent=dialog)
                return
            dest = {
                "id": new_id("dest"),
                "name": dest_name,
                "account_id": account.get("id"),
                "storage": storage,
                "local_path": storage if account.get("type") == "local" else "",
            }
            self.config_data.setdefault("destinations", []).append(dest)
            job = self._active_job()
            if job is not None:
                ids = list(job.get("destination_ids") or [])
                ids.append(dest["id"])
                job["destination_ids"] = ids
            self._save_config()
            self._refresh_destinations_tree()
            dialog.destroy()

        def _continue():
            if mode_var.get() == "new":
                self._show_account_dialog(on_saved=_create_with_account)
                return
            label = account_var.get()
            account = account_map.get(label)
            if not account:
                messagebox.showerror("Fehler", "Kein Account gewählt.", parent=dialog)
                return
            _create_with_account(account)

        ttk.Button(frame, text="Hinzufügen", style="Accent.TButton", command=_continue).grid(
            row=5, column=1, sticky="e", padx=5, pady=12
        )
        ttk.Button(frame, text="Abbrechen", command=dialog.destroy).grid(row=5, column=0, sticky="w", padx=5, pady=12)

    def _collect_sources_from_ui(self) -> None:
        """Persist current exclude-list edits into config sources before save/run."""
        if not hasattr(self, "sources_tree"):
            return
        self._commit_exclude_list_to_source()
        sources: list[dict] = []
        for iid in self.sources_tree.get_children():
            vals = self.sources_tree.item(iid, "values")
            path = str(iid)
            label = str(vals[0]) if vals else path
            size = str(vals[2]) if len(vals) > 2 else ""
            existing = next(
                (s for s in (self.config_data.get("sources") or []) if str(s.get("path")) == path),
                None,
            )
            excludes = list((existing or {}).get("excludes") or [])
            src = enrich_source(path, excludes)
            src["label"] = label or src["label"]
            if size:
                src["size"] = size
            sources.append(src)
        self.config_data["sources"] = sources or [enrich_source("/", suggest_excludes_for_path("/"))]
        sync_sources_to_flat(self.config_data)

    def _refresh_sources_tree(self) -> None:
        if not hasattr(self, "sources_tree"):
            return
        ensure_sources(self.config_data)
        prev = getattr(self, "_sources_selected_path", "")
        for item in self.sources_tree.get_children():
            self.sources_tree.delete(item)
        for src in self.config_data.get("sources") or []:
            path = str(src.get("path") or "")
            if not path:
                continue
            n_exc = len(src.get("excludes") or [])
            self.sources_tree.insert(
                "",
                tk.END,
                iid=path,
                values=(
                    src.get("label") or path,
                    path,
                    src.get("size") or "—",
                    f"{n_exc} Regeln",
                ),
            )
        children = self.sources_tree.get_children()
        if prev and prev in children:
            self.sources_tree.selection_set(prev)
            self.sources_tree.focus(prev)
        elif children:
            self.sources_tree.selection_set(children[0])
            self.sources_tree.focus(children[0])
        self._on_source_selected()

    def _selected_source_dict(self) -> dict | None:
        path = getattr(self, "_sources_selected_path", "")
        if not path:
            return None
        for src in self.config_data.get("sources") or []:
            if str(src.get("path")) == path:
                return src
        return None

    def _commit_exclude_list_to_source(self) -> None:
        if not hasattr(self, "source_exclude_list"):
            return
        src = self._selected_source_dict()
        if not src:
            return
        items = [self.source_exclude_list.get(i) for i in range(self.source_exclude_list.size())]
        src["excludes"] = [x for x in items if str(x).strip()]
        sync_sources_to_flat(self.config_data)
        # Update exclude count in tree
        path = str(src.get("path"))
        if hasattr(self, "sources_tree") and path in self.sources_tree.get_children():
            vals = list(self.sources_tree.item(path, "values"))
            if len(vals) >= 4:
                vals[3] = f"{len(src['excludes'])} Regeln"
                self.sources_tree.item(path, values=vals)

    def _load_exclude_listbox(self, src: dict | None) -> None:
        if not hasattr(self, "source_exclude_list"):
            return
        self.source_exclude_list.delete(0, tk.END)
        for exc in (src or {}).get("excludes") or []:
            self.source_exclude_list.insert(tk.END, exc)

    def _on_source_selected(self, event=None) -> None:
        if not hasattr(self, "sources_tree"):
            return
        sel = self.sources_tree.selection()
        new_path = sel[0] if sel else ""
        old_path = getattr(self, "_sources_selected_path", "")
        if new_path != old_path:
            self._commit_exclude_list_to_source()
        if not sel:
            self._sources_selected_path = ""
            if hasattr(self, "source_exclude_title"):
                self.source_exclude_title.set("Kein Laufwerk ausgewählt")
            self._load_exclude_listbox(None)
            return
        self._sources_selected_path = new_path
        src = self._selected_source_dict()
        label = (src or {}).get("label") or new_path
        if hasattr(self, "source_exclude_title"):
            self.source_exclude_title.set(f"{label}  ·  {new_path}")
        self._load_exclude_listbox(src)

    def _remove_selected_source(self) -> None:
        if not hasattr(self, "sources_tree"):
            return
        sel = self.sources_tree.selection()
        if not sel:
            return
        path = sel[0]
        sources = [s for s in (self.config_data.get("sources") or []) if str(s.get("path")) != path]
        if not sources:
            messagebox.showwarning("Quellen", "Mindestens ein Laufwerk muss bleiben.")
            return
        self._sources_selected_path = ""
        self.config_data["sources"] = sources
        sync_sources_to_flat(self.config_data)
        self._refresh_sources_tree()

    def _add_exclude_to_selected_source(self) -> None:
        src = self._selected_source_dict()
        if not src:
            messagebox.showinfo("Excludes", "Bitte zuerst ein Laufwerk auswählen.")
            return
        base = str(src.get("path") or "/")
        chosen = filedialog.askdirectory(initialdir=base if os.path.isdir(base) else "/", title="Exclude-Pfad wählen")
        if not chosen:
            return
        excludes = list(src.get("excludes") or [])
        if chosen not in excludes:
            excludes.append(chosen)
            src["excludes"] = excludes
            sync_sources_to_flat(self.config_data)
            self._load_exclude_listbox(src)
            self._refresh_sources_tree_counts_only()

    def _remove_selected_exclude(self) -> None:
        if not hasattr(self, "source_exclude_list"):
            return
        sel = list(self.source_exclude_list.curselection())
        if not sel:
            return
        for idx in reversed(sel):
            self.source_exclude_list.delete(idx)
        self._commit_exclude_list_to_source()

    def _refresh_sources_tree_counts_only(self) -> None:
        if not hasattr(self, "sources_tree"):
            return
        for src in self.config_data.get("sources") or []:
            path = str(src.get("path") or "")
            if path and path in self.sources_tree.get_children():
                vals = list(self.sources_tree.item(path, "values"))
                if len(vals) >= 4:
                    vals[3] = f"{len(src.get('excludes') or [])} Regeln"
                    self.sources_tree.item(path, values=vals)

    def _apply_exclude_suggestions_selected(self) -> None:
        src = self._selected_source_dict()
        if not src:
            messagebox.showinfo("Excludes", "Bitte zuerst ein Laufwerk auswählen.")
            return
        path = str(src.get("path") or "/")
        suggestions = suggest_excludes_for_path(path)
        current = list(src.get("excludes") or [])
        merged = list(current)
        for item in suggestions:
            if item not in merged:
                merged.append(item)
        src["excludes"] = merged
        sync_sources_to_flat(self.config_data)
        added = len(merged) - len(current)
        self._load_exclude_listbox(src)
        self._refresh_sources_tree_counts_only()
        if added:
            self._show_notice(f"{added} Exclude-Vorschlag/Vorschläge übernommen.", level="info")
        else:
            self._show_notice("Keine neuen Vorschläge (bereits enthalten).", level="info")

    def _show_disk_picker(self) -> None:
        mounts = list_backup_mounts()
        dialog = tk.Toplevel(self.master)
        dialog.title("Laufwerke hinzufügen")
        dialog.geometry("720x480")
        dialog.transient(self.master)
        dialog.configure(bg=self.ui["bg"])
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        ttk.Label(
            frame,
            text="Laufwerke auswählen — Auto-Excludes werden je Laufwerk vorgeschlagen",
            style="Muted.TLabel",
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))
        tree = ttk.Treeview(
            frame,
            columns=("label", "path", "size", "type", "fstype", "suggest"),
            show="headings",
            selectmode="extended",
        )
        tree.heading("label", text="Laufwerk")
        tree.heading("path", text="Pfad")
        tree.heading("size", text="Größe")
        tree.heading("type", text="Typ")
        tree.heading("fstype", text="FS")
        tree.heading("suggest", text="Auto-Excludes")
        tree.column("label", width=140, stretch=False)
        tree.column("path", width=160)
        tree.column("size", width=70, stretch=False)
        tree.column("type", width=70, stretch=False)
        tree.column("fstype", width=60, stretch=False)
        tree.column("suggest", width=100, stretch=False)
        tree.grid(row=1, column=0, sticky="nsew")
        existing = {str(s.get("path")) for s in (self.config_data.get("sources") or [])}
        for mount in mounts:
            path = mount["path"]
            n_sug = len(suggest_excludes_for_path(path))
            tree.insert(
                "",
                tk.END,
                iid=path,
                values=(
                    mount.get("label", ""),
                    path,
                    mount.get("size", ""),
                    mount.get("type", ""),
                    mount.get("fstype", ""),
                    f"{n_sug} Vorschläge",
                ),
            )
            if path in existing:
                tree.selection_add(path)

        apply_auto = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frame,
            text="Auto-Exclude-Vorschläge beim Hinzufügen übernehmen",
            variable=apply_auto,
        ).grid(row=2, column=0, sticky="w", pady=(8, 0))

        def _add_selected():
            selected = tree.selection()
            if not selected:
                return
            self._collect_sources_from_ui()
            sources = list(self.config_data.get("sources") or [])
            known = {str(s.get("path")) for s in sources}
            for path in selected:
                if path in known:
                    continue
                excludes = suggest_excludes_for_path(path) if apply_auto.get() else []
                src = enrich_source(path, excludes)
                # Prefer label from mount list
                for m in mounts:
                    if m["path"] == path and m.get("label"):
                        src["label"] = m["label"]
                        src["size"] = m.get("size", "")
                        break
                sources.append(src)
                known.add(path)
            self.config_data["sources"] = sources
            sync_sources_to_flat(self.config_data)
            self._refresh_sources_tree()
            dialog.destroy()

        btns = ttk.Frame(frame)
        btns.grid(row=3, column=0, sticky="ew", pady=10)
        ttk.Button(btns, text="Hinzufügen", style="Accent.TButton", command=_add_selected).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(btns, text="Abbrechen", command=dialog.destroy).pack(side="left")

    def _protocol_retention_settings_from_ui(self) -> tuple[bool, int, str]:
        enabled = True
        if hasattr(self, "protocol_retention_enabled_var"):
            enabled = bool(self.protocol_retention_enabled_var.get())
        try:
            value = int(self.protocol_retention_value_var.get() or 3)
        except (ValueError, AttributeError, tk.TclError):
            value = int(self.config_data.get("protocol_retention_value", 3) or 3)
        value = max(1, value)
        unit = "months"
        if hasattr(self, "protocol_retention_unit_var"):
            unit = self.protocol_retention_unit_var.get().strip() or "months"
        return enabled, value, unit

    def _apply_protocol_retention(self) -> None:
        """Save retention settings and optionally purge expired protocols after confirm."""
        enabled, value, unit = self._protocol_retention_settings_from_ui()
        self.config_data["protocol_retention_enabled"] = enabled
        self.config_data["protocol_retention_value"] = value
        self.config_data["protocol_retention_unit"] = unit
        self._save_config()

        if not enabled:
            self._show_notice("Aufbewahrung gespeichert (automatisches Löschen aus).", level="info")
            return

        expired = list_expired_protocol_pdfs(CONFIG_DIR, enabled, value, unit)
        if not expired:
            self._show_notice(
                f"Aufbewahrung gespeichert ({value} {unit}) — keine Protokolle zum Löschen.",
                level="info",
            )
            return

        preview = "\n".join(f"• {item['name']}" for item in expired[:8])
        if len(expired) > 8:
            preview += f"\n… und {len(expired) - 8} weitere"
        unit_de = {"days": "Tage", "months": "Monate", "years": "Jahre"}.get(unit, unit)
        if not messagebox.askyesno(
            "Aufbewahrung anwenden",
            f"Einstellung gespeichert: älter als {value} {unit_de}.\n\n"
            f"Jetzt würden {len(expired)} Protokoll(e) gelöscht:\n\n{preview}\n\n"
            "Wirklich jetzt löschen?",
            icon="warning",
        ):
            self._show_notice(
                f"Aufbewahrung gespeichert — {len(expired)} Protokoll(e) nicht gelöscht.",
                level="warning",
            )
            return

        deleted = purge_old_protocol_pdfs(CONFIG_DIR, enabled, value, unit)
        self._refresh_protocol_pdf_list()
        if hasattr(self, "protocol_preview"):
            self._append_protocol_preview(f"[Retention] {len(deleted)} Protokoll-PDF(s) gelöscht.\n")
        self._show_notice(f"{len(deleted)} Protokoll(e) gelöscht.", level="info")

    def _purge_old_protocol_pdfs(self) -> None:
        deleted = purge_old_protocol_pdfs(
            CONFIG_DIR,
            bool(self.config_data.get("protocol_retention_enabled", True)),
            int(self.config_data.get("protocol_retention_value", 3) or 3),
            str(self.config_data.get("protocol_retention_unit", "months")),
        )
        if deleted and hasattr(self, "protocol_preview"):
            self._append_protocol_preview(f"[Retention] {len(deleted)} alte PDF(s) gelöscht.\n")
        if deleted and hasattr(self, "protocol_tree"):
            self._refresh_protocol_pdf_list()

    def _refresh_protocol_pdf_list(self) -> None:
        if not hasattr(self, "protocol_tree"):
            return
        for item in self.protocol_tree.get_children():
            self.protocol_tree.delete(item)
        for pdf in list_protocol_pdfs(CONFIG_DIR):
            self.protocol_tree.insert(
                "",
                tk.END,
                iid=str(pdf["path"]),
                values=(
                    pdf["mtime"].strftime("%Y-%m-%d %H:%M"),
                    pdf["name"],
                    format_size(int(pdf["size"])),
                ),
            )

    def _open_selected_protocol_pdf(self) -> None:
        if not hasattr(self, "protocol_tree"):
            return
        sel = self.protocol_tree.selection()
        if not sel:
            messagebox.showinfo("Protokoll", "Bitte zuerst ein Protokoll in der Liste auswählen.")
            return
        path = sel[0]
        try:
            subprocess.Popen(["xdg-open", path], start_new_session=True)
        except OSError:
            messagebox.showinfo("Protokoll", path)

    def _ensure_protocol_dir(self) -> None:
        """Create protokolle/ and migrate legacy PDFs out of the config root."""
        try:
            protocol_dir(CONFIG_DIR)
            moved = migrate_protocol_pdfs_into_subdir(CONFIG_DIR)
            if moved and hasattr(self, "protocol_preview"):
                self._append_protocol_preview(
                    f"[Migration] {len(moved)} Protokoll-PDF(s) nach {PROTOCOL_DIR.name}/ verschoben.\n"
                )
            if hasattr(self, "protocol_tree"):
                self._refresh_protocol_pdf_list()
            # Fix last-pdf pointer if it still points at config root
            last = str(self.config_data.get("protocol_last_pdf") or "")
            if last:
                last_path = Path(last)
                if last_path.parent == CONFIG_DIR and (PROTOCOL_DIR / last_path.name).is_file():
                    self.config_data["protocol_last_pdf"] = str(PROTOCOL_DIR / last_path.name)
                    self._save_config()
        except OSError as exc:
            self._append_backup_log(f"[WARNUNG] Protokoll-Ordner: {exc}\n")

    def _reveal_selected_protocol_pdf(self) -> None:
        """Open the dedicated protocol folder in the file manager (selection optional)."""
        try:
            folder_path = protocol_dir(CONFIG_DIR)
        except OSError as exc:
            messagebox.showerror("Protokoll-Ordner", str(exc))
            return

        target_file: Path | None = None
        if hasattr(self, "protocol_tree"):
            sel = self.protocol_tree.selection()
            if sel:
                candidate = Path(sel[0])
                if candidate.is_file():
                    target_file = candidate

        folder = str(folder_path)
        commands: list[list[str]] = []
        if target_file is not None:
            commands.extend(
                [
                    ["nautilus", "--select", str(target_file)],
                    ["dolphin", "--select", str(target_file)],
                    ["nemo", str(target_file.parent)],
                ]
            )
        commands.extend(
            [
                ["xdg-open", folder],
                ["gio", "open", folder],
            ]
        )
        for cmd in commands:
            try:
                subprocess.Popen(cmd, start_new_session=True)
                return
            except OSError:
                continue
        messagebox.showinfo("Protokoll-Ordner", folder)

    def _delete_selected_protocol_pdf(self) -> None:
        if not hasattr(self, "protocol_tree"):
            return
        sel = self.protocol_tree.selection()
        if not sel:
            return
        path = Path(sel[0])
        if not messagebox.askyesno("Löschen", f"{path.name} löschen?"):
            return
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            messagebox.showerror("Fehler", str(exc))
            return
        self._refresh_protocol_pdf_list()

    def _set_update_status(self, text: str) -> None:
        if hasattr(self, "update_status_var"):
            self.update_status_var.set(text)

    def _auto_check_app_update(self) -> None:
        enabled = bool(self.config_data.get("auto_update_check", True))
        if hasattr(self, "auto_update_var"):
            enabled = bool(self.auto_update_var.get())
        if not enabled:
            return
        self._check_for_app_update(silent=True)

    def _check_for_app_update(self, silent: bool = False) -> None:
        self._set_update_status(f"Version {__version__} — prüfe GitHub…")

        def worker() -> None:
            try:
                import borg_updates

                # Manual button forces a refresh; auto-check uses the 6h cache.
                status = borg_updates.check_for_update(current=__version__, force=not silent)
            except Exception as exc:
                status = {"phase": "error", "error": str(exc), "available": False}

            def done() -> None:
                phase = status.get("phase")
                if status.get("available"):
                    latest = status.get("latest_version")
                    self._set_update_status(f"Update v{latest} verfügbar (aktuell {__version__})")
                    if silent and not self._update_prompt_pending:
                        self._update_prompt_pending = True
                        if messagebox.askyesno(
                            "Update verfügbar",
                            f"Borg Backup GUI v{latest} ist verfügbar "
                            f"(installiert: {__version__}).\n\n"
                            "Jetzt herunterladen und installieren?\n"
                            "(pkexec / Admin-Passwort erforderlich)",
                        ):
                            self._install_app_update()
                        self._update_prompt_pending = False
                    elif not silent:
                        self._show_notice(f"Update v{latest} verfügbar.", level="info")
                elif phase == "up-to-date":
                    self._set_update_status(f"Version {__version__} — aktuell")
                    if not silent:
                        self._show_notice("Keine neuere Version gefunden.", level="info")
                else:
                    err = status.get("error") or "Update-Check fehlgeschlagen"
                    self._set_update_status(f"Version {__version__} — {err}")
                    if not silent:
                        self._show_notice(str(err), level="warning")

            self.master.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    def _install_app_update(self) -> None:
        if self.backup_running or self.daemon_busy:
            self._show_notice(
                "Update während eines laufenden Backup-/Restore-Auftrags gesperrt.",
                level="warning",
                timeout_ms=10000,
            )
            return
        self._set_update_status(f"Version {__version__} — lade Update…")

        def worker() -> None:
            try:
                import borg_updates

                status = borg_updates.get_status()
                if not status.get("available"):
                    status = borg_updates.check_for_update(current=__version__, force=True)
                if not status.get("available"):
                    result = status
                else:

                    def on_progress(pct: int) -> None:
                        self.master.after(0, lambda p=pct: self._set_update_status(f"Download… {p}%"))

                    result = borg_updates.download_and_install(progress_cb=on_progress)
                    if result.get("phase") == "installing":
                        self.master.after(
                            0,
                            lambda: self._set_update_status("Installation läuft — Admin-Dialog bestätigen…"),
                        )
                        result = borg_updates.poll_install_finished(timeout_sec=600.0)
            except Exception as exc:
                result = {"phase": "error", "error": str(exc)}

            def done() -> None:
                if result.get("phase") == "installed":
                    latest = result.get("latest_version") or "?"
                    self._set_update_status(f"Update v{latest} installiert — Neustart")
                    self._show_notice(
                        f"Update v{latest} installiert. Bitte App neu starten.",
                        level="success",
                        timeout_ms=15000,
                    )
                    if messagebox.askyesno(
                        "Update installiert",
                        f"Version {latest} wurde installiert.\n\nApp jetzt neu starten?",
                    ):
                        self._relaunch_after_update()
                else:
                    err = result.get("error") or "Update fehlgeschlagen"
                    self._set_update_status(f"Version {__version__} — {err}")
                    messagebox.showerror("Update", str(err))

            self.master.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    def _relaunch_after_update(self) -> None:
        """Quit current process and start the installed launcher after a short delay."""
        launcher = shutil.which("borg-backup-gui")
        if launcher:
            restart_cmd = f"sleep 0.9; exec {shlex.quote(launcher)}"
        else:
            restart_cmd = f"sleep 0.9; exec {shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))}"
        try:
            subprocess.Popen(
                ["bash", "-c", restart_cmd],
                start_new_session=True,
                cwd=str(Path.home()),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            messagebox.showerror(
                "Neustart",
                f"Automatischer Neustart fehlgeschlagen: {exc}\nBitte App manuell neu starten.",
            )
            return
        try:
            self.master.destroy()
        except Exception:
            pass
        raise SystemExit(0)

    def _maybe_show_first_run_wizard(self) -> None:
        if self.config_data.get("setup_complete"):
            return
        self._show_first_run_wizard()

    def _show_first_run_wizard(self) -> None:
        """Guided onboarding: destination type, passphrase, sources, privilege + trust."""
        dialog = tk.Toplevel(self.master)
        dialog.title("Erste Einrichtung — Borg Backup GUI")
        dialog.geometry("640x520")
        dialog.transient(self.master)
        dialog.grab_set()
        dialog.configure(bg=self.ui.get("bg", "#0b1220"))
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Willkommen", style="Title.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(
            frame,
            text="Full-System-Backups mit Canary-Check, Multi-Ziel und PDF-Protokoll.\n"
            "Dieser Assistent richtet das erste Ziel und die Quellen ein.",
            style="Muted.TLabel",
            wraplength=580,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 12))

        dest_type = tk.StringVar(value=self.config_data.get("profile_type", "ssh"))
        ttk.Label(frame, text="Zieltyp").grid(row=2, column=0, sticky="e", padx=5, pady=4)
        type_box = ttk.Combobox(
            frame, textvariable=dest_type, values=["ssh", "s3", "local"], state="readonly", width=18
        )
        type_box.grid(row=2, column=1, sticky="w", padx=5, pady=4)

        storage_var = tk.StringVar(value=self.config_data.get("storage", "") or self.config_data.get("local_path", ""))
        ttk.Label(frame, text="Repo / Pfad").grid(row=3, column=0, sticky="e", padx=5, pady=4)
        ttk.Entry(frame, textvariable=storage_var).grid(row=3, column=1, sticky="ew", padx=5, pady=4)

        ssh_key_var = tk.StringVar(value=self.config_data.get("ssh_key", ""))
        ttk.Label(frame, text="SSH-Key (optional)").grid(row=4, column=0, sticky="e", padx=5, pady=4)
        ttk.Entry(frame, textvariable=ssh_key_var).grid(row=4, column=1, sticky="ew", padx=5, pady=4)

        pass_var = tk.StringVar(value=self.config_data.get("borg_passphrase", ""))
        ttk.Label(frame, text="Passphrase").grid(row=5, column=0, sticky="e", padx=5, pady=4)
        ttk.Entry(frame, textvariable=pass_var, show="•").grid(row=5, column=1, sticky="ew", padx=5, pady=4)

        source_mode = tk.StringVar(value="system")
        ttk.Label(frame, text="Quellen").grid(row=6, column=0, sticky="ne", padx=5, pady=4)
        src_fr = ttk.Frame(frame)
        src_fr.grid(row=6, column=1, sticky="w", padx=5, pady=4)
        ttk.Radiobutton(src_fr, text="System Root (/) mit Auto-Excludes", variable=source_mode, value="system").pack(
            anchor="w"
        )
        ttk.Radiobutton(src_fr, text="Nur Home-Verzeichnis", variable=source_mode, value="home").pack(anchor="w")

        trust_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frame,
            text="Repo-Zugriff bestätigen (BORG_UNKNOWN / RELOCATED Flags für Init/Umzug)",
            variable=trust_var,
        ).grid(row=7, column=0, columnspan=2, sticky="w", padx=5, pady=8)

        ttk.Label(
            frame,
            text="Hinweis: Backups führt der Systemdienst borg-backup-guid mit root-Rechten aus.\n"
            "Konfiguration und Wiederherstellung fragen bei Bedarf nach dem Admin-Passwort (polkit).",
            style="Muted.TLabel",
        ).grid(row=8, column=0, columnspan=2, sticky="w", padx=5, pady=6)

        def _finish(skip: bool = False) -> None:
            if skip:
                self.config_data["setup_complete"] = True
                self._save_config()
                dialog.destroy()
                return
            ptype = dest_type.get().strip() or "ssh"
            storage = storage_var.get().strip()
            if not storage:
                messagebox.showerror("Einrichtung", "Bitte Repository oder Pfad angeben.", parent=dialog)
                return
            passphrase = pass_var.get()
            self.config_data["profile_type"] = ptype
            self.config_data["borg_passphrase"] = passphrase
            self.config_data["ssh_key"] = ssh_key_var.get().strip()
            self.config_data["repo_access_confirmed"] = bool(trust_var.get())
            if ptype == "local":
                self.config_data["local_path"] = storage
                self.config_data["storage"] = storage
            else:
                self.config_data["storage"] = storage

            if source_mode.get() == "home":
                home = str(Path.home())
                src = enrich_source(home, suggest_excludes_for_path(home))
                self.config_data["sources"] = [src]
            else:
                src = enrich_source("/", suggest_excludes_for_path("/"))
                self.config_data["sources"] = [src]
            sync_sources_to_flat(self.config_data)

            # Seed account/destination model for the chosen target
            acc_id = new_id("acc")
            dest_id = new_id("dest")
            account = {
                "id": acc_id,
                "name": f"{ptype.upper()} Account",
                "type": ptype,
                "ssh_key": self.config_data.get("ssh_key", ""),
                "passphrase": passphrase,
                "s3_access_key": self.config_data.get("s3_access_key", ""),
                "s3_secret_key": self.config_data.get("s3_secret_key", ""),
                "s3_endpoint_url": self.config_data.get("s3_endpoint_url", ""),
                "s3_region": self.config_data.get("s3_region", ""),
            }
            destination = {
                "id": dest_id,
                "name": "Hauptziel",
                "account_id": acc_id,
                "storage": storage,
                "local_path": storage if ptype == "local" else "",
            }
            self.config_data["accounts"] = [account]
            self.config_data["destinations"] = [destination]
            job = self._active_job()
            if job is not None:
                job["destination_ids"] = [dest_id]
                job["sources"] = list(self.config_data.get("sources") or [])
                sync_sources_to_flat(job)
            else:
                self.config_data["jobs"] = [
                    {
                        "name": self.config_data.get("profile_name", "Standard"),
                        "destination_ids": [dest_id],
                        "sources": list(self.config_data.get("sources") or []),
                        "include_folders": list(self.config_data.get("include_folders") or ["/"]),
                        "exclude_folders": list(self.config_data.get("exclude_folders") or []),
                        "compression": self.config_data.get("compression", "lz4"),
                        "schedule_type": "manual",
                    }
                ]

            self.config_data["setup_complete"] = True
            self._save_config()
            self._sync_config_to_ui()
            if hasattr(self, "_refresh_destinations_tree"):
                self._refresh_destinations_tree()
            if hasattr(self, "_refresh_sources_tree"):
                self._refresh_sources_tree()
            dialog.destroy()
            self._show_notice("Erste Einrichtung abgeschlossen.", level="success")
            self._push_config_if_changed(force=True)

        btns = ttk.Frame(frame)
        btns.grid(row=9, column=0, columnspan=2, sticky="ew", pady=16)
        ttk.Button(btns, text="Übernehmen", style="Accent.TButton", command=lambda: _finish(False)).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(btns, text="Später", command=lambda: _finish(True)).pack(side="left")


def main(argv: list[str] | None = None):
    args = list(sys.argv[1:] if argv is None else argv)
    start_hidden = "--tray" in args
    root = tk.Tk()
    BorgBackupGUI(root, start_hidden=start_hidden)
    root.mainloop()


if __name__ == "__main__":
    raise SystemExit(main())
