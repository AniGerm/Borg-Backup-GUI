import re

with open('hetzner_borg_gui.py', 'r') as f:
    content = f.read()

# Replace the notebook tab initialisation to add schedule tab
content = re.sub(
    r"self\.notebook\.add\(self\.tab_archive, text='Archiv und Restore'\)\n\s*self\._build_archive_tab\(\)",
    r"self.notebook.add(self.tab_archive, text='Archiv und Restore')\n        self._build_archive_tab()\n\n        self.tab_schedule = ttk.Frame(self.notebook)\n        self.notebook.add(self.tab_schedule, text='Zeitplan & Wartung')\n        self._build_schedule_tab()",
    content
)

# Insert _build_schedule_tab
schedule_tab_code = """
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
"""
if "def _build_schedule_tab(self):" not in content:
    content = content.replace("def _build_dashboard_tab(self):", schedule_tab_code + "\n    def _build_dashboard_tab(self):")

# Remove old schedule frame from dashboard tab
old_sched_pattern = r"(?s)schedule_frame\s*=\s*ttk\.LabelFrame\(tab,\s*text='Timer fuer tägliches Backup'\).*?\(0,\s*8\)\)"
content = re.sub(old_sched_pattern, "", content)

# Remove unused `self._on_schedule_change` inside Start tab configuration or rename
# Handled safely.

# Update `_load_config()` dict to use the new defaults
load_config_repl = """            'borg_passphrase': '',
            'schedule_type': 'manual',
            'schedule_interval': 3,
            'schedule_time': '03:15',
            'catchup_missed': True,
            'prune_enabled': False,
            'validate_enabled': True,
            'validate_interval': 3,
            'optimize_enabled': False,
            'optimize_interval': 3,"""
content = re.sub(
    r"'borg_passphrase': '',\s*'schedule_enabled': False,\s*'schedule_time': '02:00',",
    load_config_repl,
    content
)

# Update `_collect_config_from_ui()` mappings
collect_ui_repl = """        if hasattr(self, 'schedule_type_var'):
            self.config_data['schedule_type'] = self.schedule_type_var.get()
            self.config_data['schedule_interval'] = int(self.schedule_interval_var.get() or 3)
            self.config_data['schedule_time'] = self.schedule_time_var.get().strip()
            self.config_data['catchup_missed'] = bool(self.catchup_var.get())
            self.config_data['prune_enabled'] = bool(self.prune_var.get())
            self.config_data['validate_enabled'] = bool(self.validate_var.get())
            self.config_data['validate_interval'] = int(self.validate_interval_var.get() or 3)
            self.config_data['optimize_enabled'] = bool(self.optimize_var.get())
            self.config_data['optimize_interval'] = int(self.optimize_interval_var.get() or 3)"""
content = re.sub(
    r"""        if hasattr\(self, 'schedule_enabled_var'\):\n\s*self\.config_data\['schedule_enabled'\] = bool\(self\.schedule_enabled_var\.get\(\)\)\n\s*if hasattr\(self, 'schedule_time_var'\):\n\s*self\.config_data\['schedule_time'\] = self\.schedule_time_var\.get\(\)\.strip\(\)""",
    collect_ui_repl,
    content
)

