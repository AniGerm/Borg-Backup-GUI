"""
Borg Backup GUI – Hauptfenster (MainWindow).

Orchestriert Konfiguration, Tabs, Tray, Backup-Ausführung und Zeitplanung.
"""

import datetime
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

try:
    from PIL import Image, ImageTk
except Exception:
    Image = None
    ImageTk = None

from borg_backup_gui.config import (
    CONFIG_DIR, CONFIG_FILE, OLD_CONFIG_DIR, OLD_CONFIG_FILE,
    ConfigManager, Profile, ProfileStatus,
)
from borg_backup_gui.runner import CommandRunner, BORG_BIN
from borg_backup_gui.tray import TrayIconManager
from borg_backup_gui.backends import (
    get_backend, get_command_with_privilege, SSHBackend, S3Backend, LocalBackend,
)

from borg_backup_gui.tabs.dashboard import DashboardTab
from borg_backup_gui.tabs.backup import BackupTab
from borg_backup_gui.tabs.archive import ArchiveTab
from borg_backup_gui.tabs.schedule import ScheduleTab
from borg_backup_gui.tabs.restore import RestoreTab

# Konstanten (Pfade)
INSTANCE_LOCK_FILE = CONFIG_DIR / 'instance.lock'
INSTANCE_SOCKET_FILE = CONFIG_DIR / 'instance.sock'
ASSET_ICON_FILE = Path(__file__).resolve().parent.parent / 'assets' / 'borg-backup-gui.svg'

# Fallback auf altes Icon falls noch nicht umbenannt
if not ASSET_ICON_FILE.exists():
    ASSET_ICON_FILE = Path(__file__).resolve().parent.parent / 'assets' / 'hetzner-borg-gui.svg'


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


