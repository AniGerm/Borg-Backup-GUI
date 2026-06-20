"""Dashboard/Start-Tab: Statusanzeige und Schnellaktionen."""

import datetime
import os
import tkinter as tk
from tkinter import ttk


class DashboardTab:
    """Übersichts-Tab mit Status, Ampel, letztem Backup und Tray-Einstellungen."""

    def __init__(self, notebook, app):
        self.app = app
        self.frame = ttk.Frame(notebook)
        notebook.add(self.frame, text='Start')
        self._build_ui()

    def _build_ui(self):
        tab = self.frame
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(3, weight=1)

        row = 0

        # Profil-Auswahl
        profile_frame = ttk.Frame(tab)
        profile_frame.grid(row=row, column=0, sticky='ew', padx=12, pady=(12, 0))
        profile_frame.columnconfigure(1, weight=1)
        ttk.Label(profile_frame, text='Aktives Profil:', font=('Arial', 12, 'bold')).grid(row=0, column=0, sticky='w')
        self.profile_combo = ttk.Combobox(profile_frame, state='readonly', width=40)
        self.profile_combo.grid(row=0, column=1, sticky='w', padx=8)
        self.profile_combo.bind('<<ComboboxSelected>>', self._on_profile_change)
        row += 1

        # Hinweis-Box für neue Features
        self.feature_hint_var = tk.StringVar(
            value='💡 Neu: Mehrere Backup-Profile mit SSH, S3 Object Storage oder lokalen Laufwerken! '
                  'Klicke auf "➕ Neues Profil" um ein S3- oder lokales Backup einzurichten.'
        )
        hint_label = ttk.Label(tab, textvariable=self.feature_hint_var, font=('Arial', 9), wraplength=900,
                                foreground='#0f766e')
        hint_label.grid(row=row, column=0, sticky='ew', padx=12, pady=(4, 0))
        row += 1

        ttk.Label(tab, text='Backup Übersicht', font=('Arial', 16, 'bold')).grid(
            row=row, column=0, sticky='w', padx=12, pady=(8, 4)
        )
        row += 1

        status_frame = ttk.LabelFrame(tab, text='Aktueller Status')
        status_frame.grid(row=row, column=0, sticky='nsew', padx=12, pady=8)
        status_frame.columnconfigure(1, weight=1)

        self.status_canvas = tk.Canvas(status_frame, width=52, height=52, bg='#2e2e2e', highlightthickness=0)
        self.status_canvas.grid(row=0, column=0, rowspan=5, padx=8, pady=8, sticky='n')
        self.status_light = self.status_canvas.create_oval(6, 6, 46, 46, fill='#6e6e6e', outline='')

        self.status_title_var = tk.StringVar(value='Noch kein Backup ausgeführt')
        ttk.Label(status_frame, textvariable=self.status_title_var, font=('Arial', 12, 'bold')).grid(
            row=0, column=1, sticky='w', padx=6, pady=(8, 2)
        )

        self.backend_type_var = tk.StringVar(value='')
        ttk.Label(status_frame, textvariable=self.backend_type_var, font=('Arial', 9)).grid(
            row=1, column=1, sticky='w', padx=6, pady=2
        )

        self.last_backup_var = tk.StringVar(value='Letzter Lauf: noch nie')
        ttk.Label(status_frame, textvariable=self.last_backup_var).grid(row=2, column=1, sticky='w', padx=6, pady=2)

        self.next_backup_var = tk.StringVar(value='Nächstes geplantes Backup: deaktiviert')
        ttk.Label(status_frame, textvariable=self.next_backup_var).grid(row=3, column=1, sticky='w', padx=6, pady=2)

        self.last_error_var = tk.StringVar(value='Letzte Meldung: noch keine')
        ttk.Label(status_frame, textvariable=self.last_error_var, wraplength=760).grid(
            row=4, column=1, sticky='w', padx=6, pady=(2, 8)
        )
        row += 1

        action_frame = ttk.LabelFrame(tab, text='Schnellaktionen')
        action_frame.grid(row=row, column=0, sticky='ew', padx=12, pady=8)
        self.backup_btn = ttk.Button(action_frame, text='Backup jetzt starten', command=self._on_start_backup)
        self.backup_btn.pack(side='left', padx=8, pady=8)
        ttk.Button(action_frame, text='Status aktualisieren', command=self._on_refresh).pack(side='left', padx=8, pady=8)

        profile_actions = ttk.Frame(action_frame)
        profile_actions.pack(side='left', padx=8, pady=8)
        ttk.Button(profile_actions, text='➕ Neues Profil (SSH/S3/Lokal)', command=self._on_new_profile).pack(side='left', padx=2)
        ttk.Button(profile_actions, text='Profil bearbeiten', command=self._on_edit_profile).pack(side='left', padx=2)
        row += 1

        stats_frame = ttk.LabelFrame(tab, text='Backup-Statistik')
        stats_frame.grid(row=row, column=0, sticky='ew', padx=12, pady=8)
        stats_frame.columnconfigure(1, weight=1)

        self.backup_files_var = tk.StringVar(value='Gefundene/verarbeitete Dateien: 0')
        self.backup_source_size_var = tk.StringVar(value='Quellgröße: noch keine Daten')
        self.backup_dedupe_size_var = tk.StringVar(value='Dedupliziert: noch keine Daten')
        self.backup_last_item_var = tk.StringVar(value='Zuletzt: noch keine Daten')

        ttk.Label(stats_frame, textvariable=self.backup_files_var).grid(row=0, column=0, sticky='w', padx=8, pady=(8, 2))
        ttk.Label(stats_frame, textvariable=self.backup_source_size_var).grid(row=0, column=1, sticky='w', padx=8, pady=(8, 2))
        ttk.Label(stats_frame, textvariable=self.backup_dedupe_size_var).grid(row=1, column=0, sticky='w', padx=8, pady=(2, 8))
        ttk.Label(stats_frame, textvariable=self.backup_last_item_var, wraplength=720).grid(row=1, column=1, sticky='w', padx=8, pady=(2, 8))
        row += 1

        tray_frame = ttk.LabelFrame(tab, text='Hintergrundsymbol in der Statusleiste')
        tray_frame.grid(row=row, column=0, sticky='ew', padx=12, pady=(0, 12))
        tray_frame.columnconfigure(3, weight=1)

        self.tray_enabled_var = tk.BooleanVar(value=self.app.config.global_settings.get('tray_enabled', True))
        tray_check = ttk.Checkbutton(tray_frame, text='Tray-Symbol aktivieren (Status im Benachrichtigungsfeld)',
                                      variable=self.tray_enabled_var, command=self._on_tray_change)
        tray_check.grid(row=0, column=0, padx=8, pady=8, sticky='w')

        ttk.Label(tray_frame, text='Icon-Stil:').grid(row=0, column=1, padx=(8, 4), pady=8, sticky='e')
        self.tray_icon_style_var = tk.StringVar(value=self.app.config.global_settings.get('tray_icon_style', 'disk'))
        tray_style_combo = ttk.Combobox(tray_frame, textvariable=self.tray_icon_style_var,
                                         values=['disk', 'shield', 'palm'], state='readonly', width=10)
        tray_style_combo.grid(row=0, column=2, padx=4, pady=8, sticky='w')
        ttk.Button(tray_frame, text='Übernehmen', command=self._on_tray_change).grid(row=0, column=3, padx=8, pady=8, sticky='w')

        ttk.Label(tray_frame, text='Hinweis: Schließen des Fensters beendet die App (kein Minimieren in den Tray mehr).',
                  font=('Arial', 9)).grid(row=1, column=0, columnspan=4, sticky='w', padx=8, pady=(0, 8))

        tray_supported = bool(self.app.tray and self.app.tray.tray_supported)
        if not tray_supported:
            tray_check.state(['disabled'])
            tray_style_combo.state(['disabled'])
            ttk.Label(tray_frame, text='Tray deaktiviert: pystray/pillow fehlen.', font=('Arial', 9)).grid(
                row=2, column=0, columnspan=4, sticky='w', padx=8, pady=(0, 8))
        elif os.geteuid() == 0:
            tray_check.state(['disabled'])
            tray_style_combo.state(['disabled'])
            ttk.Label(tray_frame, text='Tray im root-Modus deaktiviert.', font=('Arial', 9)).grid(
                row=2, column=0, columnspan=4, sticky='w', padx=8, pady=(0, 8))

    def _on_profile_change(self, event=None):
        name = self.profile_combo.get()
        if name:
            self.app.config.set_active_profile(name)
            self.app.on_profile_changed()
            self.refresh()

    def _on_start_backup(self):
        self.app.start_backup()

    def _on_refresh(self):
        self.refresh()
        self.app.refresh_stats_from_latest_archive()

    def _on_new_profile(self):
        self.app.show_profile_dialog()

    def _on_edit_profile(self):
        profile = self.app.config.get_active_profile()
        if profile:
            self.app.show_profile_dialog(edit_profile=profile)

    def _on_tray_change(self):
        self.app.config.global_settings['tray_enabled'] = bool(self.tray_enabled_var.get())
        self.app.config.global_settings['tray_icon_style'] = self.tray_icon_style_var.get().strip() or 'disk'
        self.app.config.save()
        if self.app.tray:
            self.app.tray.setup(force_restart=True)

    def refresh_profile_list(self):
        profiles = self.app.config.get_profiles()
        names = [p.name for p in profiles]
        self.profile_combo['values'] = names
        active = self.app.config.get_active_profile()
        if active and active.name in names:
            self.profile_combo.set(active.name)
        elif names:
            self.profile_combo.set(names[0])

    def refresh(self):
        self.refresh_profile_list()

        profile = self.app.config.get_active_profile()
        if not profile:
            return

        status = self.app.config.get_status(profile.name)

        from borg_backup_gui.backends import get_backend
        backend = get_backend(profile)
        self.backend_type_var.set(f'Typ: {backend.get_label()} | Ziel: {self.app.config.get_storage_for_display(profile)}')

        color, title = self._compute_traffic_light(profile, status)
        self.status_canvas.itemconfig(self.status_light, fill=color)
        self.status_title_var.set(title)

        if self.app.backup_running and self.app.current_backup_started_at:
            runtime = int((datetime.datetime.now() - self.app.current_backup_started_at).total_seconds())
            self.last_backup_var.set(f'Letzter Lauf: läuft seit {runtime // 60} min {runtime % 60} s')
        else:
            fmt = self._format_time(status.last_run_at)
            self.last_backup_var.set(f'Letzter Lauf: {fmt if fmt != "-" else "noch nie"}')

        if self.app.next_backup_dt:
            self.next_backup_var.set(f'Nächstes geplantes Backup: {self.app.next_backup_dt:%d.%m.%Y %H:%M}')
        else:
            self.next_backup_var.set('Nächstes geplantes Backup: deaktiviert')

        if self.app.backup_running:
            files_count = status.last_files_count or 0
            source_size = status.last_source_size or ''
            dedupe_size = status.last_dedupe_size or ''
            parts = [f'{files_count} Dateien'] if files_count else []
            if source_size:
                parts.append(f'Quelle {source_size}')
            if dedupe_size:
                parts.append(f'Dedupe {dedupe_size}')
            detail = ' | '.join(parts) if parts else 'arbeite...'
            self.last_error_var.set(f'Letzte Meldung: Backup läuft... {detail}')
        elif status.last_error:
            self.last_error_var.set(f'Letzte Meldung: {status.last_error}')
        elif status.last_run_at:
            dur = status.last_duration_sec
            if isinstance(dur, (int, float)):
                self.last_error_var.set(f'Letzte Meldung: Erfolgreich ({int(dur)} s)')
            else:
                self.last_error_var.set('Letzte Meldung: Erfolgreich')
        else:
            self.last_error_var.set('Letzte Meldung: noch keine')

        files_label = str(status.last_files_count) if status.last_files_count else (
            'wird geladen...' if self._stats_missing(status) else 'noch keine Daten')
        source_label = status.last_source_size or (
            'wird geladen...' if self._stats_missing(status) else 'noch keine Daten')
        dedupe_label = status.last_dedupe_size or (
            'wird geladen...' if self._stats_missing(status) else 'noch keine Daten')
        item_label = status.last_item or (
            'wird geladen...' if self._stats_missing(status) else 'noch keine Daten')

        self.backup_files_var.set(f'Gefundene/verarbeitete Dateien: {files_label}')
        self.backup_source_size_var.set(f'Quellgröße: {source_label}')
        self.backup_dedupe_size_var.set(f'Dedupliziert: {dedupe_label}')
        self.backup_last_item_var.set(f'Zuletzt: {item_label}')

    def _compute_traffic_light(self, profile, status):
        now = datetime.datetime.now()
        color = '#6e6e6e'
        title = 'Noch kein Backup ausgeführt'

        if self.app.backup_running:
            color = '#2d7ff9'
            title = 'Backup läuft gerade'
        elif status.last_exit_code == 0:
            last_success = self._parse_iso(status.last_success_at)
            if last_success:
                age = (now - last_success).total_seconds() / 3600.0
                if age <= 24:
                    color = '#28a745'
                    title = 'Backup aktuell und erfolgreich'
                elif age <= 48:
                    color = '#e0a800'
                    title = 'Backup erfolgreich, aber nicht mehr tagesaktuell'
                else:
                    color = '#dc3545'
                    title = 'Letztes erfolgreiches Backup ist zu alt'
        elif status.last_exit_code == 1:
            last_success = self._parse_iso(status.last_success_at)
            if last_success:
                age = (now - last_success).total_seconds() / 3600.0
                color = '#e0a800'
                title = 'Backup mit Warnungen abgeschlossen' if age <= 48 else 'Letztes Backup mit Warnungen und zu alt'
        elif status.last_exit_code is not None:
            color = '#dc3545'
            title = 'Letztes Backup fehlgeschlagen'

        return color, title

    def _parse_iso(self, value):
        if not value:
            return None
        try:
            return datetime.datetime.fromisoformat(value)
        except ValueError:
            return None

    def _format_time(self, value):
        dt = self._parse_iso(value)
        if not dt:
            return '-'
        return dt.strftime('%d.%m.%Y %H:%M:%S')

    def _stats_missing(self, status):
        if not status.last_success_at:
            return False
        return not (status.last_files_count and status.last_source_size and status.last_dedupe_size and status.last_item)
