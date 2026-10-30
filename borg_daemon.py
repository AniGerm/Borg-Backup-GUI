#!/usr/bin/python3
"""borg-backup-guid — root service that runs scheduled and manual Borg jobs.

The desktop GUI is only a client: it talks to this service over
``/run/borg-backup-gui/control.sock`` (see :mod:`borg_ipc`). Standard library only.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import pwd
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import borg_engine as engine
import borg_mounts as mounts
from borg_ipc import IpcError, IpcServer, Peer, PolkitAuthorizer
from borg_model import compute_next_run, format_schedule_summary
from borg_sysconfig import (
    STATUS_JOB_KEYS,
    SystemPaths,
    config_fingerprint,
    extract_system_config,
    import_ssh_keys,
    load_system_config,
    mask_secrets,
    merge_legacy_status,
    prune_unused_creds,
    prune_unused_keys,
    restore_masked_secrets,
    safe_name,
    save_system_config,
    validate_system_config,
    write_json_atomic,
)

LOG = logging.getLogger("borg-backup-guid")
TICK_SEC = 30.0
MISSED_GRACE = timedelta(minutes=10)
LOG_RING_SIZE = 3000
LOG_FILE_MAX_BYTES = 5 * 1024 * 1024
LOG_FILE_KEEP = 3
PROGRESS_EVENT_INTERVAL = 0.5
RUN_KINDS = ("backup", "extract", "delete", "init")


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now()).isoformat(timespec="seconds")


def _parse_iso(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def app_version() -> str:
    for path in (Path(__file__).resolve().parent / "VERSION", Path("/usr/lib/borg-backup-gui/VERSION")):
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return text.splitlines()[0]
    return "0.0.0"


@dataclass
class WorkItem:
    kind: str
    job: str
    params: dict[str, Any]
    trigger: str = "manual"
    requested_uid: int = 0
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: str = field(default_factory=_now_iso)
    started_at: str = ""
    label: str = ""
    task_label: str = ""
    task_index: int = 0
    task_count: int = 0
    stats: engine.LiveStats = field(default_factory=engine.LiveStats)
    stop_event: threading.Event = field(default_factory=threading.Event)

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "job": self.job,
            "trigger": self.trigger,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "label": self.label,
            "task_label": self.task_label,
            "task_index": self.task_index,
            "task_count": self.task_count,
            "cancel_requested": self.stop_event.is_set(),
            "stats": {
                "files_count": self.stats.files_count,
                "source_size": self.stats.source_size,
                "dedupe_size": self.stats.dedupe_size,
                "last_item": self.stats.last_item,
                "percent": self.stats.extra.get("percent"),
            },
        }


Runner = Callable[..., engine.CommandResult]


class Daemon:
    def __init__(
        self,
        paths: SystemPaths,
        *,
        runner: Runner = engine.run_command,
        clock: Callable[[], datetime] = datetime.now,
        hostname: str | None = None,
        path_exists: Callable[[str], bool] = os.path.exists,
    ) -> None:
        self.paths = paths
        self.runner = runner
        self.clock = clock
        self.hostname = hostname
        self.path_exists = path_exists
        self.lock = threading.RLock()
        self.wakeup = threading.Condition(self.lock)
        self.cfg = load_system_config(paths)
        self.status = self._load_status()
        self.queue: list[WorkItem] = []
        self.current: WorkItem | None = None
        self.next_runs: dict[str, datetime | None] = {}
        self.mounts: dict[str, dict[str, Any]] = {}
        self.log_ring: deque[dict[str, Any]] = deque(maxlen=LOG_RING_SIZE)
        self.log_seq = 0
        self.broadcast: Callable[[str, Any], None] = lambda _event, _data: None
        self.shutting_down = threading.Event()
        self._last_progress_emit = 0.0
        self._threads: list[threading.Thread] = []
        self.recompute_schedule()

    # ------------------------------------------------------------------ state
    def _load_status(self) -> dict[str, Any]:
        try:
            with open(self.paths.status_file, encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                data.setdefault("jobs", {})
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return {"jobs": {}, "last": {}}

    def save_status(self) -> None:
        with self.lock:
            snapshot = copy.deepcopy(self.status)
        try:
            write_json_atomic(self.paths.status_file, snapshot, 0o600)
        except OSError as exc:
            LOG.error("status.json nicht schreibbar: %s", exc)

    def job_status(self, name: str) -> dict[str, Any]:
        with self.lock:
            jobs = self.status.setdefault("jobs", {})
            entry = jobs.setdefault(name, {})
            return entry  # type: ignore[no-any-return]

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            jobs: dict[str, Any] = {}
            for job in self.cfg.get("jobs") or []:
                name = str(job.get("name") or "")
                entry = {key: self.status.get("jobs", {}).get(name, {}).get(key) for key in STATUS_JOB_KEYS}
                next_run = self.next_runs.get(name)
                entry["next_run_at"] = next_run.isoformat(timespec="seconds") if next_run else None
                entry["schedule_summary"] = format_schedule_summary(job)
                entry["queued"] = any(item.job == name and item.kind == "backup" for item in self.queue)
                entry["running"] = bool(self.current and self.current.kind == "backup" and self.current.job == name)
                jobs[name] = entry
            return {
                "version": app_version(),
                "running": self.current is not None,
                "current": self.current.summary() if self.current else None,
                "queue": [item.summary() for item in self.queue],
                "jobs": jobs,
                "last": copy.deepcopy(self.status.get("last") or {}),
                "last_restore": copy.deepcopy(self.status.get("last_restore") or {}),
                "mounts": list(self.mounts.values()),
                "config_fingerprint": config_fingerprint(mask_secrets(self.cfg)),
                "log_seq": self.log_seq,
            }

    def emit_status(self) -> None:
        self.broadcast("status", self.snapshot())

    # ------------------------------------------------------------------ logs
    def log(self, channel: str, text: str, *, job: str = "") -> None:
        text = text.rstrip("\n")
        if not text:
            return
        with self.lock:
            self.log_seq += 1
            entry = {"seq": self.log_seq, "ts": _now_iso(self.clock()), "channel": channel, "job": job, "text": text}
            self.log_ring.append(entry)
        self._append_log_file(job or channel, entry)
        self.broadcast("log", entry)

    def _append_log_file(self, name: str, entry: dict[str, Any]) -> None:
        path = self.paths.logs_dir / f"{safe_name(name, 'system')}.log"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > LOG_FILE_MAX_BYTES:
                for index in range(LOG_FILE_KEEP - 1, 0, -1):
                    older = path.with_suffix(f".log.{index}")
                    if older.exists():
                        older.replace(path.with_suffix(f".log.{index + 1}"))
                path.replace(path.with_suffix(".log.1"))
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(f"{entry['ts']} [{entry['channel']}] {entry['text']}\n")
        except OSError:
            pass

    def get_logs(self, since: int = 0, channel: str = "", limit: int = 1000) -> list[dict[str, Any]]:
        with self.lock:
            entries = [e for e in self.log_ring if e["seq"] > since and (not channel or e["channel"] == channel)]
        return entries[-max(1, min(limit, LOG_RING_SIZE)) :]

    # ------------------------------------------------------------- scheduling
    def _reference_time(self, name: str) -> datetime | None:
        entry = self.status.get("jobs", {}).get(name, {})
        stamps = [d for d in (_parse_iso(entry.get("last_success_at")), _parse_iso(entry.get("last_run_at"))) if d]
        return max(stamps) if stamps else None

    def compute_job_next_run(self, job: dict[str, Any], now: datetime) -> datetime | None:
        return compute_next_run(
            schedule_type=str(job.get("schedule_type") or "manual"),
            schedule_interval=int(job.get("schedule_interval") or 3),
            schedule_time=str(job.get("schedule_time") or "03:15"),
            catchup_missed=bool(job.get("catchup_missed", True)),
            last_success=self._reference_time(str(job.get("name") or "")),
            now=now,
        )

    def recompute_schedule(self, only: str | None = None) -> None:
        now = self.clock()
        with self.lock:
            names = set()
            for job in self.cfg.get("jobs") or []:
                name = str(job.get("name") or "")
                names.add(name)
                if only is None or only == name:
                    self.next_runs[name] = self.compute_job_next_run(job, now)
            for stale in set(self.next_runs) - names:
                self.next_runs.pop(stale, None)

    def scheduler_tick(self) -> list[str]:
        """Queue due jobs. Returns the names queued in this tick."""
        now = self.clock()
        queued: list[str] = []
        skipped: list[str] = []
        with self.lock:
            for job in self.cfg.get("jobs") or []:
                name = str(job.get("name") or "")
                due = self.next_runs.get(name)
                if due is None or now < due:
                    continue
                if self._job_busy(name):
                    continue
                if not job.get("catchup_missed", True) and now - due > MISSED_GRACE:
                    skipped.append(f"Geplanter Lauf von „{name}“ ({due:%d.%m. %H:%M}) verpasst – übersprungen.")
                    self.next_runs[name] = self.compute_job_next_run(job, now + timedelta(seconds=1))
                    continue
                self.next_runs[name] = None
                self.queue.append(WorkItem(kind="backup", job=name, params={}, trigger="schedule"))
                queued.append(name)
            if queued:
                self.wakeup.notify_all()
        for message in skipped:
            self.log("system", message)
        for name in queued:
            self.log("system", f"Geplantes Backup für „{name}“ eingereiht.")
        if queued:
            self.emit_status()
        return queued

    def _job_busy(self, name: str) -> bool:
        if self.current and self.current.kind == "backup" and self.current.job == name:
            return True
        return any(item.kind == "backup" and item.job == name for item in self.queue)

    def scheduler_loop(self) -> None:
        while not self.shutting_down.is_set():
            try:
                self.scheduler_tick()
            except Exception:
                LOG.exception("Scheduler-Fehler")
            self.shutting_down.wait(TICK_SEC)

    # ------------------------------------------------------------------ queue
    def enqueue(self, item: WorkItem) -> WorkItem:
        with self.lock:
            if item.kind == "backup" and self._job_busy(item.job):
                raise IpcError("busy", f"Backup für „{item.job}“ läuft bereits oder ist eingereiht.")
            self.queue.append(item)
            self.wakeup.notify_all()
        self.emit_status()
        return item

    def cancel(self, item_id: str = "") -> dict[str, Any]:
        with self.lock:
            if self.current and (not item_id or self.current.id == item_id):
                self.current.stop_event.set()
                target = self.current
            else:
                for item in list(self.queue):
                    if not item_id or item.id == item_id:
                        self.queue.remove(item)
                        if item.kind == "backup":
                            self.recompute_schedule(only=item.job)
                        self.log("system", f"Eingereihter Auftrag „{item.label or item.job}“ entfernt.")
                        self.emit_status()
                        return {"cancelled": item.id, "queued": True}
                raise IpcError("not_found", "Kein laufender Auftrag.")
        self.log(self._channel(target), "Abbruch angefordert – Borg schreibt einen Checkpoint …", job=target.job)
        self.emit_status()
        return {"cancelled": target.id, "queued": False}

    def worker_loop(self) -> None:
        while True:
            with self.lock:
                while not self.queue and not self.shutting_down.is_set():
                    self.wakeup.wait(timeout=5)
                if self.shutting_down.is_set():
                    return
                item = self.queue.pop(0)
                item.started_at = _now_iso(self.clock())
                self.current = item
            self.emit_status()
            try:
                self.execute(item)
            except Exception as exc:
                LOG.exception("Auftrag %s fehlgeschlagen", item.id)
                self.log(self._channel(item), f"[FEHLER] Interner Fehler: {exc}", job=item.job)
            finally:
                with self.lock:
                    self.current = None
                try:
                    self._release_idle_mounts()
                except Exception:
                    LOG.exception("Unmount nach Auftrag fehlgeschlagen")
                self.emit_status()

    def execute(self, item: WorkItem) -> None:
        if item.kind == "backup":
            self.run_backup(item)
        elif item.kind == "extract":
            self.run_extract(item)
        elif item.kind == "delete":
            self.run_delete(item)
        elif item.kind == "init":
            self.run_init(item)

    @staticmethod
    def _channel(item: WorkItem) -> str:
        return "backup" if item.kind in ("backup", "delete", "init") else "restore"

    def _run_task(self, item: WorkItem, cmd: list[str], env: dict[str, str], **kwargs: Any) -> engine.CommandResult:
        channel = self._channel(item)

        def on_line(line: str) -> None:
            progress = engine.parse_progress_line(line)
            if progress is None:
                progress = engine.parse_extract_progress(line, item.stats)
            if progress is not None:
                item.stats = progress
                now = time.monotonic()
                if now - self._last_progress_emit >= PROGRESS_EVENT_INTERVAL:
                    self._last_progress_emit = now
                    self.broadcast("progress", {"id": item.id, "job": item.job, "line": line.strip()})
                return
            engine.apply_stats_line(item.stats, line)
            self.log(channel, line, job=item.job)

        return self.runner(cmd, env, on_line=on_line, stop_event=item.stop_event, **kwargs)

    # ----------------------------------------------------------------- backup
    def run_backup(self, item: WorkItem) -> None:
        name = item.job
        started = self.clock()
        with self.lock:
            cfg = copy.deepcopy(self.cfg)
            entry = self.job_status(name)
            entry["last_run_at"] = _now_iso(started)
            entry["last_error"] = ""
        self.save_status()
        try:
            self._ensure_accounts_mounted(self._accounts_for_job_or_dest(job=name))
            plan = engine.plan_backup(
                cfg, name, self.paths, entry, now=started, hostname=self.hostname, path_exists=self.path_exists
            )
        except (engine.EngineError, IpcError) as exc:
            message = exc.message if isinstance(exc, IpcError) else str(exc)
            self.log("backup", f"[FEHLER] {message}", job=name)
            self._finish_backup(item, started, -1, message, [], None)
            return

        item.label = f"Backup {name}"
        item.task_count = len(plan.tasks)
        self.log("backup", f">>> Starte Backup „{name}“: {plan.archive_name} ({item.trigger})", job=name)
        if plan.missing_sources:
            self.log("backup", "[WARNUNG] Fehlende Quellen übersprungen: " + ", ".join(plan.missing_sources), job=name)
        if plan.canary:
            self.log("backup", f"[Canary] Check-Datei erstellt, Hash {plan.canary.expected_hash[:16]}…", job=name)

        dest_results: list[dict[str, Any]] = []
        maintenance_errors: list[str] = []
        check_ok = compact_ok = False
        for index, task in enumerate(plan.tasks, start=1):
            if item.stop_event.is_set():
                break
            item.task_index = index
            item.task_label = task.label
            self.emit_status()
            self.log("backup", f">>> {task.label}", job=name)
            result = self._run_task(item, task.cmd, task.env)
            if task.kind == "create":
                dest_results.append(
                    {
                        "id": task.dest_id,
                        "name": task.dest_name,
                        "ok": result.ok,
                        "exit_code": result.exit_code,
                        "error": "" if result.ok else engine.describe_failure(result),
                    }
                )
                if not result.ok and not result.stopped:
                    self.log("backup", f"[WARNUNG] Ziel {task.dest_name} fehlgeschlagen.", job=name)
                if index == len(plan.targets) and plan.canary and not item.stop_event.is_set():
                    self._verify_canary(item, plan, dest_results)
            elif not result.ok:
                if result.stopped:
                    break
                maintenance_errors.append(f"{task.label}: {engine.describe_failure(result)}")
            elif task.kind == "check" and task.dest_id == plan.targets[0].dest_id:
                check_ok = True
            elif task.kind == "compact" and task.dest_id == plan.targets[0].dest_id:
                compact_ok = True

        now_iso = _now_iso(self.clock())
        with self.lock:
            if check_ok:
                entry["last_check_at"] = now_iso
            if compact_ok:
                entry["last_compact_at"] = now_iso
        stopped = item.stop_event.is_set()
        ok_count = sum(1 for r in dest_results if r["ok"])
        fail_count = len(dest_results) - ok_count
        if stopped:
            exit_code, error = -2, "Backup wurde abgebrochen."
        elif not dest_results:
            exit_code, error = -1, "Kein Ziel wurde gesichert."
        elif fail_count:
            exit_code = 2
            error = f"Teilfehler: {fail_count} Ziel(e) fehlgeschlagen, {ok_count} erfolgreich."
            if fail_count == len(dest_results):
                error = "Backup fehlgeschlagen: " + "; ".join(str(r["error"]) for r in dest_results)
        else:
            exit_code = max(int(r["exit_code"]) for r in dest_results)
            error = "Backup mit Warnungen beendet." if exit_code == 1 else ""
        if maintenance_errors and not stopped:
            error = (error + " " if error else "") + "Wartung: " + "; ".join(maintenance_errors)
        self._finish_backup(item, started, exit_code, error, dest_results, plan, success=ok_count > 0 and not stopped)

    def _verify_canary(self, item: WorkItem, plan: engine.BackupPlan, dest_results: list[dict[str, Any]]) -> None:
        assert plan.canary is not None
        entry = self.job_status(item.job)
        ok_targets = [t for t, r in zip(plan.targets, dest_results) if r["ok"]]
        if not ok_targets:
            return
        item.task_label = "Canary-Prüfung"
        self.emit_status()
        results: list[str] = []
        stored_hash = ""
        for target in ok_targets:
            cmd = engine.canary_verify_cmd(target.repo, plan.archive_name, plan.canary.rel_path)
            res = self.runner(cmd, target.env, stop_event=item.stop_event, capture_stdout=True, timeout=300)
            if res.exit_code != 0:
                ok, message = False, engine.describe_failure(res)
            else:
                ok, stored_hash, message = engine.check_canary_content(res.stdout, plan.canary.expected_hash)
            results.append("ok" if ok else "fail")
            mark = "✅" if ok else "❌"
            self.log("backup", f"[Canary] {mark} {target.dest_name}: {message}", job=item.job)
        with self.lock:
            entry["canary_last_result"] = "ok" if all(r == "ok" for r in results) else "fail"
            entry["canary_last_check"] = _now_iso(self.clock())
            entry["canary_expected_hash"] = plan.canary.expected_hash
            entry["canary_verified_hash"] = stored_hash

    def _finish_backup(
        self,
        item: WorkItem,
        started: datetime,
        exit_code: int,
        error: str,
        dest_results: list[dict[str, Any]],
        plan: engine.BackupPlan | None,
        *,
        success: bool = False,
    ) -> None:
        finished = self.clock()
        with self.lock:
            entry = self.job_status(item.job)
            entry["last_exit_code"] = exit_code
            entry["last_error"] = error
            entry["last_duration_sec"] = round((finished - started).total_seconds(), 1)
            entry["last_dest_results"] = dest_results
            entry["last_files_count"] = item.stats.files_count
            entry["last_source_size"] = item.stats.source_size
            entry["last_dedupe_size"] = item.stats.dedupe_size
            entry["last_item"] = f"Archiv {plan.archive_name}" if plan else item.stats.last_item
            if success:
                entry["last_success_at"] = _now_iso(finished)
            last = {key: entry.get(key) for key in STATUS_JOB_KEYS}
            last.update({"job": item.job, "archive": plan.archive_name if plan else "", "trigger": item.trigger})
            self.status["last"] = last
            self.recompute_schedule(only=item.job)
        self.save_status()
        level = "OK" if success and not error else ("WARNUNG" if success else "FEHLER")
        self.log("backup", f"<<< Backup „{item.job}“ beendet [{level}] {error}".rstrip(), job=item.job)
        self.broadcast("job_finished", {"id": item.id, "kind": "backup", "job": item.job, "status": last})

    # ---------------------------------------------------------------- mounts
    def _accounts_for_job_or_dest(self, job: str = "", dest_id: str = "") -> list[dict[str, Any]]:
        with self.lock:
            cfg = copy.deepcopy(self.cfg)
        accounts: list[dict[str, Any]] = []
        seen: set[str] = set()
        dest_ids: list[str] = []
        if dest_id:
            dest_ids = [dest_id]
        elif job:
            found = engine.find_job(cfg, job) or {}
            dest_ids = [str(d) for d in (found.get("destination_ids") or [])]
        for did in dest_ids:
            dest = engine.find_by_id(cfg.get("destinations"), did)
            if not dest:
                continue
            acc = engine.find_by_id(cfg.get("accounts"), str(dest.get("account_id") or ""))
            if not acc:
                continue
            acc_id = str(acc.get("id") or "")
            if acc_id and acc_id not in seen:
                seen.add(acc_id)
                accounts.append(acc)
        return accounts

    def _ensure_accounts_mounted(self, accounts: list[dict[str, Any]]) -> None:
        for account in accounts:
            if not mounts.needs_mount(account):
                continue
            label = account.get("name") or account.get("id") or "?"
            try:
                mounts.ensure_mount(account, self.paths)
            except mounts.MountError as exc:
                raise IpcError("failed", str(exc)) from exc
            self.log("system", f"Datenträger/Freigabe für „{label}“ bereit.")

    def _busy_mount_account_ids(self) -> set[str]:
        """Account ids still needed by the current item or queued work."""
        needed: set[str] = set()
        with self.lock:
            items = list(self.queue)
            if self.current:
                items = [self.current, *items]
        for item in items:
            dest_id = str(item.params.get("dest_id") or "")
            for acc in self._accounts_for_job_or_dest(job=item.job, dest_id=dest_id):
                acc_id = str(acc.get("id") or "")
                if acc_id:
                    needed.add(acc_id)
        return needed

    def _release_idle_mounts(self) -> None:
        busy = self._busy_mount_account_ids()
        with self.lock:
            accounts = list(self.cfg.get("accounts") or [])
        for account in accounts:
            if not mounts.needs_mount(account):
                continue
            if str(account.get("id") or "") in busy:
                continue
            mounts.release_mount(account, self.paths)

    def list_volumes(self) -> dict[str, Any]:
        return {"volumes": mounts.list_removable_volumes()}

    # ---------------------------------------------------------------- restore
    def _target_for(self, job: str, dest_id: str = "") -> engine.Target:
        self._ensure_accounts_mounted(self._accounts_for_job_or_dest(job=job, dest_id=dest_id))
        with self.lock:
            cfg = copy.deepcopy(self.cfg)
        try:
            if dest_id:
                return engine.target_for_destination(cfg, dest_id, self.paths)
            return engine.primary_target(cfg, job, self.paths)
        except engine.EngineError as exc:
            raise IpcError("invalid", str(exc)) from exc

    def run_extract(self, item: WorkItem) -> None:
        params = item.params
        target_dir = str(params.get("target") or "")
        archive = str(params.get("archive") or "")
        item.label = f"Wiederherstellung {archive}"
        item.task_count = item.task_index = 1
        item.task_label = f"Extrahiere nach {target_dir}"
        started = self.clock()
        try:
            target = self._target_for(item.job, str(params.get("dest_id") or ""))
            cmd = engine.extract_cmd(target.repo, archive, list(params.get("paths") or []))
            Path(target_dir).mkdir(parents=True, exist_ok=True)
        except (IpcError, engine.EngineError, OSError) as exc:
            self._finish_restore(item, started, -1, str(exc))
            return
        self.log("restore", f">>> Wiederherstellung {archive} → {target_dir}", job=item.job)
        result = self._run_task(item, cmd, target.env, cwd=target_dir)
        error = "" if result.ok else engine.describe_failure(result)
        if result.ok and result.exit_code == 1:
            error = "Mit Warnungen beendet."
        self._finish_restore(item, started, result.exit_code, error)

    def _finish_restore(self, item: WorkItem, started: datetime, exit_code: int, error: str) -> None:
        info = {
            "id": item.id,
            "job": item.job,
            "archive": item.params.get("archive"),
            "target": item.params.get("target"),
            "paths": item.params.get("paths") or [],
            "started_at": _now_iso(started),
            "finished_at": _now_iso(self.clock()),
            "exit_code": exit_code,
            "error": error,
            "ok": exit_code in engine.OK_EXIT_CODES and not item.stop_event.is_set(),
        }
        with self.lock:
            self.status["last_restore"] = info
        self.save_status()
        self.log("restore", f"<<< Wiederherstellung beendet: {'OK' if info['ok'] else 'FEHLER'} {error}".rstrip())
        self.broadcast("job_finished", {"id": item.id, "kind": "extract", "job": item.job, "status": info})

    def run_delete(self, item: WorkItem) -> None:
        archive = str(item.params.get("archive") or "")
        item.label = item.task_label = f"Lösche Archiv {archive}"
        item.task_count = item.task_index = 1
        try:
            target = self._target_for(item.job, str(item.params.get("dest_id") or ""))
            cmd = engine.delete_archive_cmd(target.repo, archive)
        except (IpcError, engine.EngineError) as exc:
            self.log("backup", f"[FEHLER] {exc}", job=item.job)
            self.broadcast("job_finished", {"id": item.id, "kind": "delete", "job": item.job, "ok": False})
            return
        self.log("backup", f">>> {item.label}", job=item.job)
        result = self._run_task(item, cmd, target.env)
        message = "gelöscht" if result.ok else engine.describe_failure(result)
        self.log("backup", f"<<< Archiv {archive}: {message}", job=item.job)
        self.broadcast(
            "job_finished",
            {"id": item.id, "kind": "delete", "job": item.job, "ok": result.ok, "archive": archive, "error": message},
        )

    def run_init(self, item: WorkItem) -> None:
        dest_id = str(item.params.get("dest_id") or "")
        item.label = item.task_label = "Repository initialisieren"
        item.task_count = item.task_index = 1
        try:
            target = self._target_for("", dest_id)
            cmd = engine.init_repo_cmd(target.repo, str(item.params.get("encryption") or "repokey-blake2"))
        except (IpcError, engine.EngineError) as exc:
            self.log("backup", f"[FEHLER] {exc}")
            self.broadcast("job_finished", {"id": item.id, "kind": "init", "ok": False, "error": str(exc)})
            return
        self.log("backup", f">>> borg init {target.repo}")
        result = self._run_task(item, cmd, target.env)
        message = "initialisiert" if result.ok else engine.describe_failure(result)
        self.log("backup", f"<<< Repository {target.dest_name}: {message}")
        self.broadcast("job_finished", {"id": item.id, "kind": "init", "ok": result.ok, "error": message})

    # ------------------------------------------------------------------ mount
    def mount(self, job: str, archive: str, dest_id: str, peer: Peer) -> dict[str, Any]:
        target = self._target_for(job, dest_id)
        try:
            user = pwd.getpwuid(peer.uid)
            gid = user.pw_gid
        except KeyError:
            gid = peer.gid
        user_dir = self.paths.mount_dir / str(peer.uid)
        mountpoint = user_dir / safe_name(archive, "archiv")
        user_dir.mkdir(parents=True, exist_ok=True)
        if os.geteuid() == 0:
            os.chown(user_dir, peer.uid, gid)
        os.chmod(user_dir, 0o700)
        if os.path.ismount(mountpoint):
            return {"path": str(mountpoint), "already": True}
        mountpoint.mkdir(exist_ok=True)
        try:
            cmd = engine.mount_cmd(target.repo, archive, str(mountpoint), peer.uid, gid)
        except engine.EngineError as exc:
            raise IpcError("invalid", str(exc)) from exc
        result = engine.run_capture(cmd, target.env, timeout=300)
        if result.exit_code != 0:
            try:
                mountpoint.rmdir()
            except OSError:
                pass
            raise IpcError("failed", engine.describe_failure(result))
        info = {"path": str(mountpoint), "archive": archive, "job": job, "uid": peer.uid, "mounted_at": _now_iso()}
        with self.lock:
            self.mounts[str(mountpoint)] = info
        self.log("restore", f"Archiv {archive} eingehängt unter {mountpoint}", job=job)
        self.emit_status()
        return info

    def umount(self, path: str, peer: Peer) -> dict[str, Any]:
        with self.lock:
            info = self.mounts.get(path)
        if info is None or (peer.uid != 0 and int(info.get("uid", -1)) != peer.uid):
            raise IpcError("not_found", "Kein Mount dieses Benutzers unter diesem Pfad.")
        self._umount_path(path)
        self.emit_status()
        return {"path": path, "unmounted": True}

    def _umount_path(self, path: str) -> None:
        env = {"PATH": engine.SAFE_PATH, "LANG": "C.UTF-8"}
        result = engine.run_capture(engine.umount_cmd(path), env, timeout=60)
        if result.exit_code != 0 and os.path.ismount(path):
            subprocess.run(["umount", "-l", path], capture_output=True, check=False, timeout=30)
        with self.lock:
            self.mounts.pop(path, None)
        try:
            Path(path).rmdir()
        except OSError:
            pass
        self.log("restore", f"Archiv ausgehängt: {path}")

    # ----------------------------------------------------------------- config
    def get_config(self) -> dict[str, Any]:
        with self.lock:
            cfg = mask_secrets(self.cfg)
        return {"config": cfg, "fingerprint": config_fingerprint(cfg)}

    def save_config(self, incoming: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(incoming, dict):
            raise IpcError("invalid", "Konfiguration fehlt.")
        with self.lock:
            stored = copy.deepcopy(self.cfg)
        merged = extract_system_config(restore_masked_secrets(incoming, stored))
        errors = validate_system_config(merged)
        if errors:
            raise IpcError("invalid", "\n".join(errors))
        key_errors = import_ssh_keys(merged, self.paths)
        if key_errors:
            raise IpcError("invalid", "\n".join(key_errors))
        save_system_config(self.paths, merged)
        prune_unused_keys(merged, self.paths)
        prune_unused_creds(merged, self.paths)
        with self.lock:
            self.cfg = merged
            self.recompute_schedule()
        self.log("system", "Konfiguration gespeichert.")
        self.emit_status()
        return self.get_config()

    def import_legacy(self, incoming: dict[str, Any], legacy_status: dict[str, Any]) -> dict[str, Any]:
        result = self.save_config(incoming)
        names = [str(j.get("name") or "") for j in self.cfg.get("jobs") or []]
        with self.lock:
            merge_legacy_status(self.status, legacy_status if isinstance(legacy_status, dict) else {}, names)
            self.recompute_schedule()
        self.save_status()
        self.log("system", f"Konfiguration aus Version 2.x übernommen ({len(names)} Backup-Volumen).")
        self.emit_status()
        return result

    # --------------------------------------------------------------- dispatch
    def handle(self, method: str, params: dict[str, Any], peer: Peer) -> Any:
        if method == "ping":
            return {"version": app_version(), "uid": peer.uid, "pid": os.getpid()}
        if method == "get_status":
            return self.snapshot()
        if method == "get_config":
            return self.get_config()
        if method == "get_logs":
            return self.get_logs(int(params.get("since") or 0), str(params.get("channel") or ""))
        if method == "list_volumes":
            return self.list_volumes()
        if method == "list_archives":
            return self.list_archives(str(params.get("job") or ""), str(params.get("dest_id") or ""))
        if method == "archive_info":
            return self.archive_info(str(params.get("job") or ""), str(params.get("archive") or ""), params)
        if method == "run_job":
            job = str(params.get("job") or "")
            if not engine.find_job(self.cfg, job):
                raise IpcError("not_found", f"Backup-Volumen „{job}“ existiert nicht.")
            item = self.enqueue(WorkItem(kind="backup", job=job, params={}, requested_uid=peer.uid))
            return item.summary()
        if method == "cancel":
            return self.cancel(str(params.get("id") or ""))
        if method == "save_config":
            return self.save_config(params.get("config") or {})
        if method == "import_legacy":
            return self.import_legacy(params.get("config") or {}, params.get("status") or {})
        if method == "init_repo":
            dest_id = str(params.get("dest_id") or "")
            item = WorkItem(kind="init", job="", params={"dest_id": dest_id, "encryption": params.get("encryption")})
            return self.enqueue(item).summary()
        if method == "extract":
            return self._enqueue_extract(params, peer)
        if method == "delete_archive":
            archive = str(params.get("archive") or "")
            try:
                engine.archive_ref("x", archive)
            except engine.EngineError as exc:
                raise IpcError("invalid", str(exc)) from exc
            item = WorkItem(
                kind="delete",
                job=str(params.get("job") or ""),
                params={"archive": archive, "dest_id": str(params.get("dest_id") or "")},
                requested_uid=peer.uid,
            )
            return self.enqueue(item).summary()
        if method == "mount":
            return self.mount(
                str(params.get("job") or ""), str(params.get("archive") or ""), str(params.get("dest_id") or ""), peer
            )
        if method == "umount":
            return self.umount(str(params.get("path") or ""), peer)
        raise IpcError("unknown_method", method)

    def _enqueue_extract(self, params: dict[str, Any], peer: Peer) -> dict[str, Any]:
        target = str(params.get("target") or "").strip()
        if not target.startswith("/"):
            raise IpcError("invalid", "Zielverzeichnis muss ein absoluter Pfad sein.")
        archive = str(params.get("archive") or "")
        try:
            engine.archive_ref("x", archive)
            engine.extract_cmd("x", archive, list(params.get("paths") or []))
        except engine.EngineError as exc:
            raise IpcError("invalid", str(exc)) from exc
        item = WorkItem(
            kind="extract",
            job=str(params.get("job") or ""),
            params={
                "archive": archive,
                "target": os.path.normpath(target),
                "paths": [str(p) for p in params.get("paths") or []],
                "dest_id": str(params.get("dest_id") or ""),
            },
            requested_uid=peer.uid,
        )
        return self.enqueue(item).summary()

    def _busy(self) -> bool:
        with self.lock:
            return self.current is not None

    def list_archives(self, job: str, dest_id: str) -> dict[str, Any]:
        target = self._target_for(job, dest_id)
        cmd = engine.list_archives_cmd(target.repo)
        if self._busy():
            cmd.insert(2, "--bypass-lock")
        result = engine.run_capture(cmd, target.env, timeout=300)
        if result.exit_code != 0:
            raise IpcError("failed", engine.describe_failure(result))
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise IpcError("failed", f"Antwort von borg list nicht lesbar: {exc}") from exc
        archives = [
            {"name": a.get("name") or a.get("archive"), "time": a.get("time") or a.get("start"), "id": a.get("id")}
            for a in data.get("archives") or []
        ]
        return {"repo": target.repo, "dest": target.dest_name, "archives": archives}

    def archive_info(self, job: str, archive: str, params: dict[str, Any]) -> dict[str, Any]:
        target = self._target_for(job, str(params.get("dest_id") or ""))
        try:
            cmd = engine.archive_info_cmd(target.repo, archive)
        except engine.EngineError as exc:
            raise IpcError("invalid", str(exc)) from exc
        if self._busy():
            cmd.insert(2, "--bypass-lock")
        result = engine.run_capture(cmd, target.env, timeout=300)
        if result.exit_code != 0:
            raise IpcError("failed", engine.describe_failure(result))
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise IpcError("failed", f"Antwort von borg info nicht lesbar: {exc}") from exc
        info = (data.get("archives") or [{}])[0]
        stats = info.get("stats") or {}
        return {
            "name": info.get("name"),
            "start": info.get("start"),
            "end": info.get("end"),
            "duration": info.get("duration"),
            "hostname": info.get("hostname"),
            "command_line": info.get("command_line"),
            "stats": {
                "nfiles": stats.get("nfiles"),
                "original_size": stats.get("original_size"),
                "compressed_size": stats.get("compressed_size"),
                "deduplicated_size": stats.get("deduplicated_size"),
            },
        }

    # -------------------------------------------------------------- lifecycle
    def start_threads(self) -> None:
        for target, name in ((self.worker_loop, "worker"), (self.scheduler_loop, "scheduler")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def shutdown(self, timeout: float = engine.CANCEL_GRACE_SEC + 15) -> None:
        self.shutting_down.set()
        with self.lock:
            current = self.current
            self.queue.clear()
            self.wakeup.notify_all()
        if current is not None:
            current.stop_event.set()
            self.log(
                self._channel(current), "Dienst wird beendet – laufender Auftrag wird abgebrochen.", job=current.job
            )
        for thread in self._threads:
            thread.join(timeout=timeout)
        for path in list(self.mounts):
            self._umount_path(path)
        self.save_status()

    def cleanup_stale_mounts(self) -> None:
        if not self.paths.mount_dir.is_dir():
            return
        for user_dir in self.paths.mount_dir.iterdir():
            if not user_dir.is_dir():
                continue
            for entry in user_dir.iterdir():
                if os.path.ismount(entry):
                    subprocess.run(["umount", "-l", str(entry)], capture_output=True, check=False, timeout=30)
                try:
                    entry.rmdir()
                except OSError:
                    pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Borg Backup GUI service")
    parser.add_argument("--root", help="Prefix for /etc, /var/lib and /run (tests/dev)")
    parser.add_argument("--socket-mode", default="0666", help="Socket permissions (octal)")
    parser.add_argument("--no-polkit", action="store_true", help="Allow every local caller (dev only, never as root)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    paths = SystemPaths.under(args.root) if args.root else SystemPaths.default()
    if os.geteuid() != 0 and not args.root:
        LOG.error("borg-backup-guid muss als root laufen (oder mit --root für Tests).")
        return 2
    if args.no_polkit and os.geteuid() == 0:
        LOG.error("--no-polkit ist als root nicht erlaubt.")
        return 2
    if not shutil.which("borg") and not os.path.exists(engine.BORG_BIN):
        LOG.warning("borg wurde nicht gefunden – Backups schlagen fehl, bis borgbackup installiert ist.")

    paths.ensure()
    daemon = Daemon(paths)
    daemon.cleanup_stale_mounts()

    class _AllowAll:
        def check(self, peer: Peer, action: str) -> tuple[bool, str]:
            return True, ""

    authorizer = _AllowAll() if args.no_polkit else PolkitAuthorizer()
    server = IpcServer(paths.socket_path, daemon.handle, authorizer, int(args.socket_mode, 8))
    daemon.broadcast = server.broadcast
    daemon.start_threads()
    LOG.info("borg-backup-guid %s bereit auf %s", app_version(), paths.socket_path)
    daemon.log("system", f"Dienst gestartet (Version {app_version()}).")

    stop = threading.Event()

    def _on_signal(signum: int, _frame: Any) -> None:
        LOG.info("Signal %s empfangen, beende …", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True)
    server_thread.start()
    _notify_ready()
    while not stop.is_set():
        stop.wait(1.0)
    daemon.shutdown()
    server.shutdown()
    server.server_close()
    LOG.info("borg-backup-guid beendet.")
    return 0


def _notify_ready() -> None:
    sock_path = os.environ.get("NOTIFY_SOCKET")
    if not sock_path:
        return
    import socket

    address = "\0" + sock_path[1:] if sock_path.startswith("@") else sock_path
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(b"READY=1")
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
