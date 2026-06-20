"""Backup-Tab: Server-Konfiguration, Include/Exclude, Backup starten."""

import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from borg_backup_gui.backends import get_backend


class BackupTab:
    """Tab für Backup-Konfiguration und -Ausführung."""

    def __init__(self, notebook, app):
        self.app = app
        self.frame = ttk.Frame(notebook)
        notebook.add(self.frame, text='Backup')
        self._build_ui()

    def _build_ui(self):
        tab = self.frame
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        top_frame = ttk.Frame(tab)
        top_frame.grid(row=0, column=0, sticky='nsew', padx=10, pady=10)
        top_frame.columnconfigure(0, weight=1)
        top_frame.columnconfigure(1, weight=1)
        top_frame.rowconfigure(1, weight=1)

        # Profil-Auswahl oben
        profile_frame = ttk.Frame(top_frame)
        profile_frame.grid(row=0, column=0, columnspan=2, sticky='ew', pady=(0, 10))
        profile_frame.columnconfigure(1, weight=1)
        ttk.Label(profile_frame, text='Profil:', font=('Arial', 10, 'bold')).grid(row=0, column=0, sticky='w')
        self.profile_combo = ttk.Combobox(profile_frame, state='readonly', width=40)
        self.profile_combo.grid(row=0, column=1, sticky='w', padx=8)
        self.profile_combo.bind('<<ComboboxSelected>>', self._on_profile_change)
        ttk.Button(profile_frame, text='Neu', command=self._on_new_profile).grid(row=0, column=2, padx=4)

        left = ttk.Frame(top_frame)
        left.grid(row=1, column=0, sticky='nsew', padx=(0, 10))
        left.columnconfigure(0, weight=1)

        server_frame = ttk.LabelFrame(left, text='Backend-Konfiguration')
        server_frame.grid(row=0, column=0, sticky='ew', pady=5)
        server_frame.columnconfigure(1, weight=1)

        # Backend-Typ anzeigen
        self.backend_type_label = ttk.Label(server_frame, text='Typ: SSH / Storage Box')
        self.backend_type_label.grid(row=0, column=0, columnspan=2, sticky='w', padx=5, pady=2)

        ttk.Label(server_frame, text='Repository:').grid(row=1, column=0, sticky='e', padx=5, pady=2)
        self.storage_var = tk.StringVar()
        self.storage_entry = ttk.Entry(server_frame, textvariable=self.storage_var, width=50)
        self.storage_entry.grid(row=1, column=1, sticky='ew', padx=5, pady=2)

        ttk.Label(server_frame, text='SSH Key (Pfad):').grid(row=2, column=0, sticky='e', padx=5, pady=2)
        self.ssh_key_var = tk.StringVar()
        ssh_entry = ttk.Combobox(server_frame, textvariable=self.ssh_key_var, width=47)
        ssh_entry.grid(row=2, column=1, sticky='ew', padx=5, pady=2)
        ttk.Button(server_frame, text='Auswählen', command=self._select_ssh_key).grid(row=2, column=2, padx=5, pady=2)

        ttk.Label(server_frame, text='Passphrase:').grid(row=3, column=0, sticky='e', padx=5, pady=2)
        self.passphrase_var = tk.StringVar()
        pass_entry = ttk.Entry(server_frame, textvariable=self.passphrase_var, width=50, show='*')
        pass_entry.grid(row=3, column=1, sticky='ew', padx=5, pady=2)
        ttk.Label(server_frame, text='(leer = interaktiv abfragen)', font=('Arial', 8)).grid(row=4, column=1, sticky='w', padx=5)

        ttk.Label(server_frame, text='Kompression:').grid(row=5, column=0, sticky='e', padx=5, pady=2)
        self.compression_var = tk.StringVar(value='lz4')
        compression_combo = ttk.Combobox(server_frame, textvariable=self.compression_var,
                                          values=['none', 'lz4', 'zstd,3', 'zstd,6', 'zstd,9'],
                                          width=15, state='readonly')
        compression_combo.grid(row=5, column=1, sticky='w', padx=5, pady=2)

        # S3-Felder (zunächst versteckt)
        self.s3_frame = ttk.LabelFrame(left, text='S3 Object Storage Einstellungen')
        self.s3_frame.grid(row=1, column=0, sticky='ew', pady=5)
        self.s3_frame.columnconfigure(1, weight=1)
        ttk.Label(self.s3_frame, text='Access Key:').grid(row=0, column=0, sticky='e', padx=5, pady=2)
        self.s3_access_var = tk.StringVar()
        ttk.Entry(self.s3_frame, textvariable=self.s3_access_var, width=50).grid(row=0, column=1, sticky='ew', padx=5, pady=2)
        ttk.Label(self.s3_frame, text='Secret Key:').grid(row=1, column=0, sticky='e', padx=5, pady=2)
        self.s3_secret_var = tk.StringVar()
        ttk.Entry(self.s3_frame, textvariable=self.s3_secret_var, width=50, show='*').grid(row=1, column=1, sticky='ew', padx=5, pady=2)
        ttk.Label(self.s3_frame, text='Endpoint URL:').grid(row=2, column=0, sticky='e', padx=5, pady=2)
        self.s3_endpoint_var = tk.StringVar(value='https://fsn1.your-objectstorage.com')
        ttk.Entry(self.s3_frame, textvariable=self.s3_endpoint_var, width=50).grid(row=2, column=1, sticky='ew', padx=5, pady=2)
        ttk.Label(self.s3_frame, text='Region:').grid(row=3, column=0, sticky='e', padx=5, pady=2)
        self.s3_region_var = tk.StringVar(value='fsn1')
        ttk.Entry(self.s3_frame, textvariable=self.s3_region_var, width=20).grid(row=3, column=1, sticky='w', padx=5, pady=2)

        # Lokale Felder (zunächst versteckt)
        self.local_frame = ttk.LabelFrame(left, text='Lokale Einstellungen')
        self.local_frame.grid(row=2, column=0, sticky='ew', pady=5)
        self.local_frame.columnconfigure(1, weight=1)
        ttk.Label(self.local_frame, text='Repository Pfad:').grid(row=0, column=0, sticky='e', padx=5, pady=2)
        self.local_path_var = tk.StringVar(value='/mnt/backup/borg-repo')
        ttk.Entry(self.local_frame, textvariable=self.local_path_var, width=50).grid(row=0, column=1, sticky='ew', padx=5, pady=2)
        ttk.Button(self.local_frame, text='Wählen', command=self._select_local_path).grid(row=0, column=2, padx=5, pady=2)

        # Felder verstecken/zeigen nach Typ
        self._hide_all_backend_frames()

        right = ttk.Frame(top_frame)
        right.grid(row=1, column=1, sticky='nsew', padx=(10, 0))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        inc_frame = ttk.LabelFrame(right, text='Include (zu sichernde Ordner)')
        inc_frame.grid(row=0, column=0, sticky='nsew', pady=(0, 5))
        inc_frame.columnconfigure(0, weight=1)
        self.include_text = tk.Text(inc_frame, height=3, bg='#4a4a4a', fg='#ffffff', insertbackground='white')
        self.include_text.grid(row=0, column=0, sticky='nsew', padx=5, pady=5)

        exc_frame = ttk.LabelFrame(right, text='Exclude (ausgeschlossene Ordner)')
        exc_frame.grid(row=1, column=0, sticky='nsew', pady=5)
        exc_frame.columnconfigure(0, weight=1)
        exc_frame.rowconfigure(0, weight=1)
        self.exclude_text = tk.Text(exc_frame, height=8, bg='#4a4a4a', fg='#ffffff', insertbackground='white')
        self.exclude_text.grid(row=0, column=0, sticky='nsew', padx=5, pady=5)

        button_frame = ttk.Frame(top_frame)
        button_frame.grid(row=2, column=0, columnspan=2, sticky='ew', pady=(0, 10))
        self.run_backup_btn = ttk.Button(button_frame, text='Backup jetzt ausführen', command=self._on_start_backup)
        self.run_backup_btn.pack(side='left', padx=5)
        self.stop_btn = ttk.Button(button_frame, text='Abbrechen', command=self._on_stop_backup)
        self.stop_btn.pack(side='left', padx=5)
        self.stop_btn.config(state='disabled')

        log_frame = ttk.LabelFrame(tab, text='Live-Log')
        log_frame.grid(row=1, column=0, sticky='nsew', padx=10, pady=(0, 10))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_widget = tk.Text(log_frame, state='disabled', bg='#1e1e1e', fg='#00ff00',
                                   font=('Courier', 9), wrap=tk.WORD)
        self.log_widget.grid(row=0, column=0, sticky='nsew', padx=2, pady=2)
        log_scroll = ttk.Scrollbar(log_frame, command=self.log_widget.yview)
        log_scroll.grid(row=0, column=1, sticky='ns', padx=2, pady=2)
        self.log_widget.configure(yscrollcommand=log_scroll.set)

    def _hide_all_backend_frames(self):
        self.s3_frame.grid_remove()
        self.local_frame.grid_remove()

    def _show_backend_fields(self, backend_type):
        self._hide_all_backend_frames()
        if backend_type == 's3':
            self.s3_frame.grid()
        elif backend_type == 'local':
            self.local_frame.grid()

    def _on_profile_change(self, event=None):
        name = self.profile_combo.get()
        if name:
            self.app.config.set_active_profile(name)
            self.app.on_profile_changed()
            self.load_from_profile()

    def _on_new_profile(self):
        self.app.show_profile_dialog()

    def _on_start_backup(self):
        self._save_to_profile()
        self.app.start_backup()

    def _on_stop_backup(self):
        self.app.stop_backup()

    def _select_ssh_key(self):
        filename = filedialog.askopenfilename(title='SSH Key auswählen',
                                               initialdir=os.path.expanduser('~/.ssh'))
        if filename:
            self.ssh_key_var.set(filename)

    def _select_local_path(self):
        path = filedialog.askdirectory(title='Repository-Pfad wählen')
        if path:
            self.local_path_var.set(path)

    def refresh_profile_list(self):
        profiles = self.app.config.get_profiles()
        names = [p.name for p in profiles]
        self.profile_combo['values'] = names
        active = self.app.config.get_active_profile()
        if active and active.name in names:
            self.profile_combo.set(active.name)
        elif names:
            self.profile_combo.set(names[0])

    def load_from_profile(self):
        """Lädt aktives Profil in die UI-Felder."""
        profile = self.app.config.get_active_profile()
        if not profile:
            return

        self.storage_var.set(profile.storage)
        self.ssh_key_var.set(profile.ssh_key)
        self.passphrase_var.set(profile.passphrase)
        self.compression_var.set(profile.compression or 'lz4')
        self.s3_access_var.set(profile.s3_access_key)
        self.s3_secret_var.set(profile.s3_secret_key)
        self.s3_endpoint_var.set(profile.s3_endpoint_url)
        self.s3_region_var.set(profile.s3_region)
        self.local_path_var.set(profile.local_path)

        from borg_backup_gui.backends import get_backend
        backend = get_backend(profile)
        self.backend_type_label.config(text=f'Typ: {backend.get_label()}')

        self._show_backend_fields(profile.type)

        self.include_text.delete('1.0', tk.END)
        inc_content = '\n'.join(profile.include_folders)
        self.include_text.insert(tk.END, inc_content)

        self.exclude_text.delete('1.0', tk.END)
        exc_content = '\n'.join(profile.exclude_folders)
        self.exclude_text.insert(tk.END, exc_content)

    def _save_to_profile(self):
        """Speichert UI-Felder zurück ins aktive Profil."""
        profile = self.app.config.get_active_profile()
        if not profile:
            return

        profile.storage = self.storage_var.get().strip()
        profile.ssh_key = self.ssh_key_var.get().strip()
        profile.passphrase = self.passphrase_var.get()
        profile.compression = self.compression_var.get().strip() or 'lz4'
        profile.s3_access_key = self.s3_access_var.get().strip()
        profile.s3_secret_key = self.s3_secret_var.get().strip()
        profile.s3_endpoint_url = self.s3_endpoint_var.get().strip()
        profile.s3_region = self.s3_region_var.get().strip()
        profile.local_path = self.local_path_var.get().strip()

        includes = [line.strip() for line in self.include_text.get('1.0', tk.END).splitlines() if line.strip()]
        profile.include_folders = includes or ['/']
        excludes = [line.strip() for line in self.exclude_text.get('1.0', tk.END).splitlines() if line.strip()]
        profile.exclude_folders = excludes

        self.app.config.update_profile(profile)

    def append_log(self, text):
        """Fügt Text ins Live-Log ein (wird vom Queue-Processor aufgerufen)."""
        if self.log_widget:
            self.log_widget.configure(state='normal')
            self.log_widget.insert(tk.END, text)
            self.log_widget.see(tk.END)
            self.log_widget.configure(state='disabled')

    def set_backup_buttons(self, running: bool):
        if running:
            self.run_backup_btn.config(state='disabled')
            self.stop_btn.config(state='normal')
        else:
            self.run_backup_btn.config(state='normal')
            self.stop_btn.config(state='disabled')
