"""Restore-Tab: Wiederherstellung von Archiven."""

import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from borg_backup_gui.backends import get_backend, get_command_with_privilege
from borg_backup_gui.runner import BORG_BIN


class RestoreTab:
    """Tab für die Wiederherstellung (Restore) eines Archivs."""

    def __init__(self, notebook, app):
        self.app = app
        self.frame = ttk.Frame(notebook)
        notebook.add(self.frame, text='Restore')
        self._build_ui()

    def _build_ui(self):
        tab = self.frame

        ttk.Label(tab, text='Wiederherstellung (Restore)', font=('Arial', 14, 'bold')).pack(pady=10)

        # Profil-Auswahl
        profile_frame = ttk.Frame(tab)
        profile_frame.pack(pady=5)
        ttk.Label(profile_frame, text='Profil:').pack(side='left', padx=5)
        self.profile_combo = ttk.Combobox(profile_frame, state='readonly', width=35)
        self.profile_combo.pack(side='left', padx=5)
        self.profile_combo.bind('<<ComboboxSelected>>', self._on_profile_change)

        frame = ttk.Frame(tab)
        frame.pack(pady=10)

        ttk.Label(frame, text='Archivname:').grid(row=0, column=0, sticky='e', padx=5, pady=5)
        self.restore_archive_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.restore_archive_var, width=40).grid(row=0, column=1, padx=5)

        ttk.Label(frame, text='Zielverzeichnis:').grid(row=1, column=0, sticky='e', padx=5, pady=5)
        self.restore_path_var = tk.StringVar(value='/mnt/restore')
        ttk.Entry(frame, textvariable=self.restore_path_var, width=40).grid(row=1, column=1, padx=5)
        ttk.Button(frame, text='Ziel wählen', command=self._select_target).grid(row=1, column=2, padx=5)

        self.restore_log = tk.Text(tab, state='disabled', bg='#1e1e1e', fg='#00ff00', height=8)
        self.restore_log.pack(fill='both', expand=True, padx=10, pady=10)

        ttk.Button(tab, text='Restore starten', command=self._start_restore).pack(pady=5)

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

    def _select_target(self):
        path = filedialog.askdirectory(title='Zielverzeichnis für Restore')
        if path:
            self.restore_path_var.set(path)

    def _start_restore(self):
        self.app.save_config()
        archive = self.restore_archive_var.get().strip()
        target = self.restore_path_var.get().strip()

        if not archive or not target:
            messagebox.showerror('Fehler', 'Archivname und Zielverzeichnis angeben.')
            return
        if not os.path.isdir(target):
            messagebox.showerror('Fehler', 'Zielverzeichnis existiert nicht.')
            return

        profile = self.app.config.get_active_profile()
        if not profile:
            messagebox.showerror('Fehler', 'Kein Profil ausgewählt.')
            return

        backend = get_backend(profile)
        env = backend.get_env(profile)
        repo = backend.get_repo_url(profile)
        cmd = [BORG_BIN, 'extract', '--list', '--lock-wait=30', f'{repo}::{archive}']
        cmd, env = get_command_with_privilege(cmd, env, needs_root=True, is_root=os.geteuid() == 0)

        self._append_log(f'Starte Restore von {archive} nach {target}...\n')
        self.app.restore_runner.stop_flag = False
        self.app.restore_runner.run(cmd, env=env, cwd=target, done_callback=self._on_restore_done)

    def _on_restore_done(self, result):
        exit_code = result.get('exit_code', -1)
        if exit_code == 0:
            self._append_log('\n[OK] Restore erfolgreich!\n')
            messagebox.showinfo('Erfolg', 'Restore abgeschlossen.')
        else:
            err = result.get('exception', '') or result.get('stderr', '') or f'Exit-Code: {exit_code}'
            self._append_log(f'\n[FEHLER] {err}\n')
            messagebox.showerror('Fehler', f'Restore fehlgeschlagen:\n{err}')

    def _append_log(self, text):
        if self.restore_log:
            self.restore_log.configure(state='normal')
            self.restore_log.insert(tk.END, text)
            self.restore_log.see(tk.END)
            self.restore_log.configure(state='disabled')
