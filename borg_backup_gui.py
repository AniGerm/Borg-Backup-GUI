#!/usr/bin/env python3
"""
Borg Backup GUI - Komplettes Backup-Management mit Borg und Storage Box (SSH).
Funktioniert als root für vollständige Systemsicherungen.
"""

import datetime
import fcntl
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    from PIL import Image, ImageDraw
    import pystray
except Exception:
    Image = None
    ImageDraw = None
    pystray = None

try:
    from PIL import ImageTk
except Exception:
    ImageTk = None

try:
    import gi
    gi.require_version('AyatanaAppIndicator3', '0.1')
    gi.require_version('Gtk', '3.0')
    from gi.repository import AyatanaAppIndicator3, Gtk, GLib
except Exception:
    AyatanaAppIndicator3 = None
    Gtk = None
    GLib = None

# ============================================================
# KONFIGURATION
# ============================================================
CONFIG_DIR = Path.home() / '.config' / 'borg-backup-gui'
CONFIG_FILE = CONFIG_DIR / 'config.json'
STATUS_FILE = CONFIG_DIR / 'status.json'
INSTANCE_LOCK_FILE = CONFIG_DIR / 'instance.lock'
INSTANCE_SOCKET_FILE = CONFIG_DIR / 'instance.sock'
ASSET_ICON_FILE = Path(__file__).resolve().parent / 'assets' / 'borg-backup-gui.svg'
# Fallback auf altes Icon
if not ASSET_ICON_FILE.exists():
    ASSET_ICON_FILE = Path(__file__).resolve().parent / 'assets' / 'hetzner-borg-gui.svg'
DEFAULT_STORAGE = 'uXXXXXX@uXXXXXX.your-storagebox.de:./backup'
BORG_BIN = shutil.which('borg') or 'borg'
INSTANCE_LOCK_HANDLE = None

CANARY_FILE = '/var/tmp/borg-canary-check.txt'

# Alte Config-Pfade (für Migration)
OLD_CONFIG_DIR = Path.home() / '.config' / 'hetzner-borg-gui'
OLD_CONFIG_FILE = OLD_CONFIG_DIR / 'config.json'
OLD_STATUS_FILE = OLD_CONFIG_DIR / 'status.json'


def _migrate_old_config():
    """Kopiert alte Konfiguration einmalig ins neue Verzeichnis."""
    if CONFIG_FILE.exists():
        return  # Neue Config existiert bereits
    if not OLD_CONFIG_FILE.exists():
        return  # Keine alte Config zum Migrieren
    
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        import shutil as _shutil
        _shutil.copy2(str(OLD_CONFIG_FILE), str(CONFIG_FILE))
        if OLD_STATUS_FILE.exists():
            _shutil.copy2(str(OLD_STATUS_FILE), str(STATUS_FILE))
    except Exception:
        pass  # Migration fehlgeschlagen → startet mit Defaults


def _activate_existing_instance():
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(1.0)
        client.connect(str(INSTANCE_SOCKET_FILE))
        client.sendall(b'SHOW\n')
        client.close()
        return True
    except OSError:
        return False


# ============================================================
# HELFERKLASSE FUER LIVE-OUTPUT
# ============================================================
class CommandRunner:
    """Führt Befehle im Thread aus und liefert Logs thread-sicher an die GUI."""

    def __init__(self, append_callback, schedule_main):
        self.append_callback = append_callback
        self.schedule_main = schedule_main
        self.process = None
        self.stop_flag = False

    def append_log(self, text):
        self.append_callback(text)

    def run(self, cmd, done_callback=None, env=None, cwd=None):
        """Befehl in Hintergrund-Thread starten."""

        def target():
            result = {
                'exit_code': -1,
                'stopped': False,
                'exception': ''
            }
            try:
                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    universal_newlines=True,
                    env=env if env else os.environ,
                    cwd=cwd
                )

                for line in self.process.stdout:
                    if self.stop_flag:
                        self.process.terminate()
                        result['stopped'] = True
                        break
                    self.append_log(line)

                exit_code = self.process.wait()
                result['exit_code'] = exit_code

                if exit_code == 1 and not self.stop_flag:
                    self.append_log(f'\n[WARNUNG] Befehl endete mit Code {exit_code}\n')
                elif exit_code != 0 and not self.stop_flag:
                    self.append_log(f'\n[FEHLER] Befehl endete mit Code {exit_code}\n')
                elif exit_code == 0:
                    self.append_log('\n[OK] Fertig!\n')
            except Exception as exc:
                result['exception'] = str(exc)
                self.append_log(f'\n[EXCEPTION] {exc}\n')
            finally:
                if done_callback:
                    self.schedule_main(lambda: done_callback(result))

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        return thread

    def run_capture(self, cmd, done_callback=None, env=None, cwd=None, timeout=120):
        """Befehl in Hintergrund-Thread starten, stdout/stderr sammeln, kein Live-Log."""

        def target():
            result = {
                'exit_code': -1,
                'stopped': False,
                'exception': '',
                'stdout': '',
                'stderr': ''
            }
            try:
                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env if env else os.environ,
                    cwd=cwd
                )
                try:
                    out, err = self.process.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.communicate()
                    result['exception'] = f'Zeitüberschreitung nach {timeout}s – Server nicht erreichbar?'
                    return
                result['stdout'] = out
                result['stderr'] = err
                result['exit_code'] = self.process.returncode
            except Exception as exc:
                result['exception'] = str(exc)
            finally:
                if done_callback:
                    self.schedule_main(lambda: done_callback(result))

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        return thread

    def stop(self):
        self.stop_flag = True
        if self.process and self.process.poll() is None:
            self.process.terminate()


