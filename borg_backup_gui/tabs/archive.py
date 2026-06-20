"""Archiv-Tab: Archivliste laden, Größen anzeigen, mounten, löschen."""

import datetime
import json
import os
import re
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from borg_backup_gui.backends import get_backend, get_command_with_privilege
from borg_backup_gui.runner import BORG_BIN


class ArchiveTab:
    """Tab für Archivverwaltung: Liste, Mount, Delete."""

    def __init__(self, notebook, app):
        self.app = app
        self.frame = ttk.Frame(notebook)
        notebook.add(self.frame, text='Archiv und Restore')
        self._build_ui()

        # Cache
        self.cached_archives = {}       # profile_name -> list of archive dicts
        self.cache_timestamp = {}        # profile_name -> datetime
        self.cache_valid_seconds = 300   # 5 Minuten
        self.archive_loading_token = 0
        self.mount_active = False
        self.mount_point = Path('/tmp/borg-mount')

    def _build_ui(self):
        tab = self.frame
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        # Profil-Auswahl
        profile_frame = ttk.Frame(tab)
        profile_frame.grid(row=0, column=0, sticky='ew', padx=10, pady=(10, 0))
        ttk.Label(profile_frame, text='Profil:').pack(side='left', padx=5)
        self.profile_combo = ttk.Combobox(profile_frame, state='readonly', width=35)
        self.profile_combo.pack(side='left', padx=5)
        self.profile_combo.bind('<<ComboboxSelected>>', self._on_profile_change)

        top = ttk.Frame(tab)
        top.grid(row=1, column=0, sticky='ew', padx=10, pady=5)
        self.load_btn = ttk.Button(top, text='Archivliste laden', command=self._load_archive_list)
        self.load_btn.pack(side='left', padx=5)
        self.mount_btn = ttk.Button(top, text='Ausgewähltes Archiv mounten', command=self._toggle_mount)
        self.mount_btn.pack(side='left', padx=5)
        self.open_btn = ttk.Button(top, text='In Dateien öffnen', command=self._open_in_filemanager, state='disabled')
        self.open_btn.pack(side='left', padx=5)
        self.delete_btn = ttk.Button(top, text='Ausgewähltes Archiv löschen', command=self._delete_archive)
        self.delete_btn.pack(side='left', padx=5)
        self.archive_status_var = tk.StringVar(value='')
        ttk.Label(top, textvariable=self.archive_status_var).pack(side='left', padx=8)
        self.archive_progress = ttk.Progressbar(top, mode='indeterminate', length=140)
        self.archive_progress.pack(side='left', padx=5)
        self.archive_progress.pack_forget()

        self.tree = ttk.Treeview(tab, columns=('size', 'time'), show='tree headings', selectmode='browse')
        self.tree.heading('#0', text='Archivname')
        self.tree.heading('size', text='Größe')
        self.tree.heading('time', text='Datum')
        self.tree.column('#0', width=300)
        self.tree.column('size', width=150)
        self.tree.column('time', width=200)
        self.tree.grid(row=2, column=0, sticky='nsew', padx=10, pady=5)
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)

        scrollbar = ttk.Scrollbar(tab, orient=tk.VERTICAL, command=self.tree.yview)
        scrollbar.grid(row=2, column=1, sticky='ns')
        self.tree.configure(yscrollcommand=scrollbar.set)

        btm = ttk.Frame(tab)
        btm.grid(row=3, column=0, sticky='ew', padx=10, pady=5)

    def refresh_profile_list(self):
        profiles = self.app.config.get_profiles()
        names = [p.name for p in profiles]
        self.profile_combo['values'] = names
        active = self.app.config.get_active_profile()
        if active and active.name in names:
            self.profile_combo.set(active.name)
        elif names:
            self.profile_combo.set(names[0])

    def _on_profile_change(self, event=None):
        name = self.profile_combo.get()
        if name:
            self.app.config.set_active_profile(name)
            self.app.on_profile_changed()

    def show_tab(self):
        """Wird aufgerufen, wenn der Tab sichtbar wird."""
        self.refresh_profile_list()
        profile = self.app.config.get_active_profile()
        if profile and self._is_cache_valid(profile.name):
            self._display_from_cache(profile.name)
        else:
            self._load_archive_list()

    def _is_cache_valid(self, profile_name):
        if profile_name not in self.cached_archives:
            return False
        age = (datetime.datetime.now() - self.cache_timestamp.get(profile_name, datetime.datetime.min)).total_seconds()
        return age < self.cache_valid_seconds

    def invalidate_cache(self, profile_name):
        self.cache_timestamp.pop(profile_name, None)
        self.cached_archives.pop(profile_name, None)

    def request_background_refresh(self):
        """Leiser Refresh im Hintergrund nach Backup."""
        profile = self.app.config.get_active_profile()
        if profile:
            self.invalidate_cache(profile.name)
            self._load_archive_list(silent=True)

    def _display_from_cache(self, profile_name):
        archives = self.cached_archives.get(profile_name, [])
        self.tree.delete(*self.tree.get_children())
        for archive in archives:
            name = archive.get('name', '?')
            time_str = archive.get('time', '?')
            size = archive.get('size_hr', '')
            self.tree.insert('', tk.END, text=name, values=(size, time_str))
        self._set_loading(False, f'Archivliste geladen: {len(archives)} Archive (Cache)')

    def _load_archive_list(self, silent=False):
        self.app.save_config()
        profile = self.app.config.get_active_profile()
        if not profile:
            return

        # Wenn Backup läuft, für später merken
        if self.app.backup_running:
            self.app.archive_list_refresh_pending = True
            if not silent:
                self._set_loading(True, 'Archivliste wird nach dem Backup geladen...')
            return

        self.archive_loading_token += 1
        self._set_loading(True, 'Archivliste wird geladen...')
        self.tree.delete(*self.tree.get_children())

        backend = get_backend(profile)
        env = backend.get_env(profile)
        repo = backend.get_repo_url(profile)
        cmd = [BORG_BIN, 'list', '--lock-wait=30', repo, '--json']
        cmd, env = get_command_with_privilege(cmd, env, needs_root=False, is_root=os.geteuid() == 0)

        self.app.archive_runner.stop_flag = False
        self.app.archive_runner.run_capture(cmd, env=env, done_callback=lambda r: self._on_list_loaded(r, profile.name))

    def _on_list_loaded(self, result, profile_name):
        ex = result.get('exception', '')
        if ex:
            self._set_loading(False)
            messagebox.showerror('Fehler', str(ex))
            return
        if result['exit_code'] != 0:
            self._set_loading(False)
            err = result['stderr'].strip() or result['stdout'].strip() or 'Unbekannter Fehler'
            messagebox.showerror('Fehler', f'borg list fehlgeschlagen:\n{err}')
            return

        try:
            data = json.loads(result['stdout'])
            archives = data.get('archives', [])
            if not archives:
                self._set_loading(False, 'Keine Archive gefunden.')
                self.cached_archives[profile_name] = []
                self.cache_timestamp[profile_name] = datetime.datetime.now()
                return

            token = self.archive_loading_token
            rows = []
            for archive in archives:
                name = archive.get('name') or archive.get('archive') or '?'
                time_str = archive.get('time', '?')
                size = archive.get('stats', {}).get('original_size', None)
                size_hr = self._human_size(size) if size else 'wird geladen...'
                item_id = self.tree.insert('', tk.END, text=name, values=(size_hr, time_str))
                rows.append((item_id, name, size_hr))

            # Im Cache speichern (Größen später aktualisieren)
            cached = [{'name': r[1], 'time': self.tree.item(r[0], 'values')[1] if self.tree.exists(r[0]) else '', 'size_hr': r[2]} for r in rows]
            self.cached_archives[profile_name] = cached
            self.cache_timestamp[profile_name] = datetime.datetime.now()

            self._set_loading(True, f'Archivgrößen werden geladen (0/{len(rows)})...')
            threading.Thread(target=self._populate_sizes_async, args=(rows, token, profile_name), daemon=True).start()
        except Exception as exc:
            self._set_loading(False)
            messagebox.showerror('Fehler', str(exc))

    def _populate_sizes_async(self, rows, token, profile_name):
        total = len(rows)
        for index, (item_id, archive_name, _) in enumerate(rows, start=1):
            if token != self.archive_loading_token:
                return
            size_label = self._query_size(archive_name)
            self.app.master.after(0, lambda iid=item_id, size=size_label, idx=index, cnt=total, tok=token, pn=profile_name:
                                  self._update_row(iid, size, idx, cnt, tok, pn))

    def _update_row(self, item_id, size_label, index, total, token, profile_name):
        if token != self.archive_loading_token or not self.tree.exists(item_id):
            return
        values = list(self.tree.item(item_id, 'values'))
        if len(values) >= 2:
            values[0] = size_label
            self.tree.item(item_id, values=tuple(values))

        # Cache aktualisieren
        profile_name = profile_name or (self.app.config.get_active_profile().name if self.app.config.get_active_profile() else '')
        if profile_name and profile_name in self.cached_archives:
            for c in self.cached_archives[profile_name]:
                if c.get('name') == self.tree.item(item_id, 'text'):
                    c['size_hr'] = size_label
                    break

        if index >= total:
            self._set_loading(False, f'Archivliste geladen: {total} Archive')
        else:
            self._set_loading(True, f'Archivgrößen werden geladen ({index}/{total})...')

    def _query_size(self, archive_name):
        profile = self.app.config.get_active_profile()
        if not profile:
            return 'n/a'
        backend = get_backend(profile)
        env = backend.get_env(profile)
        repo = backend.get_repo_url(profile)
        cmd = [BORG_BIN, 'info', '--lock-wait=30', f'{repo}::{archive_name}']
        cmd, env = get_command_with_privilege(cmd, env, needs_root=False, is_root=os.geteuid() == 0)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        except Exception:
            return 'n/a'
        if result.returncode != 0:
            return 'n/a'
        match = re.search(r'This archive:\s+([0-9.,]+\s+[KMGTPE]?i?B)', result.stdout)
        return match.group(1) if match else 'n/a'

    def _human_size(self, size_bytes):
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size_bytes < 1024:
                return f'{size_bytes:.1f} {unit}'
            size_bytes /= 1024
        return f'{size_bytes:.1f} PB'

    def _toggle_mount(self):
        if self.mount_active:
            self._unmount_archive()
        else:
            self._mount_archive()

    def _mount_archive(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showerror('Fehler', 'Kein Archiv ausgewählt.')
            return

        name = self.tree.item(selected[0], 'text')
        profile = self.app.config.get_active_profile()
        if not profile:
            return

        backend = get_backend(profile)
        env = backend.get_env(profile)
        repo = backend.get_repo_url(profile)
        os.makedirs(str(self.mount_point), exist_ok=True)

        cmd = [BORG_BIN, 'mount', '--lock-wait=30', f'{repo}::{name}', str(self.mount_point)]
        cmd, env = get_command_with_privilege(cmd, env, needs_root=False, is_root=os.geteuid() == 0)

        self.app.archive_runner.stop_flag = False
        self.app.archive_runner.run_capture(cmd, env=env, done_callback=self._on_mount_done)
        self.app.append_backup_log(f'Mounte Archiv {name}...\n')
        self.mount_btn.config(text='Mounten...', state='disabled')

    def _on_mount_done(self, result):
        ex = result.get('exception', '')
        if ex:
            messagebox.showerror('Fehler', str(ex))
            self.mount_btn.config(text='Ausgewähltes Archiv mounten', state='normal')
            return
        if result['exit_code'] != 0:
            err = result['stderr'].strip() or result['stdout'].strip() or 'Unbekannter Fehler'
            messagebox.showerror('Fehler', f'Mount fehlgeschlagen:\n{err}')
            self.mount_btn.config(text='Ausgewähltes Archiv mounten', state='normal')
            return

        self.mount_active = True
        self.mount_btn.config(text='Unmounten')
        self.open_btn.config(state='normal')
        messagebox.showinfo('Info', f'Archiv gemountet auf {self.mount_point}')

    def _unmount_archive(self):
        if not self.mount_active:
            return

        cmd = ['borg', 'umount', str(self.mount_point)]
        profile = self.app.config.get_active_profile()
        if profile:
            backend = get_backend(profile)
            env = backend.get_env(profile)
        else:
            env = os.environ.copy()

        try:
            subprocess.run(cmd, capture_output=True, text=True, env=env)
        except Exception:
            # Fallback: force unmount
            subprocess.run(['umount', '-l', str(self.mount_point)], capture_output=True)

        self.mount_active = False
        self.mount_btn.config(text='Ausgewähltes Archiv mounten')
        self.open_btn.config(state='disabled')

    def _open_in_filemanager(self):
        if not self.mount_active:
            return
        try:
            subprocess.Popen(['xdg-open', str(self.mount_point)])
        except Exception:
            messagebox.showerror('Fehler', 'Konnte Dateimanager nicht öffnen.')

    def _delete_archive(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showerror('Fehler', 'Kein Archiv ausgewählt.')
            return

        name = self.tree.item(selected[0], 'text')
        if not messagebox.askyesno('Löschen', f"Archiv '{name}' wirklich löschen?"):
            return

        profile = self.app.config.get_active_profile()
        if not profile:
            return

        backend = get_backend(profile)
        env = backend.get_env(profile)
        repo = backend.get_repo_url(profile)
        cmd = [BORG_BIN, 'delete', '--lock-wait=30', f'{repo}::{name}']
        cmd, env = get_command_with_privilege(cmd, env, needs_root=False, is_root=os.geteuid() == 0)

        self.app.archive_runner.stop_flag = False
        self.app.archive_runner.run_capture(cmd, env=env, done_callback=self._on_delete_done)

    def _on_delete_done(self, result):
        ex = result.get('exception', '')
        if ex:
            messagebox.showerror('Fehler', str(ex))
            return
        if result['exit_code'] != 0:
            err = result['stderr'].strip() or result['stdout'].strip() or 'Unbekannter Fehler'
            messagebox.showerror('Fehler', f'Löschen fehlgeschlagen:\n{err}')
            return

        # Cache invalidieren
        profile = self.app.config.get_active_profile()
        if profile:
            self.invalidate_cache(profile.name)
        messagebox.showinfo('Info', 'Archiv gelöscht.')
        self._load_archive_list()

    def _set_loading(self, is_loading, text=''):
        self.archive_status_var.set(text)
        if is_loading:
            if not self.archive_progress.winfo_manager():
                self.archive_progress.pack(side='left', padx=5)
            self.archive_progress.start(10)
        else:
            self.archive_progress.stop()
            if self.archive_progress.winfo_manager():
                self.archive_progress.pack_forget()

    def cleanup_mount(self):
        """Aufräumen beim App-Ende."""
        if self.mount_active:
            self._unmount_archive()