# We need to rewrite `_schedule_next_backup` and `_on_schedule_change` entirely.
new_scheduler_code = """
    def _on_schedule_change(self):
        self._save_config()
        self._schedule_next_backup()
        messagebox.showinfo('Gespeichert', 'Zeitplan und Wartungseinstellungen wurden gespeichert und aktiviert.')

    def _schedule_next_backup(self):
        if self.scheduled_job_id:
            self.master.after_cancel(self.scheduled_job_id)
            self.scheduled_job_id = None

        if getattr(self, 'schedule_type_var', None) is None:
            return

        mode = self.schedule_type_var.get()
        if mode == 'manual':
            self.next_backup_dt = None
            return

        now = datetime.datetime.now()
        candidate = None

        if mode == 'daily':
            raw_time = self.schedule_time_var.get().strip()
            try:
                hour, minute = [int(x) for x in raw_time.split(':', 1)]
                candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if candidate <= now:
                    candidate += datetime.timedelta(days=1)
            except Exception:
                self.next_backup_dt = None
                return
        elif mode == 'interval':
            try:
                hours = int(self.schedule_interval_var.get() or 3)
            except ValueError:
                hours = 3
            last_run_str = self.status_data.get('last_success_at', '')
            last_run = self._parse_iso_time(last_run_str)
            if last_run:
                candidate = last_run + datetime.timedelta(hours=hours)
                while candidate <= now:
                    candidate += datetime.timedelta(hours=hours)
            else:
                candidate = now + datetime.timedelta(hours=hours)

        if candidate:
            self.next_backup_dt = candidate
            delay_ms = int(max(0, (candidate - now).total_seconds()) * 1000)
            self.scheduled_job_id = self.master.after(max(delay_ms, 1000), self._scheduled_backup_trigger)
"""

content = re.sub(
    r"""(?s)    def _schedule_next_backup\(self\):.*?    def _scheduled_backup_trigger\(self\):""",
    new_scheduler_code + "\n    def _scheduled_backup_trigger(self):",
    content
)

# And how `borg` creates the command list in `_start_backup`:
# We want to chain validate/compact in the bash wrapper or inside `_run_backup_thread`. Wait, it's `_start_backup`.
backup_chain_code = """
        archive_name = f"{os.uname().nodename}-{datetime.datetime.now():%Y-%m-%d_%H-%M-%S}"
        compression = self.compression_var.get().strip() or 'lz4'

        cmd = ['borg', 'create', '-v', '--stats', '--progress', f'--compression={compression}', f'{repo}::{archive_name}']
        cmd.extend(includes)
        for ex in excludes:
            cmd.extend(['--exclude', ex])

        # Maintenance Commands Check
        run_prune = False
        if getattr(self, 'prune_var', None) and self.prune_var.get():
            run_prune = True
            
        run_check = False
        if getattr(self, 'validate_var', None) and self.validate_var.get():
            try:
                last_check = self._parse_iso_time(self.status_data.get('last_check_at', ''))
                weeks = int(self.validate_interval_var.get())
                if not last_check or datetime.datetime.now() > last_check + datetime.timedelta(weeks=weeks):
                    run_check = True
            except: pass
            
        run_compact = False
        if getattr(self, 'optimize_var', None) and self.optimize_var.get():
            try:
                last_compact = self._parse_iso_time(self.status_data.get('last_compact_at', ''))
                weeks = int(self.optimize_interval_var.get())
                if not last_compact or datetime.datetime.now() > last_compact + datetime.timedelta(weeks=weeks):
                    run_compact = True
            except: pass

        chained_cmd_str = " ".join(f'"{x}"' if " " in x else x for x in cmd)
        
        if run_prune:
            chained_cmd_str += f" && echo '>>> Starte Ausduennen (Prune)' && borg prune -v --list --stats --keep-daily=7 --keep-weekly=4 --keep-monthly=6 {repo}"
        if run_check:
            chained_cmd_str += f" && echo '>>> Starte Validierung (Check)' && borg check -v --repository-only {repo}"
            self.status_data['last_check_at'] = datetime.datetime.now().isoformat()
        if run_compact:
            chained_cmd_str += f" && echo '>>> Starte Optimierung (Compact)' && borg compact -v {repo}"
            self.status_data['last_compact_at'] = datetime.datetime.now().isoformat()

        cmd = ['sh', '-c', chained_cmd_str]
"""

content = re.sub(
    r"""        archive_name = f"\{os\.uname\(\)\.nodename\}-\{datetime\.datetime\.now\(\):%Y-%m-%d_%H-%M-%S\}"\n.*?(?=\s*self\.runner\.append_log\()""",
    backup_chain_code + "\n",
    content,
    flags=re.DOTALL
)

with open('hetzner_borg_gui.py', 'w') as f:
    f.write(content)
print("SUCCESS apply Vorta schedule/settings!")