# ============================================================
# HAUPT-GUI-KLASSE
# ============================================================
class BorgBackupGUI:
    def __init__(self, master):
        self.master = master
        try:
            master.tk.call('tk', 'appname', 'BorgBackupGUI')
        except Exception:
            pass
        try:
            master.wm_class('BorgBackupGUI')
        except Exception:
            pass
        master.title('Borg Backup GUI')
        master.geometry('980x760')
        master.configure(bg='#2e2e2e')

        self.style = ttk.Style()
        self.style.theme_use('clam')
        # Modernere, weichere Farben und großzügigeres Padding
        bg_color = '#1f2937'
        fg_color = '#f8fafc'
        acc_color = '#0f766e'
        btn_color = '#374151'
        
        self.master.configure(bg=bg_color)
        
        # Dropdown-Menü Farben in Menüs (inkl Combobox Dropdown) global festlegen
        self.master.option_add('*TCombobox*Listbox.background', '#374151')
        self.master.option_add('*TCombobox*Listbox.foreground', '#f8fafc')
        self.master.option_add('*TCombobox*Listbox.selectBackground', '#0f766e')
        self.master.option_add('*TCombobox*Listbox.selectForeground', '#ffffff')
        self.master.option_add('*Listbox.background', '#374151')
        self.master.option_add('*Listbox.foreground', '#f8fafc')
        self.master.option_add('*Listbox.selectBackground', '#0f766e')
        self.master.option_add('*Listbox.selectForeground', '#ffffff')

        self.style.configure('.', background=bg_color, foreground=fg_color)
        
        self.style.configure('TNotebook', background=bg_color, borderwidth=0)
        self.style.configure('TNotebook.Tab', padding=[20, 8], font=('Segoe UI', 10, 'bold'), background='#374151', foreground=fg_color, borderwidth=0)
        self.style.map('TNotebook.Tab', background=[('selected', acc_color)], foreground=[('selected', '#ffffff')])
        
        self.style.configure('TFrame', background=bg_color)
        self.style.configure('TLabel', background=bg_color, foreground=fg_color, font=('Segoe UI', 10))
        
        self.style.configure('TButton', font=('Segoe UI', 10, 'bold'), padding=8, background=btn_color, foreground=fg_color, borderwidth=0)
        self.style.map('TButton', background=[('active', acc_color), ('disabled', '#475569')])
        
        self.style.configure('TEntry', fieldbackground='#374151', foreground=fg_color, insertcolor=fg_color, padding=5, borderwidth=0)
        self.style.configure('TSpinbox', fieldbackground='#374151', foreground=fg_color, background=btn_color, arrowcolor=fg_color, insertcolor=fg_color, padding=4)
        self.style.map('TSpinbox', fieldbackground=[('readonly', '#374151'), ('!disabled', '#374151')], foreground=[('readonly', fg_color), ('!disabled', fg_color)])
        
        self.style.configure('TCombobox', fieldbackground='#374151', foreground=fg_color, background=btn_color)
        self.style.map('TCombobox', fieldbackground=[('readonly', '#374151'), ('!readonly', '#374151')], foreground=[('readonly', fg_color), ('!readonly', fg_color)], selectbackground=[('readonly', acc_color)])

        self.style.configure('TLabelframe', background=bg_color, foreground=fg_color, borderwidth=1, bordercolor='#475569')
        self.style.configure('TLabelframe.Label', background=bg_color, foreground=acc_color, font=('Segoe UI', 11, 'bold'))
        self.style.configure('Treeview', background='#374151', foreground=fg_color, fieldbackground='#374151', borderwidth=0, rowheight=25)
        self.style.map('Treeview', background=[('selected', acc_color)])
        self.style.configure('Treeview.Heading', background=btn_color, foreground=fg_color, font=('Segoe UI', 10, 'bold'), padding=5)

        self.config_data = self._load_config()
        self._init_profiles_from_config()
        self.status_data = self._load_status()

        self.runner = None
        self.restore_runner = None
        self.log_widget = None
        self.restore_log = None

        self.log_queue = queue.Queue()
        self.scheduled_job_id = None
        self.next_backup_dt = None
        self.backup_running = False
        self.current_backup_started_at = None
        self.backup_files_seen = 0
        self.backup_last_item = ''
        self.backup_task_queue = []
        self.backup_current_task = None
        self.archive_list_refresh_pending = False
        self.tray_icon = None
        self.native_indicator = None
        self.native_indicator_thread = None
        self.native_indicator_supported = bool(AyatanaAppIndicator3 and Gtk)
        self.native_indicator_failed = False
        self.tray_supported = bool(pystray and Image and ImageDraw)
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

        self.notebook = ttk.Notebook(master)
        self.notebook.pack(fill='both', expand=True, padx=5, pady=5)

        self.notice_var = tk.StringVar(value='')
        self.notice_label = tk.Label(
            master,
            textvariable=self.notice_var,
            anchor='w',
            bg='#111827',
            fg='#cbd5e1',
            padx=10,
            pady=6
        )
        self.notice_label.pack(fill='x', side='bottom')

        self.tab_dashboard = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_dashboard, text='Start')
        self._build_dashboard_tab()

        self.tab_backup = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_backup, text='Backup')
        self._build_backup_tab()

        self.tab_archive = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_archive, text='Archiv und Restore')
        self._build_archive_tab()

        self.tab_schedule = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_schedule, text='Zeitplan & Wartung')
        self._build_schedule_tab()

        self.tab_restore = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_restore, text='Restore')
        self._build_restore_tab()

        self.runner = CommandRunner(
            append_callback=self._append_backup_log,
            schedule_main=lambda cb: self.master.after(0, cb)
        )
        self.restore_runner = CommandRunner(
            append_callback=self._append_restore_log,
            schedule_main=lambda cb: self.master.after(0, cb)
        )
        self.archive_runner = CommandRunner(
            append_callback=self._append_backup_log,
            schedule_main=lambda cb: self.master.after(0, cb)
        )

        self.master.after(100, self._process_log_queue)
        self._schedule_next_backup()
        self._update_dashboard_status()
        self.master.after(1200, self._refresh_dashboard_status)
        # Tray leicht verzögert starten, damit Tk/DBus/Panel stabil initialisiert sind.
        self.master.after(700, self._setup_tray_icon)
        self.master.after(60000, self._periodic_dashboard_refresh)
        self.master.protocol('WM_DELETE_WINDOW', self._on_close)
        
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
        self.master.after(120, self._ensure_window_visible)
        self.master.after(30000, self._periodic_tray_health_check)
        # Profil-Dropdown initialisieren
        self.master.after(500, self._refresh_profile_combo)
        self.master.after(600, self._refresh_archive_profile_combo)

        if os.geteuid() == 0:
            messagebox.showwarning(
                'Hinweis',
                'Die GUI sollte als normaler User laufen, damit das Tray-Symbol sichtbar bleibt.\n\n'
                'Root-Rechte werden bei Borg-Befehlen automatisch angefordert.'
            )

    def _on_close(self):
        tray_enabled = bool(self.config_data.get('tray_enabled', True))

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
                with open(CONFIG_FILE, 'r') as file_handle:
                    data = json.load(file_handle)
                    # Migration: Historische Eingaben wie "user@host:23/./backup" korrigieren,
                    # da der Port bereits über BORG_RSH gesetzt wird.
                    storage = str(data.get('storage', '')).strip()
                    if '://' not in storage and ':' in storage:
                        host_part, path_part = storage.split(':', 1)
                        if path_part.startswith('23/'):
                            data['storage'] = f'{host_part}:{path_part[3:]}'
                    return self._ensure_profile_keys(data)
        except Exception:
            pass

        return self._ensure_profile_keys({
            'storage': DEFAULT_STORAGE,
            'ssh_key': '',
            'include_folders': ['/'],
            'exclude_folders': [
                '/dev', '/proc', '/sys', '/tmp', '/run',
                '/mnt', '/media', '/lost+found',
                '/var/cache/apt/archives', '/var/tmp',
                '/home/*/.vagrant.d', '/home/*/snap', '/home/*/.local/share/Trash'
            ],
            'compression': 'lz4',
            'encryption': 'repokey',
                        'borg_passphrase': '',
            'schedule_type': 'manual',
            'schedule_interval': 3,
            'schedule_time': '03:15',
            'catchup_missed': True,
            'prune_enabled': False,
            'validate_enabled': True,
            'validate_interval': 3,
            'optimize_enabled': False,
            'optimize_interval': 3,
            'promptless_privilege': True,
            'tray_enabled': True,
            'tray_icon_style': 'disk',
            'privilege_mode': 'auto'
        })

    @staticmethod
    def _ensure_profile_keys(data):
        """Stellt sicher, dass Profil-Keys vorhanden sind (abwärtskompatibel)."""
        data.setdefault('profile_type', 'ssh')
        data.setdefault('profile_name', 'Standard')
        data.setdefault('profiles', [])
        data.setdefault('s3_access_key', '')
        data.setdefault('s3_secret_key', '')
        data.setdefault('s3_endpoint_url', 'https://fsn1.your-objectstorage.com')
        data.setdefault('s3_region', 'fsn1')
        data.setdefault('local_path', '')
        data.setdefault('canary_enabled', True)
        data.setdefault('canary_last_check', '')
        data.setdefault('canary_last_result', '')
        return data

    def _load_status(self):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            if STATUS_FILE.exists():
                with open(STATUS_FILE, 'r') as file_handle:
                    data = json.load(file_handle)
                    data.setdefault('last_run_at', '')
                    data.setdefault('last_success_at', '')
                    data.setdefault('last_exit_code', None)
                    data.setdefault('last_error', '')
                    data.setdefault('last_duration_sec', None)
                    data.setdefault('last_files_count', 0)
                    data.setdefault('last_source_size', '')
                    data.setdefault('last_dedupe_size', '')
                    data.setdefault('last_item', '')
                    return data
        except Exception:
            pass

        return {
            'last_run_at': '',
            'last_success_at': '',
            'last_exit_code': None,
            'last_error': '',
            'last_duration_sec': None,
            'last_files_count': 0,
            'last_source_size': '',
            'last_dedupe_size': '',
            'last_item': ''
        }

    def _collect_config_from_ui(self):
        if hasattr(self, 'storage_var'):
            self.config_data['storage'] = self.storage_var.get().strip()
        if hasattr(self, 's3_storage_var') and self.config_data.get('profile_type') == 's3':
            self.config_data['storage'] = self.s3_storage_var.get().strip()
        if hasattr(self, 'local_path_var') and self.config_data.get('profile_type') == 'local':
            self.config_data['local_path'] = self.local_path_var.get().strip()
            self.config_data['storage'] = self.local_path_var.get().strip()
        if hasattr(self, 'ssh_key_var'):
            self.config_data['ssh_key'] = self.ssh_key_var.get().strip()
        if hasattr(self, 'passphrase_var'):
            self.config_data['borg_passphrase'] = self.passphrase_var.get()
        if hasattr(self, 'compression_var'):
            self.config_data['compression'] = self.compression_var.get().strip() or 'lz4'
        # S3-spezifisch
        if hasattr(self, 's3_access_var'):
            self.config_data['s3_access_key'] = self.s3_access_var.get().strip()
        if hasattr(self, 's3_secret_var'):
            self.config_data['s3_secret_key'] = self.s3_secret_var.get()
        if hasattr(self, 's3_endpoint_var'):
            self.config_data['s3_endpoint_url'] = self.s3_endpoint_var.get().strip()
        if hasattr(self, 'include_text'):
            includes = [line.strip() for line in self.include_text.get('1.0', tk.END).splitlines() if line.strip()]
            self.config_data['include_folders'] = includes or ['/']
        if hasattr(self, 'exclude_text'):
            excludes = [line.strip() for line in self.exclude_text.get('1.0', tk.END).splitlines() if line.strip()]
            self.config_data['exclude_folders'] = excludes
        if hasattr(self, 'schedule_type_var'):
            self.config_data['schedule_type'] = self.schedule_type_var.get()
            self.config_data['schedule_interval'] = int(self.schedule_interval_var.get() or 3)
            self.config_data['schedule_time'] = self.schedule_time_var.get().strip()
            self.config_data['catchup_missed'] = bool(self.catchup_var.get())
            self.config_data['prune_enabled'] = bool(self.prune_var.get())
            self.config_data['validate_enabled'] = bool(self.validate_var.get())
            self.config_data['validate_interval'] = int(self.validate_interval_var.get() or 3)
            self.config_data['optimize_enabled'] = bool(self.optimize_var.get())
            self.config_data['optimize_interval'] = int(self.optimize_interval_var.get() or 3)
        if hasattr(self, 'tray_enabled_var'):
            self.config_data['tray_enabled'] = bool(self.tray_enabled_var.get())
        if hasattr(self, 'tray_icon_style_var'):
            self.config_data['tray_icon_style'] = self.tray_icon_style_var.get().strip() or 'disk'

    def _save_config(self):
        try:
            self._collect_config_from_ui()
            # Aktuelles Profil in der Liste aktualisieren
            if hasattr(self, '_update_current_profile_in_list'):
                self._update_current_profile_in_list()
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with open(CONFIG_FILE, 'w') as file_handle:
                json.dump(self.config_data, file_handle, indent=4)
        except Exception as exc:
            messagebox.showerror('Fehler', f'Konnte Konfiguration nicht speichern: {exc}')

    def _save_status(self):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with open(STATUS_FILE, 'w') as file_handle:
                json.dump(self.status_data, file_handle, indent=4)
        except Exception as exc:
            messagebox.showerror('Fehler', f'Konnte Status nicht speichern: {exc}')

    
    def _build_schedule_tab(self):
        tab = self.tab_schedule
        
        sched_frame = ttk.LabelFrame(tab, text='Backup-Zeitplan')
        sched_frame.pack(fill='x', padx=10, pady=10)
        
        self.schedule_type_var = tk.StringVar(value=self.config_data.get('schedule_type', 'manual'))
        
        ttk.Radiobutton(sched_frame, text='Nur manuell', variable=self.schedule_type_var, value='manual', command=self._on_schedule_change).grid(row=0, column=0, sticky='w', padx=5, pady=5)
        ttk.Radiobutton(sched_frame, text='Regelmäßiges Backup', variable=self.schedule_type_var, value='interval', command=self._on_schedule_change).grid(row=1, column=0, sticky='w', padx=5, pady=5)
        
        frame_interval = ttk.Frame(sched_frame)
        frame_interval.grid(row=1, column=1, sticky='w')
        ttk.Label(frame_interval, text='Intervall:').pack(side='left', padx=5)
        self.schedule_interval_var = tk.StringVar(value=str(self.config_data.get('schedule_interval', 3)))
        ttk.Spinbox(frame_interval, from_=1, to=168, textvariable=self.schedule_interval_var, width=5).pack(side='left')
        ttk.Label(frame_interval, text='Stunden').pack(side='left', padx=5)
        
        ttk.Radiobutton(sched_frame, text='Tägliches Backup', variable=self.schedule_type_var, value='daily', command=self._on_schedule_change).grid(row=2, column=0, sticky='w', padx=5, pady=5)
        
        frame_daily = ttk.Frame(sched_frame)
        frame_daily.grid(row=2, column=1, sticky='w')
        ttk.Label(frame_daily, text='Uhrzeit:').pack(side='left', padx=5)
        self.schedule_time_var = tk.StringVar(value=self.config_data.get('schedule_time', '03:15'))
        ttk.Entry(frame_daily, textvariable=self.schedule_time_var, width=8).pack(side='left')
        
        self.catchup_var = tk.BooleanVar(value=bool(self.config_data.get('catchup_missed', True)))
        ttk.Checkbutton(sched_frame, text='Verpasste Backups nachholen', variable=self.catchup_var).grid(row=3, column=0, columnspan=2, sticky='w', padx=5, pady=10)

        maint_frame = ttk.LabelFrame(tab, text='Wartung nach dem Backup (Vorta-Feature)')
        maint_frame.pack(fill='x', padx=10, pady=10)

        self.prune_var = tk.BooleanVar(value=bool(self.config_data.get('prune_enabled', False)))
        ttk.Checkbutton(maint_frame, text='Ausdünnen: Nach jedem Backup', variable=self.prune_var).grid(row=0, column=0, sticky='w', padx=5, pady=5)

        self.validate_var = tk.BooleanVar(value=bool(self.config_data.get('validate_enabled', True)))
        ttk.Checkbutton(maint_frame, text='Validierung: Repositorydaten überprüfen', variable=self.validate_var).grid(row=1, column=0, sticky='w', padx=5, pady=5)
        
        frame_val = ttk.Frame(maint_frame)
        frame_val.grid(row=1, column=1, sticky='w')
        ttk.Label(frame_val, text='Intervall:').pack(side='left', padx=5)
        self.validate_interval_var = tk.StringVar(value=str(self.config_data.get('validate_interval', 3)))
        ttk.Spinbox(frame_val, from_=1, to=52, textvariable=self.validate_interval_var, width=5).pack(side='left')
        ttk.Label(frame_val, text='Wochen').pack(side='left', padx=5)

        self.optimize_var = tk.BooleanVar(value=bool(self.config_data.get('optimize_enabled', False)))
        ttk.Checkbutton(maint_frame, text='Optimierung: Repository optimieren', variable=self.optimize_var).grid(row=2, column=0, sticky='w', padx=5, pady=5)
        
        frame_opt = ttk.Frame(maint_frame)
        frame_opt.grid(row=2, column=1, sticky='w')
        ttk.Label(frame_opt, text='Intervall:').pack(side='left', padx=5)
        self.optimize_interval_var = tk.StringVar(value=str(self.config_data.get('optimize_interval', 3)))
        ttk.Spinbox(frame_opt, from_=1, to=52, textvariable=self.optimize_interval_var, width=5).pack(side='left')
        ttk.Label(frame_opt, text='Wochen').pack(side='left', padx=5)
        
        ttk.Button(tab, text='Einstellungen speichern & anwenden', command=self._on_schedule_change).pack(pady=10)

    def _build_dashboard_tab(self):
        tab = self.tab_dashboard
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        ttk.Label(tab, text='Backup Übersicht', font=('Arial', 16, 'bold')).grid(
            row=0, column=0, sticky='w', padx=12, pady=(12, 6)
        )

        status_frame = ttk.LabelFrame(tab, text='Aktueller Status')
        status_frame.grid(row=1, column=0, sticky='nsew', padx=12, pady=8)
        status_frame.columnconfigure(1, weight=1)

        self.status_canvas = tk.Canvas(status_frame, width=52, height=52, bg='#2e2e2e', highlightthickness=0)
        self.status_canvas.grid(row=0, column=0, rowspan=4, padx=8, pady=8, sticky='n')
        self.status_light = self.status_canvas.create_oval(6, 6, 46, 46, fill='#6e6e6e', outline='')

        self.status_title_var = tk.StringVar(value='Noch kein Backup ausgeführt')
        ttk.Label(status_frame, textvariable=self.status_title_var, font=('Arial', 12, 'bold')).grid(
            row=0, column=1, sticky='w', padx=6, pady=(8, 4)
        )

        self.last_backup_var = tk.StringVar(value='Letzter Lauf: noch nie')
        ttk.Label(status_frame, textvariable=self.last_backup_var).grid(row=1, column=1, sticky='w', padx=6, pady=2)

        self.next_backup_var = tk.StringVar(value='Nächstes geplantes Backup: deaktiviert')
        ttk.Label(status_frame, textvariable=self.next_backup_var).grid(row=2, column=1, sticky='w', padx=6, pady=2)

        self.last_error_var = tk.StringVar(value='Letzte Meldung: noch keine')
        ttk.Label(status_frame, textvariable=self.last_error_var, wraplength=760).grid(
            row=3, column=1, sticky='w', padx=6, pady=(2, 8)
        )

        self.canary_status_var = tk.StringVar(value='Canary: noch kein Check')
        ttk.Label(status_frame, textvariable=self.canary_status_var, font=('Arial', 9)).grid(
            row=4, column=1, sticky='w', padx=6, pady=(0, 6)
        )

        action_frame = ttk.LabelFrame(tab, text='Schnellaktionen')
        action_frame.grid(row=2, column=0, sticky='ew', padx=12, pady=8)
        ttk.Button(action_frame, text='Backup jetzt starten', command=self._start_backup).pack(side='left', padx=8, pady=8)
        ttk.Button(action_frame, text='Status aktualisieren', command=self._refresh_dashboard_status).pack(
            side='left', padx=8, pady=8
        )

        stats_frame = ttk.LabelFrame(tab, text='Backup-Statistik')
        stats_frame.grid(row=3, column=0, sticky='ew', padx=12, pady=8)
        stats_frame.columnconfigure(1, weight=1)

        self.backup_files_var = tk.StringVar(value='Gefundene/verarbeitete Dateien: 0')
        self.backup_source_size_var = tk.StringVar(value='Quellgröße: noch keine Daten')
        self.backup_dedupe_size_var = tk.StringVar(value='Dedupliziert: noch keine Daten')
        self.backup_last_item_var = tk.StringVar(value='Zuletzt: noch keine Daten')

        ttk.Label(stats_frame, textvariable=self.backup_files_var).grid(row=0, column=0, sticky='w', padx=8, pady=(8, 2))
        ttk.Label(stats_frame, textvariable=self.backup_source_size_var).grid(row=0, column=1, sticky='w', padx=8, pady=(8, 2))
        ttk.Label(stats_frame, textvariable=self.backup_dedupe_size_var).grid(row=1, column=0, sticky='w', padx=8, pady=(2, 8))
        ttk.Label(stats_frame, textvariable=self.backup_last_item_var, wraplength=720).grid(row=1, column=1, sticky='w', padx=8, pady=(2, 8))

        

        tray_frame = ttk.LabelFrame(tab, text='Hintergrundsymbol in der Statusleiste')
        tray_frame.grid(row=4, column=0, sticky='ew', padx=12, pady=(0, 12))
        tray_frame.columnconfigure(3, weight=1)

        self.tray_enabled_var = tk.BooleanVar(value=bool(self.config_data.get('tray_enabled', True)))
        tray_check = ttk.Checkbutton(
            tray_frame,
            text='Tray-Symbol aktivieren (im Hintergrund laufen)',
            variable=self.tray_enabled_var,
            command=self._on_tray_settings_change
        )
        tray_check.grid(row=0, column=0, padx=8, pady=8, sticky='w')

        ttk.Label(tray_frame, text='Icon-Stil:').grid(row=0, column=1, padx=(8, 4), pady=8, sticky='e')
        self.tray_icon_style_var = tk.StringVar(value=self.config_data.get('tray_icon_style', 'disk'))
        tray_style_combo = ttk.Combobox(
            tray_frame,
            textvariable=self.tray_icon_style_var,
            values=['disk', 'shield', 'palm'],
            state='readonly',
            width=10
        )
        tray_style_combo.grid(row=0, column=2, padx=4, pady=8, sticky='w')

        tray_apply_btn = ttk.Button(tray_frame, text='Tray anwenden', command=self._on_tray_settings_change)
        tray_apply_btn.grid(
            row=0, column=3, padx=8, pady=8, sticky='w'
        )

        ttk.Label(
            tray_frame,
            text='Schliessen minimiert in den Tray. Beenden erfolgt über das Tray-Menü.',
            font=('Arial', 9)
        ).grid(row=1, column=0, columnspan=4, sticky='w', padx=8, pady=(0, 8))

        if not self.tray_supported:
            tray_check.state(['disabled'])
            tray_style_combo.state(['disabled'])
            tray_apply_btn.state(['disabled'])
            ttk.Label(
                tray_frame,
                text='Tray ist deaktiviert: Bitte pystray und pillow installieren (pip install pystray pillow).',
                font=('Arial', 9)
            ).grid(row=2, column=0, columnspan=4, sticky='w', padx=8, pady=(0, 8))
        elif self.is_root:
            tray_check.state(['disabled'])
            tray_style_combo.state(['disabled'])
            tray_apply_btn.state(['disabled'])
            ttk.Label(
                tray_frame,
                text='Tray ist im root-Modus deaktiviert. Starte die GUI als normaler User.',
                font=('Arial', 9)
            ).grid(row=2, column=0, columnspan=4, sticky='w', padx=8, pady=(0, 8))


    # ============================================================
    # PROFIL-VERWALTUNG
    # ============================================================

    def _init_profiles_from_config(self):
        """Stellt sicher, dass profiles-Liste mindestens ein Profil enthält."""
        profiles = self.config_data.get('profiles', [])
        if not profiles:
            # Ein Profil aus den flachen Config-Keys bauen
            profile = {
                'name': 'Standard',
                'type': self.config_data.get('profile_type', 'ssh'),
                'storage': self.config_data.get('storage', ''),
                'ssh_key': self.config_data.get('ssh_key', ''),
                'passphrase': self.config_data.get('borg_passphrase', ''),
                'compression': self.config_data.get('compression', 'lz4'),
                'include_folders': self.config_data.get('include_folders', ['/']),
                'exclude_folders': self.config_data.get('exclude_folders', []),
                'schedule_type': self.config_data.get('schedule_type', 'manual'),
                'schedule_interval': self.config_data.get('schedule_interval', 3),
                'schedule_time': self.config_data.get('schedule_time', '03:15'),
                'catchup_missed': self.config_data.get('catchup_missed', True),
                'prune_enabled': self.config_data.get('prune_enabled', False),
                'validate_enabled': self.config_data.get('validate_enabled', True),
                'validate_interval': self.config_data.get('validate_interval', 3),
                'optimize_enabled': self.config_data.get('optimize_enabled', False),
                'optimize_interval': self.config_data.get('optimize_interval', 3),
                's3_access_key': self.config_data.get('s3_access_key', ''),
                's3_secret_key': self.config_data.get('s3_secret_key', ''),
                's3_endpoint_url': self.config_data.get('s3_endpoint_url', ''),
                's3_region': self.config_data.get('s3_region', ''),
                'local_path': self.config_data.get('local_path', ''),
            }
            profiles.append(profile)
            self.config_data['profiles'] = profiles
            self.config_data['profile_name'] = 'Standard'
            self.config_data['profile_type'] = 'ssh'

    def _refresh_profile_combo(self):
        """Aktualisiert die Profile-Combo im Backup-Tab."""
        if not hasattr(self, 'profile_combo'):
            return
        profiles = self.config_data.get('profiles', [])
        names = [p['name'] for p in profiles]
        self.profile_combo['values'] = names
        current = self.config_data.get('profile_name', '')
        if current in names:
            self.profile_combo.set(current)
        elif names:
            self.profile_combo.set(names[0])

    def _apply_profile(self, name):
        """Lädt ein Profil in die aktive Config und UI."""
        profiles = self.config_data.get('profiles', [])
        for p in profiles:
            if p['name'] == name:
                self.config_data['profile_name'] = p['name']
                self.config_data['profile_type'] = p.get('type', 'ssh')
                self.config_data['storage'] = p.get('storage', '')
                self.config_data['ssh_key'] = p.get('ssh_key', '')
                self.config_data['borg_passphrase'] = p.get('passphrase', '')
                self.config_data['compression'] = p.get('compression', 'lz4')
                self.config_data['include_folders'] = p.get('include_folders', ['/'])
                self.config_data['exclude_folders'] = p.get('exclude_folders', [])
                self.config_data['schedule_type'] = p.get('schedule_type', 'manual')
                self.config_data['schedule_interval'] = p.get('schedule_interval', 3)
                self.config_data['schedule_time'] = p.get('schedule_time', '03:15')
                self.config_data['catchup_missed'] = p.get('catchup_missed', True)
                self.config_data['prune_enabled'] = p.get('prune_enabled', False)
                self.config_data['validate_enabled'] = p.get('validate_enabled', True)
                self.config_data['validate_interval'] = p.get('validate_interval', 3)
                self.config_data['optimize_enabled'] = p.get('optimize_enabled', False)
                self.config_data['optimize_interval'] = p.get('optimize_interval', 3)
                self.config_data['s3_access_key'] = p.get('s3_access_key', '')
                self.config_data['s3_secret_key'] = p.get('s3_secret_key', '')
                self.config_data['s3_endpoint_url'] = p.get('s3_endpoint_url', '')
                self.config_data['s3_region'] = p.get('s3_region', '')
                self.config_data['local_path'] = p.get('local_path', '')
                self._sync_config_to_ui()
                self._refresh_profile_combo()
                return

    def _sync_config_to_ui(self):
        """Überträgt config_data in die UI-Variablen (nach Profilwechsel)."""
        if hasattr(self, 'storage_var'):
            self.storage_var.set(self.config_data.get('storage', ''))
        if hasattr(self, 'ssh_key_var'):
            self.ssh_key_var.set(self.config_data.get('ssh_key', ''))
        if hasattr(self, 'passphrase_var'):
            self.passphrase_var.set(self.config_data.get('borg_passphrase', ''))
        if hasattr(self, 'compression_var'):
            self.compression_var.set(self.config_data.get('compression', 'lz4'))
        # S3/Local fields
        if hasattr(self, 's3_storage_var'):
            self.s3_storage_var.set(self.config_data.get('storage', ''))
        if hasattr(self, 's3_access_var'):
            self.s3_access_var.set(self.config_data.get('s3_access_key', ''))
        if hasattr(self, 's3_secret_var'):
            self.s3_secret_var.set(self.config_data.get('s3_secret_key', ''))
        if hasattr(self, 's3_endpoint_var'):
            self.s3_endpoint_var.set(self.config_data.get('s3_endpoint_url', ''))
        if hasattr(self, 'local_path_var'):
            self.local_path_var.set(self.config_data.get('local_path', ''))
        # Backend-Felder ein-/ausblenden
        self._update_backend_fields()
        if hasattr(self, 'schedule_type_var'):
            self.schedule_type_var.set(self.config_data.get('schedule_type', 'manual'))
            self.schedule_interval_var.set(str(self.config_data.get('schedule_interval', 3)))
            self.schedule_time_var.set(self.config_data.get('schedule_time', '03:15'))
            self.catchup_var.set(bool(self.config_data.get('catchup_missed', True)))
            self.prune_var.set(bool(self.config_data.get('prune_enabled', False)))
            self.validate_var.set(bool(self.config_data.get('validate_enabled', True)))
            self.validate_interval_var.set(str(self.config_data.get('validate_interval', 3)))
            self.optimize_var.set(bool(self.config_data.get('optimize_enabled', False)))
            self.optimize_interval_var.set(str(self.config_data.get('optimize_interval', 3)))
        # Include/Exclude Texts
        if hasattr(self, 'include_text'):
            self.include_text.delete('1.0', tk.END)
            inc = self.config_data.get('include_folders', ['/'])
            self.include_text.insert(tk.END, '\n'.join(inc))
        if hasattr(self, 'exclude_text'):
            self.exclude_text.delete('1.0', tk.END)
            exc = self.config_data.get('exclude_folders', [])
            self.exclude_text.insert(tk.END, '\n'.join(exc))
        if hasattr(self, 'profile_combo'):
            self._refresh_profile_combo()

    def _update_current_profile_in_list(self):
        """Speichert aktuelle UI-Werte zurück ins aktive Profil."""
        profiles = self.config_data.get('profiles', [])
        name = self.config_data.get('profile_name', '')
        for i, p in enumerate(profiles):
            if p['name'] == name:
                profiles[i]['storage'] = self.config_data.get('storage', '')
                profiles[i]['ssh_key'] = self.config_data.get('ssh_key', '')
                profiles[i]['passphrase'] = self.config_data.get('borg_passphrase', '')
                profiles[i]['compression'] = self.config_data.get('compression', 'lz4')
                profiles[i]['include_folders'] = self.config_data.get('include_folders', ['/'])
                profiles[i]['exclude_folders'] = self.config_data.get('exclude_folders', [])
                profiles[i]['schedule_type'] = self.config_data.get('schedule_type', 'manual')
                profiles[i]['schedule_interval'] = self.config_data.get('schedule_interval', 3)
                profiles[i]['schedule_time'] = self.config_data.get('schedule_time', '03:15')
                profiles[i]['catchup_missed'] = self.config_data.get('catchup_missed', True)
                profiles[i]['prune_enabled'] = self.config_data.get('prune_enabled', False)
                profiles[i]['validate_enabled'] = self.config_data.get('validate_enabled', True)
                profiles[i]['validate_interval'] = self.config_data.get('validate_interval', 3)
                profiles[i]['optimize_enabled'] = self.config_data.get('optimize_enabled', False)
                profiles[i]['optimize_interval'] = self.config_data.get('optimize_interval', 3)
                profiles[i]['s3_access_key'] = self.config_data.get('s3_access_key', '')
                profiles[i]['s3_secret_key'] = self.config_data.get('s3_secret_key', '')
                profiles[i]['s3_endpoint_url'] = self.config_data.get('s3_endpoint_url', '')
                profiles[i]['s3_region'] = self.config_data.get('s3_region', '')
                profiles[i]['local_path'] = self.config_data.get('local_path', '')
                break

    def _on_profile_selected(self, event=None):
        """Wird aufgerufen, wenn der User ein Profil im Dropdown wählt."""
        name = self.profile_combo.get()
        if name:
            self._apply_profile(name)
            self._on_schedule_change()  # Zeitplan neu berechnen

    def _show_profile_dialog(self):
        """Dialog zum Erstellen eines neuen Profils."""
        dialog = tk.Toplevel(self.master)
        dialog.title('Neues Profil')
        dialog.geometry('500x500')
        dialog.transient(self.master)
        dialog.grab_set()
        dialog.configure(bg='#1f2937')

        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill='both', expand=True)

        row = 0
        ttk.Label(frame, text='Profil-Name:').grid(row=row, column=0, sticky='e', padx=5, pady=5)
        name_var = tk.StringVar(value='Neues Profil')
        ttk.Entry(frame, textvariable=name_var, width=40).grid(row=row, column=1, padx=5, pady=5)

        row += 1
        ttk.Label(frame, text='Backend-Typ:').grid(row=row, column=0, sticky='e', padx=5, pady=5)
        type_var = tk.StringVar(value='ssh')
        type_combo = ttk.Combobox(frame, textvariable=type_var, values=['ssh', 's3', 'local'],
                                   state='readonly', width=15)
        type_combo.grid(row=row, column=1, sticky='w', padx=5, pady=5)

        row += 1
        # Hinweis
        hint_var = tk.StringVar(value='')
        hint_label = ttk.Label(frame, textvariable=hint_var, wraplength=400)
        hint_label.grid(row=row, column=0, columnspan=2, sticky='w', padx=5, pady=5)

        def _on_type_change(*_args):
            t = type_var.get()
            hints = {
                'ssh': 'SSH: user@host:./backup (Hetzner Storage Box, beliebiger Server)',
                's3': 'S3: Bucket-Name + Access Keys + Endpoint',
                'local': 'Lokal: Pfad zu einem Verzeichnis auf der Festplatte'
            }
            hint_var.set(hints.get(t, ''))

        type_var.trace_add('write', _on_type_change)
        _on_type_change()

        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=row + 1, column=0, columnspan=2, pady=20)
        ttk.Button(btn_frame, text='Abbrechen', command=dialog.destroy).pack(side='left', padx=5)

        def _save():
            profiles = self.config_data.get('profiles', [])
            names = {p['name'] for p in profiles}
            name = name_var.get().strip()
            if not name:
                messagebox.showerror('Fehler', 'Bitte einen Namen eingeben.')
                return
            if name in names:
                messagebox.showerror('Fehler', 'Dieser Name existiert bereits.')
                return

            new_profile = {
                'name': name,
                'type': type_var.get(),
                'storage': '',
                'ssh_key': '',
                'passphrase': '',
                'compression': 'lz4',
                'include_folders': ['/'],
                'exclude_folders': [],
                'schedule_type': 'manual',
                'schedule_interval': 3,
                'schedule_time': '03:15',
                'catchup_missed': True,
                'prune_enabled': False,
                'validate_enabled': True,
                'validate_interval': 3,
                'optimize_enabled': False,
                'optimize_interval': 3,
                's3_access_key': '',
                's3_secret_key': '',
                's3_endpoint_url': '',
                's3_region': '',
                'local_path': '',
            }
            profiles.append(new_profile)
            self.config_data['profiles'] = profiles
            self._apply_profile(name)
            self._save_config()
            dialog.destroy()
            self._show_notice(f'Profil "{name}" erstellt.', level='success')

        ttk.Button(btn_frame, text='Erstellen', command=_save).pack(side='left', padx=5)
        self.master.wait_window(dialog)

    def _show_profile_dialog_edit(self):
        """Editier-Dialog für das aktive Profil."""
        name = self.config_data.get('profile_name', '')
        profiles = self.config_data.get('profiles', [])
        profile = None
        for p in profiles:
            if p['name'] == name:
                profile = p
                break
        if not profile:
            messagebox.showerror('Fehler', 'Kein Profil ausgewählt.')
            return

        dialog = tk.Toplevel(self.master)
        dialog.title(f'Profil bearbeiten: {name}')
        dialog.geometry('500x450')
        dialog.transient(self.master)
        dialog.grab_set()
        dialog.configure(bg='#1f2937')

        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill='both', expand=True)

        row = 0
        ttk.Label(frame, text='Profil-Name:').grid(row=row, column=0, sticky='e', padx=5, pady=5)
        name_var = tk.StringVar(value=profile['name'])
        ttk.Entry(frame, textvariable=name_var, width=40).grid(row=row, column=1, padx=5, pady=5)

        row += 1
        ttk.Label(frame, text='Backend-Typ:').grid(row=row, column=0, sticky='e', padx=5, pady=5)
        type_var = tk.StringVar(value=profile.get('type', 'ssh'))
        type_combo = ttk.Combobox(frame, textvariable=type_var, values=['ssh', 's3', 'local'],
                                   state='readonly', width=15)
        type_combo.grid(row=row, column=1, sticky='w', padx=5, pady=5)

        row += 1
        ttk.Label(frame, text='Repository / Pfad:').grid(row=row, column=0, sticky='e', padx=5, pady=5)
        storage_var = tk.StringVar(value=profile.get('storage', ''))
        ttk.Entry(frame, textvariable=storage_var, width=40).grid(row=row, column=1, padx=5, pady=5)

        row += 1
        ttk.Label(frame, text='SSH-Key:').grid(row=row, column=0, sticky='e', padx=5, pady=5)
        ssh_key_var = tk.StringVar(value=profile.get('ssh_key', ''))
        ttk.Entry(frame, textvariable=ssh_key_var, width=40).grid(row=row, column=1, padx=5, pady=5)

        row += 1
        ttk.Label(frame, text='Passphrase:').grid(row=row, column=0, sticky='e', padx=5, pady=5)
        passphrase_var = tk.StringVar(value=profile.get('passphrase', ''))
        ttk.Entry(frame, textvariable=passphrase_var, width=40, show='*').grid(row=row, column=1, padx=5, pady=5)

        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=row + 1, column=0, columnspan=2, pady=20)
        ttk.Button(btn_frame, text='Abbrechen', command=dialog.destroy).pack(side='left', padx=5)

        def _save():
            profile['name'] = name_var.get().strip()
            profile['type'] = type_var.get()
            profile['storage'] = storage_var.get().strip()
            profile['ssh_key'] = ssh_key_var.get().strip()
            profile['passphrase'] = passphrase_var.get()
            self._apply_profile(profile['name'])
            self._save_config()
            dialog.destroy()
            self._show_notice(f'Profil "{profile["name"]}" aktualisiert.', level='success')

        ttk.Button(btn_frame, text='Speichern', command=_save).pack(side='left', padx=5)
        self.master.wait_window(dialog)

    def _delete_current_profile(self):
        """Löscht das aktive Profil."""
        profiles = self.config_data.get('profiles', [])
        name = self.config_data.get('profile_name', '')
        if len(profiles) <= 1:
            messagebox.showwarning('Hinweis', 'Das letzte Profil kann nicht gelöscht werden.')
            return
        if not messagebox.askyesno('Löschen', f'Profil "{name}" wirklich löschen?'):
            return
        profiles = [p for p in profiles if p['name'] != name]
        self.config_data['profiles'] = profiles
        # Zum ersten Profil wechseln
        if profiles:
            self._apply_profile(profiles[0]['name'])
        self._save_config()
        self._show_notice(f'Profil "{name}" gelöscht.', level='success')


    def _update_backend_fields(self):
        """Zeigt/versteckt SSH-, S3- und Local-Felder je nach Profil-Typ."""
        profile_type = self.config_data.get('profile_type', 'ssh')
        if hasattr(self, 'ssh_fields_frame'):
            self.ssh_fields_frame.grid_remove()
        if hasattr(self, 's3_fields_frame'):
            self.s3_fields_frame.grid_remove()
        if hasattr(self, 'local_fields_frame'):
            self.local_fields_frame.grid_remove()
        if hasattr(self, 'backend_hint_var'):
            hints = {'ssh': 'SSH / Storage Box mit Port 23', 's3': 'S3 Object Storage (z.B. Hetzner)',
                     'local': 'Lokales Laufwerk / USB-Festplatte'}
            self.backend_hint_var.set(f'Aktiver Typ: {hints.get(profile_type, profile_type)}')
        if profile_type == 'ssh' and hasattr(self, 'ssh_fields_frame'):
            self.ssh_fields_frame.grid()
        elif profile_type == 's3' and hasattr(self, 's3_fields_frame'):
            self.s3_fields_frame.grid()
        elif profile_type == 'local' and hasattr(self, 'local_fields_frame'):
            self.local_fields_frame.grid()

    def _refresh_archive_profile_combo(self):
        """Aktualisiert das Profil-Dropdown im Archiv-Tab."""
        if not hasattr(self, 'archive_profile_combo'):
            return
        profiles = self.config_data.get('profiles', [])
        names = ['Alle Profile'] + [p['name'] for p in profiles]
        self.archive_profile_combo['values'] = names
        current = self.config_data.get('profile_name', '')
        # 'Alle Profile' nur setzen wenn es mehrere Profile gibt
        if current in names:
            self.archive_profile_combo.set(current)
        elif names:
            self.archive_profile_combo.set(names[0])

    def _on_archive_profile_change(self, event=None):
        """Wechselt das aktive Profil aus dem Archiv-Tab."""
        name = self.archive_profile_combo.get()
        if name == 'Alle Profile':
            self._load_all_profiles_archives()
            return
        if name:
            self.config_data['profile_name'] = name
            if name in self.archive_cache:
                self._display_archives_from_cache(name)
            else:
                self._load_archive_list()

    def _load_all_profiles_archives(self):
        """Laedt Archive von allen Profilen und zeigt sie kombiniert an."""
        profiles = self.config_data.get('profiles', [])
        if len(profiles) <= 1:
            self._load_archive_list()
            return

        self._set_archive_loading(True, 'Lade Archive aller Profile...')
        self.tree.delete(*self.tree.get_children())
        self.archive_loading_token += 1
        token = self.archive_loading_token

        def _load_sequential(profiles_list, index=0, all_archives=None):
            if all_archives is None:
                all_archives = []
            if index >= len(profiles_list) or token != self.archive_loading_token:
                # Alle geladen, anzeigen
                self._set_archive_loading(False, f'Archivliste geladen: {len(all_archives)} Archive (alle Profile)')
                return

            p = profiles_list[index]
            self._set_archive_loading(True, f'Lade {p["name"]} ({index+1}/{len(profiles_list)})...')

            from borg_backup_gui.backends import SSHBackend, S3Backend, LocalBackend
            # Wir brauchen eine Hilfsmethode, die aus einem Profil-Dict Env baut
            # Einfacher: temporaer config_data setzen
            saved_type = self.config_data.get('profile_type')
            saved_name = self.config_data.get('profile_name')
            self.config_data['profile_type'] = p.get('type', 'ssh')
            self.config_data['profile_name'] = p['name']
            self.config_data['storage'] = p.get('storage', '')
            self.config_data['local_path'] = p.get('local_path', '')
            self.config_data['s3_access_key'] = p.get('s3_access_key', '')
            self.config_data['s3_secret_key'] = p.get('s3_secret_key', '')
            self.config_data['s3_endpoint_url'] = p.get('s3_endpoint_url', '')

            repo = self._borg_repo()
            env = self._borg_env()

            import subprocess as _sp
            try:
                result = _sp.run([BORG_BIN, 'list', '--lock-wait=15', repo, '--json'],
                                 capture_output=True, text=True, env=env, timeout=60)
                if result.returncode == 0:
                    import json as _json
                    data = _json.loads(result.stdout)
                    for arch in data.get('archives', []):
                        name = arch.get('name', '?')
                        time_str = arch.get('time', '?')
                        all_archives.append((name, time_str, p['name'], p.get('type', 'ssh')))
            except Exception:
                pass

            # Naechstes Profil
            self.master.after(100, lambda: _load_sequential(profiles_list, index + 1, all_archives))

        _load_sequential(profiles)

    def _display_archives_from_cache(self, profile_name):
        """Zeigt gecachte Archivliste an (ohne erneuten Borg-Zugriff)."""
        cached = self.archive_cache.get(profile_name, [])
        self.tree.delete(*self.tree.get_children())
        for name, size, time_str, ptype in cached:
            if profile_name:
                pass  # gefiltert durch den Cache-Key
            self.tree.insert('', tk.END, text=name, values=(size, time_str, ptype))
        self._apply_archive_filter()
        self.archive_status_var.set(f'Archivliste geladen: {len(cached)} Archive (Cache)')

    def _refresh_archive_cache(self):
        """Invalidiert den Cache und laedt im Hintergrund (nach Backup)."""
        self.archive_cache = {}
        self.cache_time = None
        self.master.after(2000, self._load_archive_list)

    def _apply_archive_filter(self, event=None):
        """Filtert die Archivliste nach eingegebenem Text."""
        text = self.archive_filter_var.get().lower().strip()
        for item in self.tree.get_children():
            name = self.tree.item(item, 'text').lower()
            if text:
                self.tree.detach(item) if text not in name else None
            else:
                self.tree.reattach(item, '', 'end') if self.tree.exists(item) else None
        # Vereinfachter Filter: zeige/verstecke Items
        if text:
            for item in self.tree.get_children():
                if text not in self.tree.item(item, 'text').lower():
                    self.tree.detach(item)
        else:
            for item in self.tree.get_children():
                try:
                    self.tree.move(item, '', 'end')
                except tk.TclError:
                    pass

    def _clear_archive_filter(self):
        self.archive_filter_var.set('')
        self._apply_archive_filter()

    def _toggle_mount(self):
        """Toggelt zwischen Mount und Unmount."""
        if self.mount_active:
            self._unmount_archive()
        else:
            self._mount_archive()

    def _open_mount_in_fm(self):
        """Oeffnet den Mount-Punkt im Dateimanager."""
        if not self.mount_active:
            return
        try:
            import subprocess as _sp
            _sp.Popen(['xdg-open', self.mount_point])
        except Exception:
            messagebox.showerror('Fehler', 'Konnte Dateimanager nicht öffnen.')

    def _unmount_archive(self):
        """Unmountet das Archiv und aktualisiert Buttons."""
        import subprocess as _sp
        try:
            _sp.run(['borg', 'umount', self.mount_point], capture_output=True, timeout=10)
        except Exception:
            _sp.run(['umount', '-l', self.mount_point], capture_output=True)
        self.mount_active = False
        self.mounted_archive_name = ''
        if hasattr(self, 'mount_btn'):
            self.mount_btn.config(text='Ausgewähltes Archiv mounten')
        if hasattr(self, 'open_btn'):
            self.open_btn.config(state='disabled')


    def _create_canary_file(self):
        """Erstellt Canary-Datei mit bekanntem Inhalt vor dem Backup."""
        import hashlib
        profile_name = self.config_data.get('profile_name', 'Standard')
        timestamp = datetime.datetime.now().isoformat(timespec='seconds')
        content_lines = [
            'BORG-CANARY-CHECK',
            f'Profile: {profile_name}',
            f'Timestamp: {timestamp}',
        ]
        content_text = '\n'.join(content_lines) + '\n'
        expected_hash = hashlib.sha256(content_text.encode()).hexdigest()
        content_lines.append(f'SHA256: {expected_hash}')
        final_text = '\n'.join(content_lines) + '\n'
        with open(CANARY_FILE, "w") as cf: cf.write(final_text)
        self.canary_expected_hash = expected_hash
        self.canary_profile_name = profile_name
        self._append_backup_log(f'\n[Canary] Check-Datei erstellt: {CANARY_FILE}\n')
        self._append_backup_log(f'[Canary] Erwarteter Hash: {expected_hash[:16]}...\n')
        return str(CANARY_FILE)

    def _verify_canary(self):
        """Extrahiert Canary aus dem Archiv und verifiziert den Hash."""
        import hashlib
        import subprocess as _sp
        profile_name = self.canary_profile_name if hasattr(self, 'canary_profile_name') else self.config_data.get('profile_name', 'Standard')
        expected_hash = self.canary_expected_hash if hasattr(self, 'canary_expected_hash') else ''
        canary_rel_path = 'var/tmp/borg-canary-check.txt'
        repo = self._borg_repo()
        env = self._borg_env()

        # Letztes Archiv ermitteln
        try:
            result = _sp.run([BORG_BIN, 'list', '--lock-wait=15', '--last', '1', '--short', repo],
                             capture_output=True, text=True, env=env, timeout=60)
            if result.returncode != 0 or not result.stdout.strip():
                self.config_data['canary_last_result'] = 'fail'
                self.config_data['canary_last_check'] = datetime.datetime.now().isoformat(timespec='seconds')
                self._append_backup_log('[Canary] ❌ KEIN Archiv zum Prüfen gefunden\n')
                return False
            latest_archive = result.stdout.strip().split('\n')[0].strip()
        except Exception as e:
            self.config_data['canary_last_result'] = 'fail'
            self._append_backup_log(f'[Canary] ❌ Archiv-Ermittlung fehlgeschlagen: {e}\n')
            return False

        # Canary: Prüfen ob die Datei im Archiv existiert
        try:
            check = _sp.run([BORG_BIN, 'list', '--lock-wait=15', f'{repo}::{latest_archive}', canary_rel_path],
                            capture_output=True, text=True, env=env, timeout=60)
            if check.returncode == 0 and canary_rel_path.replace('/', '') in check.stdout.replace('/', ''):
                self.config_data['canary_last_result'] = 'ok'
                self.config_data['canary_last_check'] = datetime.datetime.now().isoformat(timespec='seconds')
                self._append_backup_log(f'[Canary] ✅ Datei im Archiv gefunden: {canary_rel_path}\n')
                return True
            else:
                self.config_data['canary_last_result'] = 'fail'
                self.config_data['canary_last_check'] = datetime.datetime.now().isoformat(timespec='seconds')
                self._append_backup_log(f'[Canary] ❌ Datei NICHT im Archiv: {canary_rel_path}\n')
                return False
        except Exception as e:
            self.config_data['canary_last_result'] = 'fail'
            self._append_backup_log(f'[Canary] ❌ Verifikation fehlgeschlagen: {e}\n')
            return False
    def _build_backup_tab(self):
        tab = self.tab_backup
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=2)
        tab.rowconfigure(1, weight=3)

        top_frame = ttk.Frame(tab)
        top_frame.grid(row=0, column=0, sticky='nsew', padx=10, pady=(10, 5))
        top_frame.columnconfigure(0, weight=1)
        top_frame.columnconfigure(1, weight=1)
        top_frame.rowconfigure(1, weight=1)

        # Profil-Auswahl
        profile_frame = ttk.Frame(top_frame)
        profile_frame.grid(row=0, column=0, columnspan=2, sticky='ew', pady=(0, 10))
        profile_frame.columnconfigure(1, weight=1)
        ttk.Label(profile_frame, text='Profil:', font=('Arial', 10, 'bold')).grid(row=0, column=0, padx=(0, 5))
        self.profile_combo = ttk.Combobox(profile_frame, state='readonly', width=40)
        self.profile_combo.grid(row=0, column=1, sticky='w')
        self.profile_combo.bind('<<ComboboxSelected>>', self._on_profile_selected)
        ttk.Button(profile_frame, text='Neu', command=self._show_profile_dialog).grid(row=0, column=2, padx=(5, 2))
        ttk.Button(profile_frame, text='Bearbeiten', command=self._show_profile_dialog_edit).grid(row=0, column=3, padx=2)
        ttk.Button(profile_frame, text='Löschen', command=self._delete_current_profile).grid(row=0, column=4, padx=(2, 5))

        left = ttk.Frame(top_frame)
        left.grid(row=1, column=0, sticky='nsew', padx=(0, 10))
        left.columnconfigure(0, weight=1)

        server_frame = ttk.LabelFrame(left, text='Backend-Konfiguration')
        server_frame.grid(row=0, column=0, sticky='ew', pady=5)
        server_frame.columnconfigure(1, weight=1)

        # === SSH-Felder (sichtbar bei Typ ssh) ===
        self.ssh_fields_frame = ttk.Frame(server_frame)
        self.ssh_fields_frame.grid(row=0, column=0, columnspan=3, sticky='ew')
        self.ssh_fields_frame.columnconfigure(1, weight=1)

        ttk.Label(self.ssh_fields_frame, text='Borg Repository URL:').grid(row=0, column=0, sticky='e', padx=5, pady=2)
        self.storage_var = tk.StringVar(value=self.config_data.get('storage', DEFAULT_STORAGE))
        self.storage_entry = ttk.Entry(self.ssh_fields_frame, textvariable=self.storage_var, width=35)
        self.storage_entry.grid(row=0, column=1, sticky='ew', padx=5, pady=2)

        ttk.Label(self.ssh_fields_frame, text='SSH Key (Pfad, optional):').grid(row=1, column=0, sticky='e', padx=5, pady=2)
        default_ssh_keys = []
        for key in ['id_ed25519', 'id_rsa', 'id_ecdsa']:
            key_path = os.path.expanduser(f'~/.ssh/{key}')
            if os.path.exists(key_path):
                default_ssh_keys.append(key_path)
        self.ssh_key_var = tk.StringVar(value=self.config_data.get('ssh_key', ''))
        if not self.ssh_key_var.get() and default_ssh_keys:
            self.ssh_key_var.set(default_ssh_keys[0])
        ssh_entry = ttk.Combobox(self.ssh_fields_frame, textvariable=self.ssh_key_var, values=default_ssh_keys, width=32)
        ssh_entry.grid(row=1, column=1, sticky='ew', padx=5, pady=2)
        ttk.Button(self.ssh_fields_frame, text='Auswählen', command=self._select_ssh_key).grid(row=1, column=2, padx=5, pady=2)

        # === S3-Felder (sichtbar bei Typ s3) ===
        self.s3_fields_frame = ttk.Frame(server_frame)
        self.s3_fields_frame.grid(row=1, column=0, columnspan=3, sticky='ew')
        self.s3_fields_frame.columnconfigure(1, weight=1)
        ttk.Label(self.s3_fields_frame, text='Bucket Name:').grid(row=0, column=0, sticky='e', padx=5, pady=2)
        self.s3_storage_var = tk.StringVar(value=self.config_data.get('storage', ''))
        ttk.Entry(self.s3_fields_frame, textvariable=self.s3_storage_var, width=35).grid(row=0, column=1, sticky='ew', padx=5, pady=2)
        ttk.Label(self.s3_fields_frame, text='S3 Access Key:').grid(row=1, column=0, sticky='e', padx=5, pady=2)
        self.s3_access_var = tk.StringVar(value=self.config_data.get('s3_access_key', ''))
        ttk.Entry(self.s3_fields_frame, textvariable=self.s3_access_var, width=35).grid(row=1, column=1, sticky='ew', padx=5, pady=2)
        ttk.Label(self.s3_fields_frame, text='S3 Secret Key:').grid(row=2, column=0, sticky='e', padx=5, pady=2)
        self.s3_secret_var = tk.StringVar(value=self.config_data.get('s3_secret_key', ''))
        ttk.Entry(self.s3_fields_frame, textvariable=self.s3_secret_var, width=35, show='*').grid(row=2, column=1, sticky='ew', padx=5, pady=2)
        ttk.Label(self.s3_fields_frame, text='Endpoint URL:').grid(row=3, column=0, sticky='e', padx=5, pady=2)
        self.s3_endpoint_var = tk.StringVar(value=self.config_data.get('s3_endpoint_url', 'https://fsn1.your-objectstorage.com'))
        ttk.Entry(self.s3_fields_frame, textvariable=self.s3_endpoint_var, width=35).grid(row=3, column=1, sticky='ew', padx=5, pady=2)

        # === Lokale Felder (sichtbar bei Typ local) ===
        self.local_fields_frame = ttk.Frame(server_frame)
        self.local_fields_frame.grid(row=2, column=0, columnspan=3, sticky='ew')
        self.local_fields_frame.columnconfigure(1, weight=1)
        ttk.Label(self.local_fields_frame, text='Ordner:').grid(row=0, column=0, sticky='e', padx=5, pady=2)
        self.local_path_var = tk.StringVar(value=self.config_data.get('local_path', ''))
        ttk.Entry(self.local_fields_frame, textvariable=self.local_path_var, width=35).grid(row=0, column=1, sticky='ew', padx=5, pady=2)
        ttk.Button(self.local_fields_frame, text='Wählen', command=self._select_local_path).grid(row=0, column=2, padx=5, pady=2)

        # === Gemeinsame Felder ===
        common_frame = ttk.Frame(server_frame)
        common_frame.grid(row=3, column=0, columnspan=3, sticky='ew')
        common_frame.columnconfigure(1, weight=1)

        ttk.Label(common_frame, text='Passphrase:').grid(row=0, column=0, sticky='e', padx=5, pady=2)
        self.passphrase_var = tk.StringVar(value=self.config_data.get('borg_passphrase', ''))
        ttk.Entry(common_frame, textvariable=self.passphrase_var, width=35, show='*').grid(row=0, column=1, sticky='ew', padx=5, pady=2)
        ttk.Label(common_frame, text='(leer = interaktiv abfragen)', font=('Arial', 8)).grid(row=1, column=1, sticky='w', padx=5)

        ttk.Label(common_frame, text='Kompression:').grid(row=2, column=0, sticky='e', padx=5, pady=2)
        self.compression_var = tk.StringVar(value=self.config_data.get('compression', 'lz4'))
        compression_combo = ttk.Combobox(common_frame, textvariable=self.compression_var,
                                          values=['none', 'lz4', 'zstd,3', 'zstd,6', 'zstd,9'],
                                          width=15, state='readonly')
        compression_combo.grid(row=2, column=1, sticky='w', padx=5, pady=2)

        # Backend-Typ-Hinweis
        self.backend_hint_var = tk.StringVar(value='')
        ttk.Label(common_frame, textvariable=self.backend_hint_var, font=('Arial', 8),
                  foreground='#0f766e').grid(row=3, column=1, sticky='w', padx=5)

        # Backup-Buttons (links unten unter Config)
        btn_frame = ttk.LabelFrame(left, text='Backup-Aktion')
        btn_frame.grid(row=1, column=0, sticky='ew', pady=(5, 0))
        self.backup_btn = ttk.Button(btn_frame, text='Backup jetzt ausführen', command=self._start_backup)
        self.backup_btn.pack(side='left', padx=5, pady=5)
        self.stop_btn = ttk.Button(btn_frame, text='Abbrechen', command=self._stop_backup)
        self.stop_btn.pack(side='left', padx=5, pady=5)
        self.stop_btn.config(state='disabled')

        right = ttk.Frame(top_frame)
        right.grid(row=1, column=1, sticky='nsew', padx=(10, 0))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        right.rowconfigure(3, weight=1)

        inc_frame = ttk.LabelFrame(right, text='Include (zu sichernde Ordner)')
        inc_frame.grid(row=0, column=0, sticky='nsew', pady=(0, 5))
        inc_frame.columnconfigure(0, weight=1)
        self.include_text = tk.Text(inc_frame, height=3, bg='#4a4a4a', fg='#ffffff', insertbackground='white')
        self.include_text.grid(row=0, column=0, sticky='nsew', padx=5, pady=5)
        inc_content = '\n'.join(self.config_data.get('include_folders', ['/']))
        self.include_text.insert(tk.END, inc_content)

        exc_frame = ttk.LabelFrame(right, text='Exclude (ausgeschlossene Ordner)')
        exc_frame.grid(row=1, column=0, sticky='nsew', pady=5)
        exc_frame.columnconfigure(0, weight=1)
        exc_frame.rowconfigure(0, weight=1)
        self.exclude_text = tk.Text(exc_frame, height=8, bg='#4a4a4a', fg='#ffffff', insertbackground='white')
        self.exclude_text.grid(row=0, column=0, sticky='nsew', padx=5, pady=5)
        exc_content = '\n'.join(self.config_data.get('exclude_folders', []))
        self.exclude_text.insert(tk.END, exc_content)

        log_frame = ttk.LabelFrame(tab, text='Live-Log')
        log_frame.grid(row=1, column=0, sticky='nsew', padx=10, pady=(0, 10))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_widget = tk.Text(
            log_frame,
            state='disabled',
            bg='#1e1e1e',
            fg='#00ff00',
            font=('Courier', 9),
            wrap=tk.WORD
        )
        self.log_widget.grid(row=0, column=0, sticky='nsew', padx=2, pady=2)
        log_scroll = ttk.Scrollbar(log_frame, command=self.log_widget.yview)
        log_scroll.grid(row=0, column=1, sticky='ns', padx=2, pady=2)
        self.log_widget.configure(yscrollcommand=log_scroll.set)

    def _build_archive_tab(self):
        tab = self.tab_archive
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)

        top = ttk.Frame(tab)
        top.grid(row=0, column=0, sticky='ew', padx=10, pady=(10, 5))
        ttk.Label(top, text='Profil:').pack(side='left', padx=(0, 5))
        self.archive_profile_combo = ttk.Combobox(top, state='readonly', width=35)
        self.archive_profile_combo.pack(side='left', padx=(0, 10))
        self.archive_profile_combo.bind('<<ComboboxSelected>>', self._on_archive_profile_change)

        self.load_btn = ttk.Button(top, text='Archivliste laden', command=self._load_archive_list)
        self.load_btn.pack(side='left', padx=2)
        self.archive_status_var = tk.StringVar(value='')
        ttk.Label(top, textvariable=self.archive_status_var).pack(side='left', padx=8)
        self.archive_progress = ttk.Progressbar(top, mode='indeterminate', length=140)
        self.archive_progress.pack(side='left', padx=5)
        self.archive_progress.pack_forget()

        # Filter
        filter_frame = ttk.Frame(tab)
        filter_frame.grid(row=1, column=0, sticky='ew', padx=10, pady=(0, 5))
        ttk.Label(filter_frame, text='Filter:').pack(side='left', padx=(0, 5))
        self.archive_filter_var = tk.StringVar(value='')
        self.archive_filter_entry = ttk.Entry(filter_frame, textvariable=self.archive_filter_var, width=30)
        self.archive_filter_entry.pack(side='left', padx=(0, 5))
        self.archive_filter_entry.bind('<KeyRelease>', self._apply_archive_filter)
        ttk.Button(filter_frame, text='✕', command=self._clear_archive_filter, width=3).pack(side='left')

        self.tree = ttk.Treeview(tab, columns=('size', 'time', 'type'), show='tree headings', selectmode='browse')
        self.tree.heading('#0', text='Archivname')
        self.tree.heading('size', text='Größe')
        self.tree.heading('time', text='Datum')
        self.tree.heading('type', text='Profil-Typ')
        self.tree.column('#0', width=280)
        self.tree.column('size', width=120)
        self.tree.column('time', width=160)
        self.tree.column('type', width=80)
        self.tree.grid(row=2, column=0, sticky='nsew', padx=10, pady=5)

        scrollbar = ttk.Scrollbar(tab, orient=tk.VERTICAL, command=self.tree.yview)
        scrollbar.grid(row=2, column=1, sticky='ns')
        self.tree.configure(yscrollcommand=scrollbar.set)

        btm = ttk.Frame(tab)
        btm.grid(row=3, column=0, sticky='ew', padx=10, pady=5)
        self.mount_btn = ttk.Button(btm, text='Ausgewähltes Archiv mounten', command=self._toggle_mount)
        self.mount_btn.pack(side='left', padx=2)
        self.open_btn = ttk.Button(btm, text='In Dateien öffnen', command=self._open_mount_in_fm, state='disabled')
        self.open_btn.pack(side='left', padx=2)
        ttk.Button(btm, text='Ausgewähltes Archiv löschen', command=self._delete_archive).pack(side='left', padx=2)

        # Cache & Mount-State
        self.archive_cache = {}        # profile_name -> list of tuples
        self.cache_time = None
        self.mount_active = False
        self.mount_point = '/tmp/borg-mount'
        self.mounted_archive_name = ''

    def _build_restore_tab(self):
        tab = self.tab_restore
        ttk.Label(tab, text='Wiederherstellung (Restore)', font=('Arial', 14, 'bold')).pack(pady=10)

        frame = ttk.Frame(tab)
        frame.pack(pady=10)

        ttk.Label(frame, text='Archivname:').grid(row=0, column=0, sticky='e', padx=5, pady=5)
        self.restore_archive_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.restore_archive_var, width=40).grid(row=0, column=1, padx=5)

        ttk.Label(frame, text='Zielverzeichnis:').grid(row=1, column=0, sticky='e', padx=5, pady=5)
        self.restore_path_var = tk.StringVar(value='/mnt/restore')
        ttk.Entry(frame, textvariable=self.restore_path_var, width=40).grid(row=1, column=1, padx=5)
        ttk.Button(frame, text='Ziel wählen', command=self._select_restore_target).grid(row=1, column=2, padx=5)

        self.restore_log = tk.Text(tab, state='disabled', bg='#1e1e1e', fg='#00ff00', height=8)
        self.restore_log.pack(fill='both', expand=True, padx=10, pady=10)

        ttk.Button(tab, text='Restore starten', command=self._start_restore).pack(pady=5)

    def _append_backup_log(self, text):
        self.log_queue.put(('backup', text))

    def _append_restore_log(self, text):
        self.log_queue.put(('restore', text))

    def _process_log_queue(self):
        while True:
            try:
                target, text = self.log_queue.get_nowait()
            except queue.Empty:
                break

            if target == 'backup' and self.log_widget:
                self._update_backup_progress(text)
                self.log_widget.configure(state='normal')
                self.log_widget.insert(tk.END, text)
                self.log_widget.see(tk.END)
                self.log_widget.configure(state='disabled')
            elif target == 'restore' and self.restore_log:
                self.restore_log.configure(state='normal')
                self.restore_log.insert(tk.END, text)
                self.restore_log.see(tk.END)
                self.restore_log.configure(state='disabled')

        # Fallback-Takt: hält die Tray-Livewerte im Sekundentakt frisch,
        # auch wenn ein einzelner Timer-Callback verspätet kommt.
        if self.backup_running and self.tray_runtime_available and self.config_data.get('tray_enabled', True):
            now = datetime.datetime.now()
            if self.tray_menu_last_refresh_at is None or (now - self.tray_menu_last_refresh_at).total_seconds() >= 1.0:
                self.tray_menu_last_refresh_at = now
                self._update_tray_menu_labels()
        else:
            self.tray_menu_last_refresh_at = None

        self.master.after(100, self._process_log_queue)

    def _clear_notice(self):
        self.notice_after_id = None
        if hasattr(self, 'notice_var'):
            self.notice_var.set('')
        if hasattr(self, 'notice_label'):
            self.notice_label.configure(bg='#111827', fg='#cbd5e1')

    def _show_notice(self, text, level='info', timeout_ms=7000):
        palette = {
            'info': ('#0f172a', '#e2e8f0'),
            'success': ('#14532d', '#dcfce7'),
            'warning': ('#78350f', '#fef3c7'),
            'error': ('#7f1d1d', '#fee2e2')
        }
        bg, fg = palette.get(level, palette['info'])
        self.notice_var.set(text)
        self.notice_label.configure(bg=bg, fg=fg)
        if self.notice_after_id:
            self.master.after_cancel(self.notice_after_id)
            self.notice_after_id = None
        if timeout_ms:
            self.notice_after_id = self.master.after(timeout_ms, self._clear_notice)

    def _reset_backup_progress(self):
        self.backup_files_seen = 0
        self.backup_last_item = ''
        if hasattr(self, 'backup_files_var'):
            self.backup_files_var.set('Gefundene/verarbeitete Dateien: 0')
        if hasattr(self, 'backup_source_size_var'):
            self.backup_source_size_var.set('Quellgröße: noch keine Daten')
        if hasattr(self, 'backup_dedupe_size_var'):
            self.backup_dedupe_size_var.set('Dedupliziert: noch keine Daten')
        if hasattr(self, 'backup_last_item_var'):
            self.backup_last_item_var.set('Zuletzt: noch keine Daten')
        self.status_data['last_files_count'] = 0
        self.status_data['last_source_size'] = ''
        self.status_data['last_dedupe_size'] = ''
        self.status_data['last_item'] = ''

    def _set_live_stats(self, files_count=None, source_size=None, dedupe_size=None, last_item=None):
        changed = False

        if files_count is not None:
            self.status_data['last_files_count'] = int(files_count)
            changed = True
        if source_size:
            self.status_data['last_source_size'] = source_size
            changed = True
        if dedupe_size:
            self.status_data['last_dedupe_size'] = dedupe_size
            changed = True
        if last_item:
            self.status_data['last_item'] = last_item
            changed = True

        if changed and not self.backup_running:
            self._save_status()

    def _restore_stats_from_status(self):
        files_count = self.status_data.get('last_files_count', 0)
        stats_missing = self._stats_missing_from_status()
        if not files_count and self.status_data.get('last_success_at'):
            files_label = 'wird geladen...' if stats_missing else 'nicht verfügbar'
        else:
            files_label = str(files_count)

        source_size = self.status_data.get('last_source_size', '')
        dedupe_size = self.status_data.get('last_dedupe_size', '')
        last_item = self.status_data.get('last_item', '')

        if not source_size:
            if stats_missing:
                source_size = 'wird geladen...'
            else:
                source_size = 'nicht verfügbar' if self.status_data.get('last_success_at') else 'noch keine Daten'
        if not dedupe_size:
            if stats_missing:
                dedupe_size = 'wird geladen...'
            else:
                dedupe_size = 'nicht verfügbar' if self.status_data.get('last_success_at') else 'noch keine Daten'
        if not last_item:
            if stats_missing:
                last_item = 'wird geladen...'
            else:
                last_item = 'nicht verfügbar' if self.status_data.get('last_success_at') else 'noch keine Daten'

        if hasattr(self, 'backup_files_var'):
            self.backup_files_var.set(f'Gefundene/verarbeitete Dateien: {files_label}')
        if hasattr(self, 'backup_source_size_var'):
            self.backup_source_size_var.set(f'Quellgröße: {source_size}')
        if hasattr(self, 'backup_dedupe_size_var'):
            self.backup_dedupe_size_var.set(f'Dedupliziert: {dedupe_size}')
        if hasattr(self, 'backup_last_item_var'):
            self.backup_last_item_var.set(f'Zuletzt: {last_item}')

    def _stats_missing_from_status(self):
        if not self.status_data.get('last_success_at'):
            return False

        return any([
            not self.status_data.get('last_files_count'),
            not self.status_data.get('last_source_size'),
            not self.status_data.get('last_dedupe_size'),
            not self.status_data.get('last_item')
        ])

    def _refresh_dashboard_status(self):
        self._update_dashboard_status()
        if self.backup_running or not self._stats_missing_from_status():
            return

        self._show_notice('Statistik des letzten Backups wird geladen...', level='info', timeout_ms=4000)
        threading.Thread(target=self._refresh_stats_from_latest_archive_worker, daemon=True).start()

    def _refresh_stats_from_latest_archive_worker(self):
        repo = self._borg_repo()
        env = self._borg_env()
        cmd = [BORG_BIN, 'info', '--lock-wait=30', '--last', '1', repo]
        cmd, env = self._command_with_privilege(cmd, env, needs_root=False)

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=90)
        except Exception:
            return

        if result.returncode != 0:
            return

        files_match = re.search(r'Number of files:\s*(\d+)', result.stdout)
        archive_name_match = re.search(r'Archive name:\s*(.+)', result.stdout)
        totals_match = re.search(
            r'This archive:\s+([0-9.,]+\s*[KMGTPE]?i?B)\s+([0-9.,]+\s*[KMGTPE]?i?B)\s+([0-9.,]+\s*[KMGTPE]?i?B)',
            result.stdout
        )

        updates = {}
        if files_match:
            updates['last_files_count'] = int(files_match.group(1))
        if archive_name_match:
            updates['last_item'] = f"Archiv {archive_name_match.group(1).strip()}"
        if totals_match:
            updates['last_source_size'] = totals_match.group(1)
            updates['last_dedupe_size'] = totals_match.group(3)

        if not updates:
            return

        self.master.after(0, lambda: self._apply_recovered_dashboard_stats(updates))

    def _apply_recovered_dashboard_stats(self, updates):
        self.status_data.update(updates)
        self._save_status()
        self._update_dashboard_status()

    def _update_backup_progress(self, text):
        line = text.strip()
        if not line:
            return

        progress_match = re.match(
            r'^([0-9.,]+\s*[KMGTPE]?i?B)\s+O\s+([0-9.,]+\s*[KMGTPE]?i?B)\s+C\s+([0-9.,]+\s*[KMGTPE]?i?B)\s+D\s+(\d+)\s+N\s+(.+)$',
            line
        )
        if progress_match:
            if hasattr(self, 'backup_source_size_var'):
                self.backup_source_size_var.set(f'Quellgröße: {progress_match.group(1)}')
            if hasattr(self, 'backup_dedupe_size_var'):
                self.backup_dedupe_size_var.set(f'Dedupliziert: {progress_match.group(3)}')
            if hasattr(self, 'backup_files_var'):
                self.backup_files_var.set(f'Gefundene/verarbeitete Dateien: {progress_match.group(4)}')
            if hasattr(self, 'backup_last_item_var'):
                self.backup_last_item_var.set(f'Zuletzt: {progress_match.group(5)[:110]}')
            self._set_live_stats(
                files_count=progress_match.group(4),
                source_size=progress_match.group(1),
                dedupe_size=progress_match.group(3),
                last_item=progress_match.group(5)[:110]
            )
            self._update_dashboard_status()
            return

        if line.startswith('/') or line.startswith('./') or line.startswith('../'):
            self.backup_files_seen += 1
            if hasattr(self, 'backup_files_var'):
                self.backup_files_var.set(f'Gefundene/verarbeitete Dateien: {self.backup_files_seen}')
            self.backup_last_item = line
            if hasattr(self, 'backup_last_item_var'):
                self.backup_last_item_var.set(f'Zuletzt: {line[:110]}')
            self._set_live_stats(files_count=self.backup_files_seen, last_item=line[:110])

        match_files = re.search(r'(?:Number of files|Files)\s*:\s*(\d+)', line)
        if match_files and hasattr(self, 'backup_files_var'):
            self.backup_files_var.set(f'Gefundene/verarbeitete Dateien: {match_files.group(1)}')
            self._set_live_stats(files_count=match_files.group(1))

        match_source = re.search(r'(?:Original size|This archive)\s*:\s*([0-9.,]+\s*[KMGTPE]?i?B)', line)
        if match_source and hasattr(self, 'backup_source_size_var'):
            self.backup_source_size_var.set(f'Quellgröße: {match_source.group(1)}')
            self._set_live_stats(source_size=match_source.group(1))

        match_dedupe = re.search(r'Deduplicated size\s*:\s*([0-9.,]+\s*[KMGTPE]?i?B)', line)
        if match_dedupe and hasattr(self, 'backup_dedupe_size_var'):
            self.backup_dedupe_size_var.set(f'Dedupliziert: {match_dedupe.group(1)}')
            self._set_live_stats(dedupe_size=match_dedupe.group(1))

        match_archive_totals = re.search(
            r'This archive:\s+([0-9.,]+\s*[KMGTPE]?i?B)\s+([0-9.,]+\s*[KMGTPE]?i?B)\s+([0-9.,]+\s*[KMGTPE]?i?B)',
            line
        )
        if match_archive_totals:
            if hasattr(self, 'backup_source_size_var'):
                self.backup_source_size_var.set(f'Quellgröße: {match_archive_totals.group(1)}')
            if hasattr(self, 'backup_dedupe_size_var'):
                self.backup_dedupe_size_var.set(f'Dedupliziert: {match_archive_totals.group(3)}')
            self._set_live_stats(source_size=match_archive_totals.group(1), dedupe_size=match_archive_totals.group(3))

        self._update_dashboard_status()

    def _select_ssh_key(self):
        filename = filedialog.askopenfilename(
            title='SSH Key auswählen',
            initialdir=os.path.expanduser('~/.ssh')
        )
        if filename:
            self.ssh_key_var.set(filename)

    def _select_local_path(self):
        path = filedialog.askdirectory(title='Repository-Pfad wählen')
        if path:
            self.local_path_var.set(path)
            self.config_data['local_path'] = path

    def _select_restore_target(self):
        path = filedialog.askdirectory(title='Zielverzeichnis fuer Restore')
        if path:
            self.restore_path_var.set(path)

    def _borg_env(self):
        """Liefert Umgebungsvariablen für Borg – je nach Profil-Typ (ssh/s3/local)."""
        env = os.environ.copy()
        profile_type = self.config_data.get('profile_type', 'ssh')

        # Cache immer setzen
        env['BORG_CACHE_DIR'] = os.path.expanduser('~/.cache/borg')

        if profile_type == 's3':
            # S3 Object Storage – KEIN BORG_RSH!
            env['AWS_ACCESS_KEY_ID'] = self.config_data.get('s3_access_key', '')
            env['AWS_SECRET_ACCESS_KEY'] = self.config_data.get('s3_secret_key', '')
            if self.config_data.get('s3_endpoint_url'):
                env['AWS_ENDPOINT_URL'] = self.config_data.get('s3_endpoint_url')
            if self.config_data.get('s3_region'):
                env['AWS_DEFAULT_REGION'] = self.config_data.get('s3_region')
        elif profile_type == 'local':
            # Lokales Laufwerk – kein BORG_RSH, kein AWS
            pass
        else:
            # SSH / Storage Box (Standard)
            ssh_cmd = ("ssh -p 23"
                       " -o StrictHostKeyChecking=accept-new"
                       " -o BatchMode=yes"
                       " -o ConnectTimeout=30"
                       " -o ServerAliveInterval=15"
                       " -o ServerAliveCountMax=3")
            if self.ssh_key_var.get():
                ssh_cmd += f" -i {self.ssh_key_var.get()}"
            env['BORG_RSH'] = ssh_cmd
            env['BORG_RELOCATED_REPO_ACCESS_IS_OK'] = 'yes'

        # Passphrase für alle Typen
        if self.passphrase_var.get():
            env['BORG_PASSPHRASE'] = self.passphrase_var.get()
        return env

    def _borg_repo(self):
        """Liefert die Borg-Repository-URL je nach Profil-Typ."""
        profile_type = self.config_data.get('profile_type', 'ssh')

        if profile_type == 'local':
            return self.config_data.get('local_path', '') or '/tmp/borg-repo'

        # Speicher-URL aus Config (wird von _collect_config_from_ui aktuell gehalten)
        repo = self.config_data.get('storage', '').strip()

        if profile_type == 's3':
            if not repo.startswith('s3://'):
                repo = f's3://{repo}'
            return repo

        # SSH – Korrigiert versehentliches Scp-Format mit Port im Pfad
        if '://' not in repo and ':' in repo:
            host_part, path_part = repo.split(':', 1)
            if path_part.startswith('23/'):
                repo = f'{host_part}:{path_part[3:]}'
                self.config_data['storage'] = repo
                if hasattr(self, 'storage_var'):
                    self.storage_var.set(repo)

        return repo

    def _command_with_privilege(self, cmd, env, needs_root=False):
        """Wrap command with non-interactive sudo when privileged access is required."""
        if not needs_root:
            return cmd, env

        if self.is_root:
            return cmd, env

        # Keine interaktiven Passworteingaben mehr: nur non-interactive sudo.
        # Wenn das System nicht entsprechend vorbereitet ist, scheitert der Befehl sauber ohne Nachfrage.
        if shutil.which('sudo'):
            check = subprocess.run(
                ['sudo', '-n', '-E', BORG_BIN, '--version'],
                capture_output=True, text=True,
                env=env  # Env mit BORG_RSH/AWS_* übergeben
            )
            if check.returncode != 0:
                raise PermissionError(
                    'Passwortfreies sudo ist nicht eingerichtet. '\
                    'Für automatische Backups muss eine einmalige NOPASSWD-Regel '\
                    'für dieses Programm eingerichtet werden.'
                )

            # Root-Läufe bekommen ihren eigenen Cache, damit keine root-owned
            # Cache-Dateien den User-Zugriff auf borg list/info blockieren.
            env['HOME'] = '/root'
            env['XDG_CONFIG_HOME'] = '/root/.config'
            env['XDG_CACHE_HOME'] = '/root/.cache'
            env['BORG_CACHE_DIR'] = '/root/.cache/borg'

            wrapped = ['sudo', '-n', '-E']
            wrapped.extend(cmd)
            return wrapped, env

        return cmd, env

    def _run_borg_sync(self, cmd, timeout=None, needs_root=False):
        env = self._borg_env()
        final_cmd, final_env = self._command_with_privilege(cmd, env, needs_root=needs_root)
        return subprocess.run(final_cmd, capture_output=True, text=True, timeout=timeout, env=final_env)

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
            return '-'
        return dt.strftime('%d.%m.%Y %H:%M:%S')

    def _on_tray_settings_change(self):
        if not self.tray_supported:
            self._show_notice('Tray nicht verfügbar. Installiere bitte pystray und pillow.', level='warning')
            return
        self._save_config()
        self._setup_tray_icon(force_restart=True)
        self._update_dashboard_status()
        self._show_notice('Tray-Einstellungen übernommen.', level='success')

    def _setup_tray_icon(self, force_restart=False):
        if not self.tray_runtime_available:
            return

        tray_enabled = bool(self.config_data.get('tray_enabled', True))
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

        if not (pystray and Image and ImageDraw):
            self.tray_ready = False
            self._show_notice('Tray-Fallback nicht verfügbar (pystray/Pillow fehlen).', level='warning', timeout_ms=10000)
            return

        self._schedule_tray_live_refresh(1000)

        initial_color, _ = self._compute_status_traffic_light()
        image = self._build_tray_icon_image(initial_color, self._tray_icon_status_kind(initial_color))

        try:
            self.tray_icon = pystray.Icon('borg_backup_gui', image, 'Borg Backup GUI', self._build_pystray_menu())
            self.tray_icon.run_detached()
            self.tray_ready = True
        except Exception as exc:
            self.tray_icon = None
            self.tray_ready = False
            self._show_notice(f'Tray konnte nicht gestartet werden: {exc}', level='warning', timeout_ms=10000)

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
        last_success = self.status_data.get('last_success_at', '')
        last_success_text = self._format_time(last_success)
        if last_success_text == '-':
            last_success_text = 'noch nie'

        if self.next_backup_dt:
            next_backup_text = self.next_backup_dt.strftime('%d.%m.%Y %H:%M')
        else:
            next_backup_text = 'deaktiviert'

        return (
            f'Zuletzt: {last_success_text}',
            f'Geplant: {next_backup_text}'
        )

    def _tray_backup_action_label(self):
        return 'Backup abbrechen' if self.backup_running else 'Backup jetzt starten'

    def _format_runtime(self, seconds):
        if seconds is None:
            return 'noch keine Daten'

        total_seconds = max(0, int(seconds))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f'{hours:02d}:{minutes:02d}:{seconds:02d}'

    def _tray_live_summary_lines(self):
        if self.backup_running:
            runtime = None
            if self.current_backup_started_at:
                runtime = (datetime.datetime.now() - self.current_backup_started_at).total_seconds()

            task_label = self.backup_current_task.get('label', '') if self.backup_current_task else ''
            files_count = self.status_data.get('last_files_count', self.backup_files_seen)
            source_size = self.status_data.get('last_source_size', '') or 'wird geladen...'
            dedupe_size = self.status_data.get('last_dedupe_size', '') or 'wird geladen...'
            last_item = self.status_data.get('last_item', self.backup_last_item) or 'wird geladen...'

            return (
                'Live: Backup läuft',
                f'Laufzeit: {self._format_runtime(runtime)}',
                f'Fortschritt: {files_count} Dateien | Quelle {source_size} | Dedupe {dedupe_size}',
                f'Aktuell: {task_label or last_item[:100] or "arbeitet..."}'
            )

        status_title = self.status_title_var.get() if hasattr(self, 'status_title_var') else 'Borg Backup GUI'
        last_success = self._format_time(self.status_data.get('last_success_at', ''))
        if last_success == '-':
            last_success = 'noch nie'

        next_backup = self.next_backup_dt.strftime('%d.%m.%Y %H:%M') if self.next_backup_dt else 'deaktiviert'
        last_error = self.status_data.get('last_error', '').strip() or 'keine Meldung'

        return (
            f'Live: {status_title}',
            f'Letzter Lauf: {last_success}',
            f'Geplant: {next_backup}',
            f'Status: {last_error[:110]}'
        )

    def _tray_info_text(self, index):
        return lambda _item: self._tray_live_summary_lines()[index]

    def _tray_backup_action_text(self, _item=None):
        return self._tray_backup_action_label()

    def _build_pystray_menu(self):
        return pystray.Menu(
            pystray.MenuItem(self._tray_info_text(0), lambda *_: None, enabled=False),
            pystray.MenuItem(self._tray_info_text(1), lambda *_: None, enabled=False),
            pystray.MenuItem(self._tray_info_text(2), lambda *_: None, enabled=False),
            pystray.MenuItem(self._tray_info_text(3), lambda *_: None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Fenster anzeigen', self._tray_menu_show_window),
            pystray.MenuItem(self._tray_backup_action_text, self._tray_menu_backup_toggle),
            pystray.MenuItem('Beenden', self._tray_menu_quit)
        )

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

        show_item = Gtk.MenuItem(label='Fenster anzeigen')
        show_item.connect('activate', lambda *_: self.master.after(0, self._show_window))
        menu.append(show_item)

        backup_item = Gtk.MenuItem(label=self._tray_backup_action_label())
        backup_item.connect('activate', lambda *_: self.master.after(0, self._tray_backup_toggle))
        menu.append(backup_item)

        menu.append(Gtk.SeparatorMenuItem())

        quit_item = Gtk.MenuItem(label='Beenden')
        quit_item.connect('activate', lambda *_: self.master.after(0, self._quit_app))
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

        if not self.tray_runtime_available or not self.config_data.get('tray_enabled', True):
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
                'borg_backup_gui',
                '',
                AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS
            )
            self.native_indicator = indicator

            if Gtk.main_level() == 0:
                self.native_indicator_thread = threading.Thread(target=Gtk.main, daemon=True)
                self.native_indicator_thread.start()

            initial_color, _ = self._compute_status_traffic_light()
            if GLib is not None:
                menu = self._build_native_indicator_menu()
                title = self.status_title_var.get() if hasattr(self, 'status_title_var') else 'Borg Backup GUI'
                status_kind = self._tray_icon_status_kind(initial_color)
                rgb = tuple(int(initial_color[i:i + 2], 16) for i in (1, 3, 5))
                startup_image = self._build_tray_icon_image((*rgb, 255), status_kind, self.tray_running_animation_phase)

                def _activate_indicator():
                    try:
                        icon_path = self._tray_icon_file_path()
                        startup_image.save(icon_path, 'PNG')
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
            self._show_notice(f'Native AppIndicator konnte nicht gestartet werden: {exc}', level='warning', timeout_ms=10000)
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
            self._show_notice('Backup wird abgebrochen...', level='warning')
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
            if str(self.master.state()) == 'withdrawn':
                self.master.deiconify()
            self.master.lift()
        except Exception:
            pass

    def _hide_window_to_tray(self):
        self.master.withdraw()
        if not self.tray_hint_shown:
            self.tray_hint_shown = True
            self._show_notice('Die App läuft jetzt im Hintergrund. Öffnen über das Tray-Symbol.', level='info')
        # Sicherstellen, dass das Fenster wieder erscheint, falls der Tray stirbt
        self.master.after(10000, self._ensure_window_visible_if_tray_dead)

    def _ensure_window_visible_if_tray_dead(self):
        if self.master.state() == 'withdrawn':
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

    def _setup_instance_server(self):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            if INSTANCE_SOCKET_FILE.exists():
                try:
                    if _activate_existing_instance():
                        raise SystemExit('Bestehende Instanz aktiviert')
                except SystemExit:
                    raise
                except Exception:
                    pass
                try:
                    INSTANCE_SOCKET_FILE.unlink()
                except OSError:
                    return

            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(INSTANCE_SOCKET_FILE))
            server.listen(1)
            self.instance_server = server

            def serve():
                while self.instance_server is server:
                    try:
                        conn, _ = server.accept()
                    except OSError:
                        break
                    try:
                        message = conn.recv(64).decode('utf-8', errors='ignore').strip()
                    except OSError:
                        message = ''
                    finally:
                        try:
                            conn.close()
                        except OSError:
                            pass

                    if message == 'SHOW':
                        self.master.after(0, self._show_window)

            self.instance_server_thread = threading.Thread(target=serve, daemon=True)
            self.instance_server_thread.start()
        except OSError:
            self.instance_server = None

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
            return 'running'
        if color_hex == '#28a745':
            return 'ok'
        if color_hex == '#dc3545':
            return 'error'
        if color_hex == '#e0a800':
            return 'warning'
        return 'idle'

    def _build_tray_icon_image(self, status_color, status_kind='idle', animation_phase=0):
        size = 64
        image = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)

        bg = (35, 35, 35, 245)
        draw.rounded_rectangle((4, 4, 60, 60), radius=14, fill=bg)

        if status_kind == 'running':
            draw.rounded_rectangle((6, 6, 58, 58), radius=13, outline=status_color, width=4)

        style = self.config_data.get('tray_icon_style', 'disk')
        fg = (245, 245, 245, 255)
        muted = (180, 180, 180, 255)

        if style == 'shield':
            draw.polygon([(32, 12), (48, 18), (45, 38), (32, 51), (19, 38), (16, 18)], fill=fg)
            draw.polygon([(32, 18), (42, 22), (40, 35), (32, 43), (24, 35), (22, 22)], fill=bg)
        elif style == 'palm':
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
        if status_kind == 'running':
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
            icon_image = self._build_tray_icon_image((34, 197, 94, 255), 'ok')
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
            img.save(icon_path, 'PNG')
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
        return CONFIG_DIR / 'tray-icon.png'

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

        tray_enabled = bool(self.config_data.get('tray_enabled', True))
        if not tray_enabled:
            self.tray_ready = False
            return

        # Prüfe pystray Tray
        if self.tray_icon is not None:
            try:
                if hasattr(self.tray_icon, 'update_menu'):
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
                self._show_notice('Tray-Symbol wurde neu gestartet.', level='info', timeout_ms=5000)
                self._start_tray_icon()
                self._restore_window_if_hidden()
            return

        # Prüfe nativen Indicator
        if self.native_indicator is not None:
            try:
                if Gtk is not None and Gtk.main_level() == 0:
                    raise RuntimeError('Gtk main loop not running')
            except Exception:
                self.native_indicator = None
                self.native_indicator_failed = True
                self.tray_ready = False
                self._show_notice('Tray-Symbol wurde neu gestartet.', level='info', timeout_ms=5000)
                self._start_tray_icon()
                self._restore_window_if_hidden()
            return

        # Kein Tray-Icon, aber Tray sollte aktiv sein → neu starten
        self.tray_ready = False
        self._start_tray_icon()
        self._restore_window_if_hidden()

    def _restore_window_if_hidden(self):
        try:
            if str(self.master.state()) == 'withdrawn':
                self._show_window()
        except Exception:
            pass


    def _tick_running_tray_animation(self):
        self.tray_running_animation_id = None
        self.tray_running_animation_phase = 0

    def _update_tray_status_icon(self, color_hex, animation_phase=None):
        status_kind = self._tray_icon_status_kind(color_hex)
        if animation_phase is None:
            animation_phase = self.tray_running_animation_phase if status_kind == 'running' else 0
        title = self.status_title_var.get() if hasattr(self, 'status_title_var') else 'Borg Backup GUI'
        icon_signature = (bool(self.native_indicator), color_hex, status_kind, animation_phase, self.config_data.get('tray_icon_style', 'disk'), title)

        if icon_signature == self.tray_icon_signature:
            return

        if self.native_indicator is not None:
            try:
                rgb = tuple(int(color_hex[i:i + 2], 16) for i in (1, 3, 5))
                img = self._build_tray_icon_image((*rgb, 255), status_kind, animation_phase)
                self._set_native_indicator_icon_from_image(img, title)
            except Exception:
                pass
            self.tray_icon_signature = icon_signature
            return

        if self.tray_icon is None or not self.tray_supported:
            return

        try:
            rgb = tuple(int(color_hex[i:i + 2], 16) for i in (1, 3, 5))
            self.tray_icon.icon = self._build_tray_icon_image((*rgb, 255), status_kind, animation_phase)
            self.tray_icon.title = title
        except Exception:
            pass
        self.tray_icon_signature = icon_signature

    def _compute_status_traffic_light(self):
        now = datetime.datetime.now()
        last_success = self._parse_iso_time(self.status_data.get('last_success_at', ''))
        exit_code = self.status_data.get('last_exit_code', None)

        color = '#6e6e6e'
        title = 'Noch kein Backup ausgeführt'

        if self.backup_running:
            color = '#2d7ff9'
            title = 'Backup läuft gerade'
        elif exit_code == 0 and last_success:
            age_hours = (now - last_success).total_seconds() / 3600.0
            if age_hours <= 24:
                color = '#28a745'
                title = 'Backup aktuell und erfolgreich'
            elif age_hours <= 48:
                color = '#e0a800'
                title = 'Backup erfolgreich, aber nicht mehr tagesaktuell'
            else:
                color = '#dc3545'
                title = 'Letztes erfolgreiches Backup ist zu alt'
        elif exit_code == 1 and last_success:
            age_hours = (now - last_success).total_seconds() / 3600.0
            if age_hours <= 24:
                color = '#e0a800'
                title = 'Backup mit Warnungen abgeschlossen'
            elif age_hours <= 48:
                color = '#e0a800'
                title = 'Backup mit Warnungen, aber nicht mehr tagesaktuell'
            else:
                color = '#dc3545'
                title = 'Letztes Backup hatte Warnungen und ist zu alt'
        elif exit_code is not None:
            color = '#dc3545'
            title = 'Letztes Backup fehlgeschlagen'

        return color, title


    def _on_schedule_change(self):
        self._save_config()
        self._schedule_next_backup()
        self._show_notice('Zeitplan und Wartungseinstellungen gespeichert.', level='success')

    def _schedule_next_backup(self):
        if hasattr(self, 'scheduled_job_id') and self.scheduled_job_id:
            self.master.after_cancel(self.scheduled_job_id)
            self.scheduled_job_id = None

        # Fallback: wenn schedule_type_var nicht existiert, aus config_data lesen
        mode = 'manual'
        if getattr(self, 'schedule_type_var', None) is not None:
            mode = self.schedule_type_var.get()
        if mode == 'manual':
            # Evtl. hat das Profil einen aktiven Zeitplan, der nicht in der UI ist
            mode = self.config_data.get('schedule_type', 'manual')
            if mode != 'manual' and hasattr(self, 'schedule_type_var'):
                self.schedule_type_var.set(mode)
        if mode == 'manual':
            self.next_backup_dt = None
            return

        now = datetime.datetime.now()
        candidate = None
        catchup = False
        if getattr(self, 'catchup_var', None):
            catchup = self.catchup_var.get()

        last_run_str = self.status_data.get('last_success_at', '')
        last_run = self._parse_iso_time(last_run_str)

        if mode == 'daily':
            raw_time = self.config_data.get('schedule_time', '03:15').strip()
            if hasattr(self, 'schedule_time_var') and self.schedule_time_var.get().strip():
                raw_time = self.schedule_time_var.get().strip()
            try:
                hour, minute = [int(x) for x in raw_time.split(':', 1)]
                candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                
                # Check for catchup
                if candidate <= now:
                    if catchup and (not last_run or (now - last_run).total_seconds() > 86400):
                        # Wir haben den heutigen Termin verpasst und das letzte Backup ist > 24h her!
                        candidate = now + datetime.timedelta(seconds=5)
                    else:
                        candidate += datetime.timedelta(days=1)
            except Exception:
                self.next_backup_dt = None
                return
        elif mode == 'interval':
            try:
                hours = int(self.schedule_interval_var.get() or 3)
            except ValueError:
                hours = 3
            if last_run:
                candidate = last_run + datetime.timedelta(hours=hours)
                if candidate <= now:
                    if catchup:
                        # Verpasstes Backup innerhalb von 5 Sekunden starten
                        candidate = now + datetime.timedelta(seconds=5)
                    else:
                        # In die Zukunft schieben
                        while candidate <= now:
                            candidate += datetime.timedelta(hours=hours)
            else:
                candidate = now + datetime.timedelta(seconds=5)

        if candidate:
            self.next_backup_dt = candidate
            delay_ms = int(max(0, (candidate - now).total_seconds()) * 1000)
            self.scheduled_job_id = self.master.after(max(delay_ms, 1000), self._scheduled_backup_trigger)

    def _scheduled_backup_trigger(self):
        self.scheduled_job_id = None
        if not self.backup_running:
            self._append_backup_log('\n>>> Geplanter täglicher Backup-Start\n')
            self._start_backup(from_scheduler=True)
        else:
            self._append_backup_log('\n[WARNUNG] Geplanter Start übersprungen: Backup läuft bereits.\n')
            # Nicht sofort neu schedulen (würde Endlosschleife erzeugen).
            # Stattdessen in 5 Minuten erneut prüfen ob das Backup fertig ist.
            self.scheduled_job_id = self.master.after(5 * 60 * 1000, self._scheduled_backup_trigger)
            self._update_dashboard_status()

    def _periodic_dashboard_refresh(self):
        self._update_dashboard_status()
        self.master.after(60000, self._periodic_dashboard_refresh)

    def _update_dashboard_status(self):
        last_run = self._parse_iso_time(self.status_data.get('last_run_at', ''))
        last_error = self.status_data.get('last_error', '')

        color, title = self._compute_status_traffic_light()

        self.status_canvas.itemconfig(self.status_light, fill=color)
        self.status_title_var.set(title)
        self._update_tray_status_icon(color)
        self._sync_running_tray_animation()
        self._update_tray_menu_labels()
        if self.backup_running and self.current_backup_started_at:
            runtime = int((datetime.datetime.now() - self.current_backup_started_at).total_seconds())
            self.last_backup_var.set(f'Letzter Lauf: läuft seit {runtime // 60} min {runtime % 60} s')
        else:
            formatted_last_run = self._format_time(self.status_data.get('last_run_at', ''))
            if formatted_last_run == '-':
                self.last_backup_var.set('Letzter Lauf: noch nie')
            else:
                self.last_backup_var.set(f'Letzter Lauf: {formatted_last_run}')

        if self.next_backup_dt:
            self.next_backup_var.set(f'Nächstes geplantes Backup: {self.next_backup_dt:%d.%m.%Y %H:%M}')
        else:
            self.next_backup_var.set('Nächstes geplantes Backup: deaktiviert')

        if self.backup_running:
            files_count = self.status_data.get('last_files_count', 0)
            source_size = self.status_data.get('last_source_size', '')
            dedupe_size = self.status_data.get('last_dedupe_size', '')
            progress_parts = [f'{files_count} Dateien'] if files_count else []
            if source_size:
                progress_parts.append(f'Quelle {source_size}')
            if dedupe_size:
                progress_parts.append(f'Dedupe {dedupe_size}')
            detail = ' | '.join(progress_parts) if progress_parts else 'arbeite...'
            self.last_error_var.set(f'Letzte Meldung: Backup läuft... {detail}')
        elif last_error:
            self.last_error_var.set(f'Letzte Meldung: {last_error}')
        elif last_run:
            duration = self.status_data.get('last_duration_sec', None)
            if isinstance(duration, (int, float)):
                self.last_error_var.set(f'Letzte Meldung: Erfolgreich ({int(duration)} s)')
            else:
                self.last_error_var.set('Letzte Meldung: Erfolgreich')
        else:
            self.last_error_var.set('Letzte Meldung: noch keine')

        # Canary-Status aktualisieren
        if hasattr(self, 'canary_status_var'):
            canary_result = self.config_data.get('canary_last_result', '')
            canary_check = self.config_data.get('canary_last_check', '')
            if canary_result == 'ok':
                self.canary_status_var.set(f'✅ Canary: OK ({canary_check[:16]})')
            elif canary_result == 'fail':
                self.canary_status_var.set(f'❌ Canary: FEHLER ({canary_check[:16]})')
            else:
                self.canary_status_var.set('⏳ Canary: noch kein Check')

        self._restore_stats_from_status()

    def _start_backup(self, from_scheduler=False):
        if self.backup_running:
            if not from_scheduler:
                self._show_notice('Es läuft bereits ein Backup.', level='info')
            return

        self._save_config()
        if not from_scheduler:
            self.notebook.select(self.tab_backup)
        repo = self._borg_repo()
        env = self._borg_env()

        includes = [line.strip() for line in self.include_text.get('1.0', tk.END).splitlines() if line.strip()]
        excludes = [line.strip() for line in self.exclude_text.get('1.0', tk.END).splitlines() if line.strip()]

        if not includes:
            messagebox.showerror('Fehler', 'Mindestens ein Include-Ordner muss angegeben sein.')
            return

        archive_name = f"{os.uname().nodename}-{datetime.datetime.now():%Y-%m-%d_%H-%M-%S}"
        compression = self.compression_var.get().strip() or 'lz4'

        self._reset_backup_progress()
        self.backup_task_queue = []
        self.backup_current_task = None

        # Canary-Check-Datei erstellen (vor dem Backup)
        canary_enabled = self.config_data.get('canary_enabled', True)
        if canary_enabled:
            canary_path = self._create_canary_file()
            # Canary-Verzeichnis automatisch zum Include hinzufuegen
            if canary_path not in includes:
                includes.append(canary_path)

        create_cmd = [BORG_BIN, 'create', '--lock-wait=30', '-v', '--stats', '--progress', f'--compression={compression}', f'{repo}::{archive_name}']
        create_cmd.extend(includes)
        for ex in excludes:
            create_cmd.extend(['--exclude', ex])
        self.backup_task_queue.append({'label': f'Starte Backup: {archive_name}', 'cmd': create_cmd, 'needs_root': True, 'kind': 'create'})

        run_prune = bool(getattr(self, 'prune_var', None) and self.prune_var.get())
        run_check = False
        if getattr(self, 'validate_var', None) and self.validate_var.get():
            try:
                last_check = self._parse_iso_time(self.status_data.get('last_check_at', ''))
                weeks = int(self.validate_interval_var.get())
                if not last_check or datetime.datetime.now() > last_check + datetime.timedelta(weeks=weeks):
                    run_check = True
            except Exception:
                run_check = True

        run_compact = False
        if getattr(self, 'optimize_var', None) and self.optimize_var.get():
            try:
                last_compact = self._parse_iso_time(self.status_data.get('last_compact_at', ''))
                weeks = int(self.optimize_interval_var.get())
                if not last_compact or datetime.datetime.now() > last_compact + datetime.timedelta(weeks=weeks):
                    run_compact = True
            except Exception:
                run_compact = True

        if run_prune:
            prune_cmd = [BORG_BIN, 'prune', '-v', '--list', '--stats', '--keep-daily=7', '--keep-weekly=4', '--keep-monthly=6', repo]
            self.backup_task_queue.append({'label': 'Starte Ausdünnen (Prune)', 'cmd': prune_cmd, 'needs_root': True, 'kind': 'prune'})
        if run_check:
            check_cmd = [BORG_BIN, 'check', '-v', '--repository-only', repo]
            self.backup_task_queue.append({'label': 'Starte Validierung (Check)', 'cmd': check_cmd, 'needs_root': True, 'kind': 'check'})
        if run_compact:
            compact_cmd = [BORG_BIN, 'compact', '-v', repo]
            self.backup_task_queue.append({'label': 'Starte Optimierung (Compact)', 'cmd': compact_cmd, 'needs_root': True, 'kind': 'compact'})

        self.runner.append_log(f'\n>>> Starte Backup: {archive_name}\n\n')
        self.backup_btn.config(state='disabled')
        self.stop_btn.config(state='normal')
        self.runner.stop_flag = False
        self.backup_running = True
        self.current_backup_started_at = datetime.datetime.now()
        self.status_data['last_run_at'] = self.current_backup_started_at.isoformat(timespec='seconds')
        self.status_data['last_error'] = ''
        self._save_status()
        self._schedule_tray_live_refresh(1000)
        self._update_dashboard_status()

        self._run_next_backup_task(repo)

    def _run_next_backup_task(self, repo=None):
        if not self.backup_task_queue:
            self._backup_done({'exit_code': 0, 'stopped': False, 'exception': ''})
            return

        task = self.backup_task_queue.pop(0)
        self.backup_current_task = task

        try:
            cmd, env = self._command_with_privilege(task['cmd'], self._borg_env(), needs_root=task.get('needs_root', False))
        except PermissionError as exc:
            self.backup_task_queue = []
            self.backup_current_task = None
            self.backup_running = False
            self.backup_btn.config(state='normal')
            self.stop_btn.config(state='disabled')
            self.status_data['last_error'] = str(exc)
            self._save_status()
            self._update_dashboard_status()
            self._append_backup_log(f'\n[HINWEIS] {exc}\n')
            messagebox.showinfo(
                'Einmalige Einrichtung erforderlich',
                f'{exc}\n\n'
                'Danach laufen Backup und Restore ohne Passwortabfrage im Hintergrund.'
            )
            return

        self.runner.append_log(f'\n>>> {task["label"]}\n')
        self.runner.stop_flag = False
        self.runner.run(cmd, env=env, done_callback=self._backup_task_done)

    def _backup_task_done(self, result):
        task = self.backup_current_task or {}
        kind = task.get('kind', '')

        if result.get('exit_code', -1) in (0, 1) and not result.get('stopped', False) and not result.get('exception', '').strip():
            if kind == 'check':
                self.status_data['last_check_at'] = datetime.datetime.now().isoformat(timespec='seconds')
            elif kind == 'compact':
                self.status_data['last_compact_at'] = datetime.datetime.now().isoformat(timespec='seconds')

            self._save_status()
            self._update_dashboard_status()
            self._run_next_backup_task()
            return

        self._backup_done(result)

    def _stop_backup(self):
        if self.runner:
            self.runner.stop()
        self.backup_btn.config(state='normal')
        self.stop_btn.config(state='disabled')

    def _backup_done(self, result):
        self.backup_btn.config(state='normal')
        self.stop_btn.config(state='disabled')

        self.backup_running = False
        finished_at = datetime.datetime.now()
        duration = None
        if self.current_backup_started_at:
            duration = (finished_at - self.current_backup_started_at).total_seconds()

        exit_code = result.get('exit_code', -1)
        stopped = bool(result.get('stopped', False))
        exception_text = result.get('exception', '').strip()

        self.status_data['last_files_count'] = self.status_data.get('last_files_count', self.backup_files_seen)
        self.status_data['last_source_size'] = self.status_data.get('last_source_size', '')
        self.status_data['last_dedupe_size'] = self.status_data.get('last_dedupe_size', '')
        self.status_data['last_item'] = self.status_data.get('last_item', self.backup_last_item)

        self.status_data['last_exit_code'] = exit_code
        self.status_data['last_duration_sec'] = duration

        if exit_code in (0, 1) and not stopped and not exception_text:
            self.status_data['last_success_at'] = finished_at.isoformat(timespec='seconds')
            # Canary-Check nach erfolgreichem Backup
            if self.config_data.get('canary_enabled', True):
                self.master.after(500, self._verify_canary)
            if exit_code == 1:
                self.status_data['last_error'] = 'Backup mit Warnungen beendet.'
                self._show_notice('Backup mit Warnungen beendet. Das Archiv wurde trotzdem erfolgreich geschrieben.', level='warning', timeout_ms=10000)
            else:
                self.status_data['last_error'] = ''
                self._show_notice('Backup erfolgreich beendet.', level='success', timeout_ms=10000)
        else:
            if stopped:
                self.status_data['last_error'] = 'Backup wurde manuell abgebrochen.'
            elif exception_text:
                self.status_data['last_error'] = f'Exception: {exception_text}'
            else:
                self.status_data['last_error'] = f'Borg Exit-Code: {exit_code}'
            self._show_notice(f"Backup nicht erfolgreich: {self.status_data['last_error']}", level='error', timeout_ms=12000)

        self.backup_task_queue = []
        self.backup_current_task = None
        self._save_status()
        self._schedule_next_backup()
        self._update_dashboard_status()

        if self.archive_list_refresh_pending:
            self.archive_list_refresh_pending = False
            self.master.after(1000, self._load_archive_list)
        else:
            # Nach jedem Backup Cache invalidieren + neu laden
            self._refresh_archive_cache()

    def _load_archive_list(self):
        self._save_config()
        profile_type = self.config_data.get('profile_type', 'ssh')
        profile_name = self.config_data.get('profile_name', 'Standard')
        repo = self._borg_repo()

        if self.backup_running:
            self.archive_list_refresh_pending = True
            self._show_notice('Archivliste wird nach dem laufenden Backup neu geladen.', level='info')
            return

        self.archive_loading_token += 1
        self._set_archive_loading(True, 'Archivliste wird geladen...')
        self.tree.delete(*self.tree.get_children())

        # Cache invalidieren
        self.archive_cache.pop(profile_name, None)
        self.cache_time = None

        env = self._borg_env()
        cmd = [BORG_BIN, 'list', '--lock-wait=30', repo, '--json']
        cmd, env = self._command_with_privilege(cmd, env, needs_root=False)

        self._append_backup_log('Lade Archivliste... (Timeout: 120s)\n')
        self.archive_runner.stop_flag = False
        self.archive_runner.run_capture(cmd, env=env, done_callback=self._on_archive_list_loaded)

    def _on_archive_list_loaded(self, result):
        ex = result.get('exception', '')
        if ex:
            self._set_archive_loading(False)
            messagebox.showerror('Fehler', str(ex))
            return
        if result['exit_code'] != 0:
            self._set_archive_loading(False)
            err = result['stderr'].strip() or result['stdout'].strip() or 'Unbekannter Fehler'
            # Bei leerem/nicht initiiertem Repository: freundliche Meldung
            if 'Repository' in err and ('does not exist' in err or 'not found' in err):
                self._set_archive_loading(False, 'Noch kein Repository initialisiert - führe zuerst ein Backup aus.')
                return
            messagebox.showerror('Fehler', f'borg list fehlgeschlagen:\n{err}')
            return
        try:
            profile_name = self.config_data.get('profile_name', 'Standard')
            profile_type = self.config_data.get('profile_type', 'ssh')
            data = json.loads(result['stdout'])
            archives = data.get('archives', [])
            if not archives:
                self._set_archive_loading(False, 'Keine Archive gefunden.')
                self.archive_cache[profile_name] = []
                self.cache_time = datetime.datetime.now()
                return

            token = self.archive_loading_token
            rows = []
            cache_list = []
            for archive in archives:
                name = archive.get('name') or archive.get('archive') or '?'
                time_str = archive.get('time', '?')
                size = archive.get('stats', {}).get('original_size', None)
                size_hr = self._human_size(size) if size else 'wird geladen...'
                item_id = self.tree.insert('', tk.END, text=name, values=(size_hr, time_str, profile_type))
                rows.append((item_id, name, size_hr, profile_type))
                cache_list.append((name, size_hr, time_str, profile_type))

            # Cache speichern
            self.archive_cache[profile_name] = cache_list
            self.cache_time = datetime.datetime.now()

            self._set_archive_loading(True, f'Archivgrößen werden geladen (0/{len(rows)})...')
            threading.Thread(target=self._populate_archive_sizes_async, args=(rows, token, profile_name), daemon=True).start()
        except Exception as exc:
            self._set_archive_loading(False)
            messagebox.showerror('Fehler', str(exc))

    def _set_archive_loading(self, is_loading, text=''):
        if hasattr(self, 'archive_status_var'):
            self.archive_status_var.set(text)
        if hasattr(self, 'archive_progress'):
            if is_loading:
                if not self.archive_progress.winfo_manager():
                    self.archive_progress.pack(side='left', padx=5)
                self.archive_progress.start(10)
            else:
                self.archive_progress.stop()
                if self.archive_progress.winfo_manager():
                    self.archive_progress.pack_forget()

    def _populate_archive_sizes_async(self, rows, token, profile_name=None):
        total = len(rows)
        for index, (item_id, archive_name, _, _ptype) in enumerate(rows, start=1):
            if token != self.archive_loading_token:
                return
            size_label = self._query_archive_size_label(archive_name)
            self.master.after(
                0,
                lambda iid=item_id, size=size_label, idx=index, count=total, current_token=token, pn=profile_name: self._update_archive_row_size(iid, size, idx, count, current_token, pn)
            )

    def _update_archive_row_size(self, item_id, size_label, index, total, token, profile_name=None):
        if token != self.archive_loading_token or not self.tree.exists(item_id):
            return

        values = list(self.tree.item(item_id, 'values'))
        if len(values) >= 2:
            values[0] = size_label
            self.tree.item(item_id, values=tuple(values))

        if index >= total:
            self._set_archive_loading(False, f'Archivliste geladen: {total} Archive')
            self._show_notice(f'Archivliste geladen: {total} Archive.', level='success')
        else:
            self._set_archive_loading(True, f'Archivgrößen werden geladen ({index}/{total})...')

    def _query_archive_size_label(self, archive_name):
        repo = self._borg_repo()
        env = self._borg_env()
        cmd = [BORG_BIN, 'info', '--lock-wait=30', f'{repo}::{archive_name}']
        cmd, env = self._command_with_privilege(cmd, env, needs_root=False)

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        except Exception:
            return 'n/a'

        if result.returncode != 0:
            return 'n/a'

        match = re.search(r'This archive:\s+([0-9.,]+\s+[KMGTPE]?i?B)', result.stdout)
        if match:
            return match.group(1)

        return 'n/a'

    def _human_size(self, size_bytes):
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size_bytes < 1024:
                return f'{size_bytes:.1f} {unit}'
            size_bytes /= 1024
        return f'{size_bytes:.1f} PB'

    def _mount_archive(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showerror('Fehler', 'Kein Archiv ausgewählt.')
            return

        name = self.tree.item(selected[0], 'text')
        repo = self._borg_repo()
        os.makedirs(self.mount_point, exist_ok=True)

        env = self._borg_env()
        cmd = [BORG_BIN, 'mount', '--lock-wait=30', f'{repo}::{name}', self.mount_point]
        cmd, env = self._command_with_privilege(cmd, env, needs_root=False)

        self._append_backup_log(f'Mounte Archiv {name}...\n')
        self.archive_runner.stop_flag = False
        self.archive_runner.run_capture(cmd, env=env, done_callback=self._on_mount_done)

    def _on_mount_done(self, result):
        ex = result.get('exception', '')
        if ex:
            messagebox.showerror('Fehler', str(ex))
            if hasattr(self, 'mount_btn'):
                self.mount_btn.config(text='Ausgewähltes Archiv mounten')
            return
        if result['exit_code'] != 0:
            err = result['stderr'].strip() or result['stdout'].strip() or 'Unbekannter Fehler'
            messagebox.showerror('Fehler', f'Mount fehlgeschlagen:\n{err}')
            if hasattr(self, 'mount_btn'):
                self.mount_btn.config(text='Ausgewähltes Archiv mounten')
            return
        self.mount_active = True
        selected = self.tree.selection()
        if selected:
            self.mounted_archive_name = self.tree.item(selected[0], 'text')
        if hasattr(self, 'mount_btn'):
            self.mount_btn.config(text='Unmounten')
        if hasattr(self, 'open_btn'):
            self.open_btn.config(state='normal')
        self._show_notice(f'Archiv gemountet auf {self.mount_point}', level='success', timeout_ms=5000)

    def _delete_archive(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showerror('Fehler', 'Kein Archiv ausgewählt.')
            return

        name = self.tree.item(selected[0], 'text')
        if not messagebox.askyesno('Löschen', f"Archiv '{name}' wirklich löschen?"):
            return

        repo = self._borg_repo()
        env = self._borg_env()
        cmd = [BORG_BIN, 'delete', '--lock-wait=30', f'{repo}::{name}']
        cmd, env = self._command_with_privilege(cmd, env, needs_root=False)

        self._append_backup_log(f'Lösche Archiv {name}...\n')
        self.archive_runner.stop_flag = False
        self.archive_runner.run_capture(cmd, env=env, done_callback=self._on_delete_done)

    def _on_delete_done(self, result):
        ex = result.get('exception', '')
        if ex:
            messagebox.showerror('Fehler', str(ex))
            return
        if result['exit_code'] != 0:
            err = result['stderr'].strip() or result['stdout'].strip() or 'Unbekannter Fehler'
            messagebox.showerror('Fehler', f'Löschen fehlgeschlagen:\n{err}')
            return
        messagebox.showinfo('Info', 'Archiv geloescht.')
        # Cache invalidieren
        pname = self.config_data.get('profile_name', 'Standard')
        self.archive_cache.pop(pname, None)
        self._load_archive_list()

    def _start_restore(self):
        self._save_config()
        archive = self.restore_archive_var.get().strip()
        target = self.restore_path_var.get().strip()

        if not archive or not target:
            messagebox.showerror('Fehler', 'Archivname und Zielverzeichnis angeben.')
            return
        if not os.path.isdir(target):
            messagebox.showerror('Fehler', 'Zielverzeichnis existiert nicht.')
            return

        self._append_restore_log(f'Starte Restore von {archive} nach {target}...\n')


def main():
    root = tk.Tk()
    BorgBackupGUI(root)
    root.mainloop()


if __name__ == '__main__':
    raise SystemExit(main())
