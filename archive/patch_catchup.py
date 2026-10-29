import re

with open('hetzner_borg_gui.py', 'r') as f:
    content = f.read()

new_schedule_method = """    def _schedule_next_backup(self):
        if hasattr(self, 'scheduled_job_id') and self.scheduled_job_id:
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
        catchup = False
        if getattr(self, 'catchup_var', None):
            catchup = self.catchup_var.get()

        last_run_str = self.status_data.get('last_success_at', '')
        last_run = self._parse_iso_time(last_run_str)

        if mode == 'daily':
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
            self.scheduled_job_id = self.master.after(max(delay_ms, 1000), self._scheduled_backup_trigger)"""

content = re.sub(
    r"(?s)    def _schedule_next_backup\(self\):.*?    def _scheduled_backup_trigger\(self\):",
    new_schedule_method + "\n\n    def _scheduled_backup_trigger(self):",
    content
)

with open('hetzner_borg_gui.py', 'w') as f:
    f.write(content)
print("SUCCESS patch catchup")
