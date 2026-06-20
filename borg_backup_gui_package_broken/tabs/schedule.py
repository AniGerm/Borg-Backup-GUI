"""Zeitplan- und Wartungs-Tab: Backup-Planung pro Profil."""

import tkinter as tk
from tkinter import ttk


class ScheduleTab:
    """Tab für Backup-Zeitplan und Wartungskonfiguration pro Profil."""

    def __init__(self, notebook, app):
        self.app = app
        self.frame = ttk.Frame(notebook)
        notebook.add(self.frame, text='Zeitplan & Wartung')
        self._build_ui()

    def _build_ui(self):
        tab = self.frame

        # Profil-Auswahl
        profile_frame = ttk.Frame(tab)
        profile_frame.pack(fill='x', padx=10, pady=(10, 0))
        ttk.Label(profile_frame, text='Profil:').pack(side='left', padx=5)
        self.profile_combo = ttk.Combobox(profile_frame, state='readonly', width=35)
        self.profile_combo.pack(side='left', padx=5)
        self.profile_combo.bind('<<ComboboxSelected>>', self._on_profile_change)

        sched_frame = ttk.LabelFrame(tab, text='Backup-Zeitplan')
        sched_frame.pack(fill='x', padx=10, pady=10)

        self.schedule_type_var = tk.StringVar(value='manual')

        ttk.Radiobutton(sched_frame, text='Nur manuell', variable=self.schedule_type_var,
                        value='manual', command=self._on_change).grid(row=0, column=0, sticky='w', padx=5, pady=5)
        ttk.Radiobutton(sched_frame, text='Regelmäßiges Backup', variable=self.schedule_type_var,
                        value='interval', command=self._on_change).grid(row=1, column=0, sticky='w', padx=5, pady=5)

        frame_interval = ttk.Frame(sched_frame)
        frame_interval.grid(row=1, column=1, sticky='w')
        ttk.Label(frame_interval, text='Intervall:').pack(side='left', padx=5)
        self.schedule_interval_var = tk.StringVar(value='3')
        ttk.Spinbox(frame_interval, from_=1, to=168, textvariable=self.schedule_interval_var, width=5).pack(side='left')
        ttk.Label(frame_interval, text='Stunden').pack(side='left', padx=5)

        ttk.Radiobutton(sched_frame, text='Tägliches Backup', variable=self.schedule_type_var,
                        value='daily', command=self._on_change).grid(row=2, column=0, sticky='w', padx=5, pady=5)

        frame_daily = ttk.Frame(sched_frame)
        frame_daily.grid(row=2, column=1, sticky='w')
        ttk.Label(frame_daily, text='Uhrzeit:').pack(side='left', padx=5)
        self.schedule_time_var = tk.StringVar(value='03:15')
        ttk.Entry(frame_daily, textvariable=self.schedule_time_var, width=8).pack(side='left')

        self.catchup_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(sched_frame, text='Verpasste Backups nachholen',
                        variable=self.catchup_var).grid(row=3, column=0, columnspan=2, sticky='w', padx=5, pady=10)

        maint_frame = ttk.LabelFrame(tab, text='Wartung nach dem Backup')
        maint_frame.pack(fill='x', padx=10, pady=10)

        self.prune_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(maint_frame, text='Ausdünnen: Nach jedem Backup',
                        variable=self.prune_var).grid(row=0, column=0, sticky='w', padx=5, pady=5)

        self.validate_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(maint_frame, text='Validierung: Repositorydaten überprüfen',
                        variable=self.validate_var).grid(row=1, column=0, sticky='w', padx=5, pady=5)

        frame_val = ttk.Frame(maint_frame)
        frame_val.grid(row=1, column=1, sticky='w')
        ttk.Label(frame_val, text='Intervall:').pack(side='left', padx=5)
        self.validate_interval_var = tk.StringVar(value='3')
        ttk.Spinbox(frame_val, from_=1, to=52, textvariable=self.validate_interval_var, width=5).pack(side='left')
        ttk.Label(frame_val, text='Wochen').pack(side='left', padx=5)

        self.optimize_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(maint_frame, text='Optimierung: Repository optimieren',
                        variable=self.optimize_var).grid(row=2, column=0, sticky='w', padx=5, pady=5)

        frame_opt = ttk.Frame(maint_frame)
        frame_opt.grid(row=2, column=1, sticky='w')
        ttk.Label(frame_opt, text='Intervall:').pack(side='left', padx=5)
        self.optimize_interval_var = tk.StringVar(value='3')
        ttk.Spinbox(frame_opt, from_=1, to=52, textvariable=self.optimize_interval_var, width=5).pack(side='left')
        ttk.Label(frame_opt, text='Wochen').pack(side='left', padx=5)

        ttk.Button(tab, text='Einstellungen speichern & anwenden', command=self._on_save).pack(pady=10)

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
            self.load_from_profile()

    def load_from_profile(self):
        """Lädt Zeitplan-Einstellungen aus dem aktiven Profil."""
        profile = self.app.config.get_active_profile()
        if not profile:
            return

        self.schedule_type_var.set(profile.schedule_type)
        self.schedule_interval_var.set(str(profile.schedule_interval))
        self.schedule_time_var.set(profile.schedule_time)
        self.catchup_var.set(bool(profile.catchup_missed))
        self.prune_var.set(bool(profile.prune_enabled))
        self.validate_var.set(bool(profile.validate_enabled))
        self.validate_interval_var.set(str(profile.validate_interval))
        self.optimize_var.set(bool(profile.optimize_enabled))
        self.optimize_interval_var.set(str(profile.optimize_interval))

    def _save_to_profile(self):
        """Speichert Zeitplan-Einstellungen ins aktive Profil."""
        profile = self.app.config.get_active_profile()
        if not profile:
            return

        profile.schedule_type = self.schedule_type_var.get()
        profile.schedule_interval = int(self.schedule_interval_var.get() or 3)
        profile.schedule_time = self.schedule_time_var.get().strip()
        profile.catchup_missed = bool(self.catchup_var.get())
        profile.prune_enabled = bool(self.prune_var.get())
        profile.validate_enabled = bool(self.validate_var.get())
        profile.validate_interval = int(self.validate_interval_var.get() or 3)
        profile.optimize_enabled = bool(self.optimize_var.get())
        profile.optimize_interval = int(self.optimize_interval_var.get() or 3)

        self.app.config.update_profile(profile)

    def _on_change(self):
        """Wird bei jeder Änderung aufgerufen."""
        pass  # Nur Speichern beim expliziten Speichern-Button

    def _on_save(self):
        self._save_to_profile()
        self.app.reschedule_all()
        self.app.show_notice('Zeitplan und Wartungseinstellungen gespeichert.', level='success')