class MainWindow:
    """Hauptfenster der Borg Backup GUI."""

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

        # ---- Style ----
        self._setup_style(master)

        # ---- Config ----
        self.config = ConfigManager()

        # ---- State ----
        self.runner = None
        self.restore_runner = None
        self.archive_runner = None
        self.scheduled_job_id = None
        self.next_backup_dt = None
        self.backup_running = False
        self.current_backup_started_at = None
        self.backup_files_seen = 0
        self.backup_last_item = ''
        self.backup_task_queue = []
        self.backup_current_task = None
        self.archive_list_refresh_pending = False
        self.notice_after_id = None
        self.instance_server = None
        self.instance_server_thread = None

        # ---- Log Queue ----
        self.log_queue = queue.Queue()

        # ---- Window Icon ----
        self.window_icon_image = None
        self._set_window_icon()

        # ---- Notebook + Tabs ----
        self.notebook = ttk.Notebook(master)
        self.notebook.pack(fill='both', expand=True, padx=5, pady=5)

        # Notice-Bar
        self.notice_var = tk.StringVar(value='')
        self.notice_label = tk.Label(
            master, textvariable=self.notice_var, anchor='w',
            bg='#111827', fg='#cbd5e1', padx=10, pady=6
        )
        self.notice_label.pack(fill='x', side='bottom')

        # Tabs
        self.tab_dashboard = DashboardTab(self.notebook, self)
        self.tab_backup = BackupTab(self.notebook, self)
        self.tab_archive = ArchiveTab(self.notebook, self)
        self.tab_schedule = ScheduleTab(self.notebook, self)
        self.tab_restore = RestoreTab(self.notebook, self)

        # ---- Runner ----
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

        # ---- Tray ----
        self.tray = TrayIconManager(self)

        # ---- Initialisierung ----
        self.master.after(100, self._process_log_queue)
        self.reschedule_all()
        self.tab_dashboard.refresh()
        self.master.after(1200, self.tab_dashboard.refresh)
        self.master.after(700, lambda: self.tray.setup())
        self.master.after(60000, self._periodic_dashboard_refresh)

        # Window-Events
        self.master.protocol('WM_DELETE_WINDOW', self._on_close)
        self.master.bind("<<ShowWindow>>", lambda e: self._show_window())
        self.master.bind("<<TrayBackupToggle>>", lambda e: self._tray_backup_toggle())
        self.master.bind("<<QuitApp>>", lambda e: self._quit_app())

        # Instance-Server (Single-Instance)
        try:
            self._setup_instance_server()
        except SystemExit:
            master.destroy()
            import sys
            sys.exit(0)

        self.master.after(120, self._ensure_window_visible)
        self.master.after(30000, self._periodic_health_check)

        # Profile von alter Config laden/anzeigen
        self.on_profile_changed()

        # Root-Warnung
        if os.geteuid() == 0:
            messagebox.showwarning(
                'Hinweis',
                'Die GUI sollte als normaler User laufen, damit das Tray-Symbol sichtbar bleibt.\n\n'
                'Root-Rechte werden bei Borg-Befehlen automatisch angefordert.'
            )

    def _setup_style(self, master):
        """Setzt das Tkinter-Theme (dunkles Design)."""
        self.style = ttk.Style()
        self.style.theme_use('clam')

        bg_color = '#1f2937'
        fg_color = '#f8fafc'
        acc_color = '#0f766e'
        btn_color = '#374151'

        master.configure(bg=bg_color)
        master.option_add('*TCombobox*Listbox.background', '#374151')
        master.option_add('*TCombobox*Listbox.foreground', '#f8fafc')
        master.option_add('*TCombobox*Listbox.selectBackground', '#0f766e')
        master.option_add('*TCombobox*Listbox.selectForeground', '#ffffff')
        master.option_add('*Listbox.background', '#374151')
        master.option_add('*Listbox.foreground', '#f8fafc')
        master.option_add('*Listbox.selectBackground', '#0f766e')
        master.option_add('*Listbox.selectForeground', '#ffffff')

        self.style.configure('.', background=bg_color, foreground=fg_color)
        self.style.configure('TNotebook', background=bg_color, borderwidth=0)
        self.style.configure('TNotebook.Tab', padding=[20, 8], font=('Segoe UI', 10, 'bold'),
                             background='#374151', foreground=fg_color, borderwidth=0)
        self.style.map('TNotebook.Tab', background=[('selected', acc_color)],
                       foreground=[('selected', '#ffffff')])
        self.style.configure('TFrame', background=bg_color)
        self.style.configure('TLabel', background=bg_color, foreground=fg_color, font=('Segoe UI', 10))
        self.style.configure('TButton', font=('Segoe UI', 10, 'bold'), padding=8,
                             background=btn_color, foreground=fg_color, borderwidth=0)
        self.style.map('TButton', background=[('active', acc_color), ('disabled', '#475569')])
        self.style.configure('TEntry', fieldbackground='#374151', foreground=fg_color,
                             insertcolor=fg_color, padding=5, borderwidth=0)
        self.style.configure('TSpinbox', fieldbackground='#374151', foreground=fg_color,
                             background=btn_color, arrowcolor=fg_color, insertcolor=fg_color, padding=4)
        self.style.configure('TCombobox', fieldbackground='#374151', foreground=fg_color, background=btn_color)
        self.style.configure('TLabelframe', background=bg_color, foreground=fg_color,
                             borderwidth=1, bordercolor='#475569')
        self.style.configure('TLabelframe.Label', background=bg_color, foreground=acc_color,
                             font=('Segoe UI', 11, 'bold'))
        self.style.configure('Treeview', background='#374151', foreground=fg_color,
                             fieldbackground='#374151', borderwidth=0, rowheight=25)
        self.style.map('Treeview', background=[('selected', acc_color)])
        self.style.configure('Treeview.Heading', background=btn_color, foreground=fg_color,
                             font=('Segoe UI', 10, 'bold'), padding=5)

    def _set_window_icon(self):
        if not Image or not ImageTk:
            return
        try:
            img = Image.new('RGBA', (64, 64), (34, 197, 94, 255))
            self.window_icon_image = ImageTk.PhotoImage(img)
            self.master.iconphoto(True, self.window_icon_image)
        except Exception:
            pass

    # ============================================================
    # Profil-Wechsel
    # ============================================================

    def on_profile_changed(self):
        """Wird aufgerufen, wenn das aktive Profil gewechselt wurde."""
        profile = self.config.get_active_profile()
        if profile:
            self.tab_backup.load_from_profile()
            self.tab_schedule.load_from_profile()
            self.tab_dashboard.refresh_profile_list()
            self.tab_backup.refresh_profile_list()
            self.tab_schedule.refresh_profile_list()
            self.tab_archive.refresh_profile_list()
            self.tab_restore.refresh_profile_list()

    def save_config(self):
        """Speichert die Konfiguration."""
        self.config.save()

    # ============================================================
    # Notice-Bar
    # ============================================================

    def show_notice(self, text, level='info', timeout_ms=7000):
        palette = {
            'info': ('#0f172a', '#e2e8f0'),
            'success': ('#14532d', '#dcfce7'),
            'warning': ('#78350f', '#fef3c7'),
            'error': ('#7f1d1d', '#fee2e2'),
        }
        bg, fg = palette.get(level, palette['info'])
        self.notice_var.set(text)
        self.notice_label.configure(bg=bg, fg=fg)
        if self.notice_after_id:
            self.master.after_cancel(self.notice_after_id)
            self.notice_after_id = None
        if timeout_ms:
            self.notice_after_id = self.master.after(timeout_ms, self._clear_notice)

    def _clear_notice(self):
        self.notice_after_id = None
        if hasattr(self, 'notice_var'):
            self.notice_var.set('')
        if hasattr(self, 'notice_label'):
            self.notice_label.configure(bg='#111827', fg='#cbd5e1')

    # ============================================================
    # Log Queue
    # ============================================================

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

            if target == 'backup':
                self._update_backup_progress(text)
                self.tab_backup.append_log(text)
            elif target == 'restore':
                self.tab_restore._append_log(text)

        # Tray-Live-Update
        if self.backup_running and self.tray and self.tray.tray_runtime_available and \
                self.config.global_settings.get('tray_enabled', True):
            now = datetime.datetime.now()
            if not hasattr(self, '_tray_menu_last_refresh') or \
                    (now - getattr(self, '_tray_menu_last_refresh', datetime.datetime.min)).total_seconds() >= 1.0:
                self._tray_menu_last_refresh = now
                if self.tray:
                    self.tray._update_menu_labels()

        self.master.after(100, self._process_log_queue)

    def _update_backup_progress(self, text):
        """Parst Borg-Output und aktualisiert Live-Statistiken."""
        line = text.strip()
        if not line:
            return

        profile = self.config.get_active_profile()
        status = self.config.get_status(profile.name) if profile else None
        if not status:
            return

        progress_match = re.match(
            r'^([0-9.,]+\s*[KMGTPE]?i?B)\s+O\s+([0-9.,]+\s*[KMGTPE]?i?B)\s+C\s+([0-9.,]+\s*[KMGTPE]?i?B)\s+D\s+(\d+)\s+N\s+(.+)$',
            line
        )
        if progress_match:
            status.last_source_size = progress_match.group(1)
            status.last_dedupe_size = progress_match.group(3)
            status.last_files_count = int(progress_match.group(4))
            status.last_item = progress_match.group(5)[:110]
            if profile:
                self.config.save_status(profile.name)
            self.tab_dashboard.refresh()
            return

        if line.startswith('/') or line.startswith('./') or line.startswith('../'):
            self.backup_files_seen += 1
            status.last_files_count = self.backup_files_seen
            status.last_item = line[:110]
            self.tab_dashboard.refresh()

        match_files = re.search(r'(?:Number of files|Files)\s*:\s*(\d+)', line)
        if match_files:
            status.last_files_count = int(match_files.group(1))
            self.tab_dashboard.refresh()

        match_source = re.search(r'(?:Original size|This archive)\s*:\s*([0-9.,]+\s*[KMGTPE]?i?B)', line)
        if match_source:
            status.last_source_size = match_source.group(1)
            self.tab_dashboard.refresh()

        match_dedupe = re.search(r'Deduplicated size\s*:\s*([0-9.,]+\s*[KMGTPE]?i?B)', line)
        if match_dedupe:
            status.last_dedupe_size = match_dedupe.group(1)
            self.tab_dashboard.refresh()

        match_archive = re.search(
            r'This archive:\s+([0-9.,]+\s*[KMGTPE]?i?B)\s+([0-9.,]+\s*[KMGTPE]?i?B)\s+([0-9.,]+\s*[KMGTPE]?i?B)',
            line
        )
        if match_archive:
            status.last_source_size = match_archive.group(1)
            status.last_dedupe_size = match_archive.group(3)
            self.tab_dashboard.refresh()

    def refresh_stats_from_latest_archive(self):
        """Lädt Statistik des letzten Archivs im Hintergrund."""
        profile = self.config.get_active_profile()
        if not profile or self.backup_running:
            return

        status = self.config.get_status(profile.name)
        if not status.last_success_at:
            return
        if status.last_files_count and status.last_source_size and status.last_dedupe_size and status.last_item:
            return

        self.show_notice('Statistik wird geladen...', level='info', timeout_ms=4000)
        threading.Thread(target=self._refresh_stats_worker, daemon=True).start()

    def _refresh_stats_worker(self):
        profile = self.config.get_active_profile()
        if not profile:
            return
        backend = get_backend(profile)
        env = backend.get_env(profile)
        repo = backend.get_repo_url(profile)
        cmd = [BORG_BIN, 'info', '--lock-wait=30', '--last', '1', repo]
        cmd, env = get_command_with_privilege(cmd, env, needs_root=False, is_root=os.geteuid() == 0)

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=90)
        except Exception:
            return
        if result.returncode != 0:
            return

        files_match = re.search(r'Number of files:\s*(\d+)', result.stdout)
        name_match = re.search(r'Archive name:\s*(.+)', result.stdout)
        totals_match = re.search(
            r'This archive:\s+([0-9.,]+\s*[KMGTPE]?i?B)\s+([0-9.,]+\s*[KMGTPE]?i?B)\s+([0-9.,]+\s*[KMGTPE]?i?B)',
            result.stdout
        )

        def _apply():
            p = self.config.get_active_profile()
            if not p or p.name != profile.name:
                return
            s = self.config.get_status(p.name)
            if files_match:
                s.last_files_count = int(files_match.group(1))
            if name_match:
                s.last_item = f"Archiv {name_match.group(1).strip()}"
            if totals_match:
                s.last_source_size = totals_match.group(1)
                s.last_dedupe_size = totals_match.group(3)
            self.config.save_status(p.name)
            self.tab_dashboard.refresh()

        self.master.after(0, _apply)

    # ============================================================
    # Backup-Ausführung
    # ============================================================

    def start_backup(self, from_scheduler=False):
        """Startet ein Backup für das aktive Profil."""
        if self.backup_running:
            if not from_scheduler:
                self.show_notice('Es läuft bereits ein Backup.', level='info')
            return

        self.save_config()
        if not from_scheduler:
            self.notebook.select(self.tab_backup.frame)

        profile = self.config.get_active_profile()
        if not profile:
            messagebox.showerror('Fehler', 'Kein Profil ausgewählt.')
            return

        backend = get_backend(profile)
        env = backend.get_env(profile)
        repo = backend.get_repo_url(profile)

        includes = profile.include_folders
        excludes = profile.exclude_folders

        if not includes:
            messagebox.showerror('Fehler', 'Mindestens ein Include-Ordner muss angegeben sein.')
            return

        archive_name = f"{os.uname().nodename}-{datetime.datetime.now():%Y-%m-%d_%H-%M-%S}"
        compression = profile.compression or 'lz4'

        self._reset_backup_progress()
        self.backup_task_queue = []
        self.backup_current_task = None

        create_cmd = [BORG_BIN, 'create', '-v', '--stats', '--progress',
                       f'--compression={compression}', f'{repo}::{archive_name}']
        create_cmd.extend(includes)
        for ex in excludes:
            create_cmd.extend(['--exclude', ex])
        self.backup_task_queue.append({
            'label': f'Starte Backup: {archive_name}',
            'cmd': create_cmd,
            'needs_root': True,
            'kind': 'create'
        })

        # Wartungsaufgaben
        run_prune = profile.prune_enabled
        run_check = False
        if profile.validate_enabled:
            try:
                last_check = self._parse_iso(self.config.get_status(profile.name).last_check_at)
                weeks = profile.validate_interval
                if not last_check or datetime.datetime.now() > last_check + datetime.timedelta(weeks=weeks):
                    run_check = True
            except Exception:
                run_check = True

        run_compact = False
        if profile.optimize_enabled:
            try:
                last_compact = self._parse_iso(self.config.get_status(profile.name).last_compact_at)
                weeks = profile.optimize_interval
                if not last_compact or datetime.datetime.now() > last_compact + datetime.timedelta(weeks=weeks):
                    run_compact = True
            except Exception:
                run_compact = True

        if run_prune:
            prune_cmd = [BORG_BIN, 'prune', '-v', '--list', '--stats',
                         '--keep-daily=7', '--keep-weekly=4', '--keep-monthly=6', repo]
            self.backup_task_queue.append({'label': 'Starte Ausdünnen (Prune)', 'cmd': prune_cmd,
                                           'needs_root': True, 'kind': 'prune'})
        if run_check:
            check_cmd = [BORG_BIN, 'check', '-v', '--repository-only', repo]
            self.backup_task_queue.append({'label': 'Starte Validierung (Check)', 'cmd': check_cmd,
                                           'needs_root': True, 'kind': 'check'})
        if run_compact:
            compact_cmd = [BORG_BIN, 'compact', '-v', repo]
            self.backup_task_queue.append({'label': 'Starte Optimierung (Compact)', 'cmd': compact_cmd,
                                           'needs_root': True, 'kind': 'compact'})

        self.runner.append_log(f'\n>>> Starte Backup: {archive_name}\n\n')
        self.tab_backup.set_backup_buttons(True)
        self.runner.stop_flag = False
        self.backup_running = True
        self.current_backup_started_at = datetime.datetime.now()

        status = self.config.get_status(profile.name)
        status.last_run_at = self.current_backup_started_at.isoformat(timespec='seconds')
        status.last_error = ''
        status.last_files_count = 0
        self.config.save_status(profile.name)

        if self.tray:
            self.tray._schedule_live_refresh(1000)
        self.tab_dashboard.refresh()

        self._run_next_backup_task()

    def _reset_backup_progress(self):
        self.backup_files_seen = 0
        self.backup_last_item = ''
        profile = self.config.get_active_profile()
        if profile:
            status = self.config.get_status(profile.name)
            status.last_files_count = 0
            status.last_source_size = ''
            status.last_dedupe_size = ''
            status.last_item = ''

    def _run_next_backup_task(self, repo=None):
        if not self.backup_task_queue:
            self._backup_done({'exit_code': 0, 'stopped': False, 'exception': ''})
            return

        task = self.backup_task_queue.pop(0)
        self.backup_current_task = task

        try:
            cmd, env = get_command_with_privilege(
                task['cmd'], get_backend(self.config.get_active_profile()).get_env(self.config.get_active_profile()),
                needs_root=task.get('needs_root', False),
                is_root=os.geteuid() == 0
            )
        except PermissionError as exc:
            self.backup_task_queue = []
            self.backup_current_task = None
            self.backup_running = False
            self.tab_backup.set_backup_buttons(False)
            profile = self.config.get_active_profile()
            if profile:
                status = self.config.get_status(profile.name)
                status.last_error = str(exc)
                self.config.save_status(profile.name)
            self.tab_dashboard.refresh()
            self.runner.append_log(f'\n[HINWEIS] {exc}\n')
            messagebox.showinfo('Einmalige Einrichtung erforderlich',
                                f'{exc}\n\nDanach laufen Backup und Restore ohne Passwortabfrage.')
            return

        self.runner.append_log(f'\n>>> {task["label"]}\n')
        self.runner.stop_flag = False
        self.runner.run(cmd, env=env, done_callback=self._backup_task_done)

    def _backup_task_done(self, result):
        task = self.backup_current_task or {}
        kind = task.get('kind', '')

        if result.get('exit_code', -1) in (0, 1) and not result.get('stopped', False) and \
                not result.get('exception', '').strip():
            profile = self.config.get_active_profile()
            if profile:
                status = self.config.get_status(profile.name)
                if kind == 'check':
                    status.last_check_at = datetime.datetime.now().isoformat(timespec='seconds')
                elif kind == 'compact':
                    status.last_compact_at = datetime.datetime.now().isoformat(timespec='seconds')
                self.config.save_status(profile.name)
            self.tab_dashboard.refresh()
            self._run_next_backup_task()
            return

        self._backup_done(result)

    def stop_backup(self):
        if self.runner:
            self.runner.stop()
        self.tab_backup.set_backup_buttons(False)

    def _backup_done(self, result):
        self.tab_backup.set_backup_buttons(False)
        self.backup_running = False
        finished_at = datetime.datetime.now()

        duration = None
        if self.current_backup_started_at:
            duration = (finished_at - self.current_backup_started_at).total_seconds()

        exit_code = result.get('exit_code', -1)
        stopped = bool(result.get('stopped', False))
        exception_text = result.get('exception', '').strip()

        profile = self.config.get_active_profile()
        status = self.config.get_status(profile.name) if profile else None

        if status:
            status.last_exit_code = exit_code
            status.last_duration_sec = duration

            if exit_code in (0, 1) and not stopped and not exception_text:
                status.last_success_at = finished_at.isoformat(timespec='seconds')
                if exit_code == 1:
                    status.last_error = 'Backup mit Warnungen beendet.'
                    self.show_notice('Backup mit Warnungen beendet.', level='warning', timeout_ms=10000)
                else:
                    status.last_error = ''
                    self.show_notice('Backup erfolgreich beendet.', level='success', timeout_ms=10000)
            else:
                if stopped:
                    status.last_error = 'Backup wurde manuell abgebrochen.'
                elif exception_text:
                    status.last_error = f'Exception: {exception_text}'
                else:
                    status.last_error = f'Borg Exit-Code: {exit_code}'
                self.show_notice(f"Backup nicht erfolgreich: {status.last_error}", level='error', timeout_ms=12000)

            self.config.save_status(profile.name)

        self.backup_task_queue = []
        self.backup_current_task = None

        # Zeitplan nächster Durchlauf
        self.reschedule_all()
        self.tab_dashboard.refresh()

        # Archiv-Refresh
        if self.archive_list_refresh_pending:
            self.archive_list_refresh_pending = False
            self.master.after(1000, self.tab_archive.request_background_refresh)
        elif profile:
            self.tab_archive.invalidate_cache(profile.name)
            self.tab_archive.request_background_refresh()

    def _tray_backup_toggle(self):
        if self.backup_running:
            self.stop_backup()
            self.show_notice('Backup wird abgebrochen...', level='warning')
        else:
            self.start_backup()

    # ============================================================
    # Zeitplanung
    # ============================================================

    def _parse_iso(self, value):
        if not value:
            return None
        try:
            return datetime.datetime.fromisoformat(value)
        except ValueError:
            return None

    def reschedule_all(self):
        """Berechnet Zeitpläne für alle aktiven Profile."""
        if self.scheduled_job_id:
            self.master.after_cancel(self.scheduled_job_id)
            self.scheduled_job_id = None

        # Nur das aktive Profil plant den nächsten Lauf
        profile = self.config.get_active_profile()
        if not profile or profile.schedule_type == 'manual' or not profile.enabled:
            self.next_backup_dt = None
            return

        now = datetime.datetime.now()
        candidate = None

        last_run_str = self.config.get_status(profile.name).last_success_at
        last_run = self._parse_iso(last_run_str)

        if profile.schedule_type == 'daily':
            try:
                hour, minute = [int(x) for x in profile.schedule_time.split(':', 1)]
                candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if candidate <= now:
                    if profile.catchup_missed and (not last_run or (now - last_run).total_seconds() > 86400):
                        candidate = now + datetime.timedelta(seconds=5)
                    else:
                        candidate += datetime.timedelta(days=1)
            except Exception:
                self.next_backup_dt = None
                return
        elif profile.schedule_type == 'interval':
            hours = profile.schedule_interval
            if last_run:
                candidate = last_run + datetime.timedelta(hours=hours)
                if candidate <= now:
                    if profile.catchup_missed:
                        candidate = now + datetime.timedelta(seconds=5)
                    else:
                        while candidate <= now:
                            candidate += datetime.timedelta(hours=hours)
            else:
                candidate = now + datetime.timedelta(seconds=5)

        if candidate:
            self.next_backup_dt = candidate
            delay_ms = int(max(0, (candidate - now).total_seconds()) * 1000)
            self.scheduled_job_id = self.master.after(
                max(delay_ms, 1000), self._scheduled_backup_trigger
            )

    def _scheduled_backup_trigger(self):
        self.scheduled_job_id = None
        if not self.backup_running:
            self._append_backup_log('\n>>> Geplanter Backup-Start\n')
            self.start_backup(from_scheduler=True)
        else:
            self._append_backup_log('\n[WARNUNG] Geplanter Start übersprungen: Backup läuft bereits.\n')
            self.scheduled_job_id = self.master.after(5 * 60 * 1000, self._scheduled_backup_trigger)
            self.tab_dashboard.refresh()

    # ============================================================
    # Profil-Dialog
    # ============================================================

    def show_profile_dialog(self, edit_profile: Profile | None = None):
        """Zeigt Dialog zum Erstellen/Bearbeiten eines Profils."""
        dialog = tk.Toplevel(self.master)
        dialog.title('Profil bearbeiten' if edit_profile else 'Neues Profil')
        dialog.geometry('500x500')
        dialog.transient(self.master)
        dialog.grab_set()
        dialog.configure(bg='#1f2937')

        is_new = edit_profile is None
        profile = edit_profile if edit_profile else Profile(name='Neues Profil', type='ssh')

        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill='both', expand=True)

        row = 0
        ttk.Label(frame, text='Profil-Name:').grid(row=row, column=0, sticky='e', padx=5, pady=5)
        name_var = tk.StringVar(value=profile.name)
        ttk.Entry(frame, textvariable=name_var, width=40).grid(row=row, column=1, padx=5, pady=5)

        row += 1
        ttk.Label(frame, text='Backend-Typ:').grid(row=row, column=0, sticky='e', padx=5, pady=5)
        type_var = tk.StringVar(value=profile.type)
        type_combo = ttk.Combobox(frame, textvariable=type_var, values=['ssh', 's3', 'local'],
                                   state='readonly', width=15)
        type_combo.grid(row=row, column=1, sticky='w', padx=5, pady=5)

        # SSH-Felder
        ssh_frame = ttk.LabelFrame(frame, text='SSH Einstellungen')
        ssh_frame.grid(row=row + 1, column=0, columnspan=2, sticky='ew', pady=5)
        ssh_frame.columnconfigure(1, weight=1)
        ttk.Label(ssh_frame, text='Repository URL:').grid(row=0, column=0, sticky='e', padx=5, pady=2)
        storage_var = tk.StringVar(value=profile.storage)
        ttk.Entry(ssh_frame, textvariable=storage_var, width=50).grid(row=0, column=1, padx=5, pady=2)
        ttk.Label(ssh_frame, text='SSH Key:').grid(row=1, column=0, sticky='e', padx=5, pady=2)
        ssh_key_var = tk.StringVar(value=profile.ssh_key)
        ttk.Entry(ssh_frame, textvariable=ssh_key_var, width=50).grid(row=1, column=1, padx=5, pady=2)

        # S3-Felder
        s3_frame = ttk.LabelFrame(frame, text='S3 Einstellungen')
        s3_frame.grid(row=row + 2, column=0, columnspan=2, sticky='ew', pady=5)
        s3_frame.columnconfigure(1, weight=1)
        ttk.Label(s3_frame, text='Bucket Name:').grid(row=0, column=0, sticky='e', padx=5, pady=2)
        s3_bucket_var = tk.StringVar(value=profile.storage if profile.type == 's3' else '')
        ttk.Entry(s3_frame, textvariable=s3_bucket_var, width=40).grid(row=0, column=1, padx=5, pady=2)
        ttk.Label(s3_frame, text='Access Key:').grid(row=1, column=0, sticky='e', padx=5, pady=2)
        s3_access_var = tk.StringVar(value=profile.s3_access_key)
        ttk.Entry(s3_frame, textvariable=s3_access_var, width=40).grid(row=1, column=1, padx=5, pady=2)
        ttk.Label(s3_frame, text='Secret Key:').grid(row=2, column=0, sticky='e', padx=5, pady=2)
        s3_secret_var = tk.StringVar(value=profile.s3_secret_key)
        ttk.Entry(s3_frame, textvariable=s3_secret_var, width=40, show='*').grid(row=2, column=1, padx=5, pady=2)
        ttk.Label(s3_frame, text='Endpoint URL:').grid(row=3, column=0, sticky='e', padx=5, pady=2)
        s3_endpoint_var = tk.StringVar(value=profile.s3_endpoint_url or 'https://fsn1.your-objectstorage.com')
        ttk.Entry(s3_frame, textvariable=s3_endpoint_var, width=40).grid(row=3, column=1, padx=5, pady=2)

        # Local-Felder
        local_frame = ttk.LabelFrame(frame, text='Lokale Einstellungen')
        local_frame.grid(row=row + 3, column=0, columnspan=2, sticky='ew', pady=5)
        local_frame.columnconfigure(1, weight=1)
        ttk.Label(local_frame, text='Repository Pfad:').grid(row=0, column=0, sticky='e', padx=5, pady=2)
        local_path_var = tk.StringVar(value=profile.local_path or '/mnt/backup/borg-repo')
        ttk.Entry(local_frame, textvariable=local_path_var, width=50).grid(row=0, column=1, padx=5, pady=2)

        # Passwort (für alle Typen)
        row += 4
        ttk.Label(frame, text='Passphrase:').grid(row=row, column=0, sticky='e', padx=5, pady=5)
        passphrase_var = tk.StringVar(value=profile.passphrase)
        ttk.Entry(frame, textvariable=passphrase_var, width=40, show='*').grid(row=row, column=1, padx=5, pady=5)

        row += 1
        enabled_var = tk.BooleanVar(value=profile.enabled)
        ttk.Checkbutton(frame, text='Profil aktivieren', variable=enabled_var).grid(
            row=row, column=0, columnspan=2, padx=5, pady=5)

        # Frame-Sichtbarkeit je nach Typ
        def _on_type_change(*_args):
            t = type_var.get()
            ssh_frame.grid_remove()
            s3_frame.grid_remove()
            local_frame.grid_remove()
            if t == 'ssh':
                ssh_frame.grid()
            elif t == 's3':
                s3_frame.grid()
            elif t == 'local':
                local_frame.grid()

        type_var.trace_add('write', _on_type_change)
        _on_type_change()

        # Buttons
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=row + 1, column=0, columnspan=2, pady=20)
        ttk.Button(btn_frame, text='Abbrechen', command=dialog.destroy).pack(side='left', padx=5)

        def _save():
            ptype = type_var.get()
            new_profile = Profile(
                name=name_var.get().strip() or 'Neues Profil',
                type=ptype,
                storage=storage_var.get().strip() if ptype == 'ssh' else (s3_bucket_var.get().strip() if ptype == 's3' else ''),
                ssh_key=ssh_key_var.get().strip(),
                s3_access_key=s3_access_var.get().strip(),
                s3_secret_key=s3_secret_var.get().strip(),
                s3_endpoint_url=s3_endpoint_var.get().strip(),
                s3_region='',
                local_path=local_path_var.get().strip(),
                passphrase=passphrase_var.get(),
                enabled=enabled_var.get(),
            )

            if is_new:
                self.config.add_profile(new_profile)
                self.config.set_active_profile(new_profile.name)
            else:
                self.config.update_profile(new_profile)

            self.on_profile_changed()
            self.tab_dashboard.refresh()
            dialog.destroy()
            self.show_notice(f'Profil "{new_profile.name}" {"erstellt" if is_new else "aktualisiert"}.', level='success')

        ttk.Button(btn_frame, text='Speichern', command=_save).pack(side='left', padx=5)

        self.master.wait_window(dialog)

    # ============================================================
    # Fenster-Management
    # ============================================================

    def _on_close(self):
        tray_enabled = bool(self.config.global_settings.get('tray_enabled', True))
        if self.tray and self.tray.tray_runtime_available and tray_enabled:
            self.save_config()
            self._hide_window_to_tray()
            return
        self._quit_app()

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
        if self.tray and not self.tray.tray_hint_shown:
            self.tray.tray_hint_shown = True
            self.show_notice('App läuft im Hintergrund. Öffnen über das Tray-Symbol.', level='info')
        self.master.after(10000, self._ensure_window_visible_if_tray_dead)

    def _ensure_window_visible_if_tray_dead(self):
        if self.master.state() == 'withdrawn':
            if self.tray and not self.tray.tray_ready:
                self._show_window()

    def _quit_app(self):
        try:
            self.save_config()
        finally:
            self._cleanup_instance_server()
            self.master.destroy()
            if self.tray:
                self.tray.stop()

    # ============================================================
    # Instance-Server (Single-Instance)
    # ============================================================

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
        except SystemExit:
            raise
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

    # ============================================================
    # Periodische Tasks
    # ============================================================

    def _periodic_dashboard_refresh(self):
        self.tab_dashboard.refresh()
        self.master.after(60000, self._periodic_dashboard_refresh)

    def _periodic_health_check(self):
        """Gesundheitscheck – aktuell nur weiterplanen."""
        self.master.after(30000, self._periodic_health_check)


def main():
    """Einstiegspunkt für die Borg Backup GUI."""
    root = tk.Tk()
    MainWindow(root)
    root.mainloop()


if __name__ == '__main__':
    raise SystemExit(main())
