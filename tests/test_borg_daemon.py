from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import borg_engine as engine
from borg_daemon import Daemon, WorkItem
from borg_ipc import IpcError, Peer
from borg_sysconfig import SECRET_MASK, SystemPaths, extract_system_config, save_system_config
from tests.conftest import make_config

USER = Peer(pid=1234, uid=1000, gid=1000)


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


class FakeRunner:
    def __init__(self, paths: SystemPaths, fail_repos: tuple[str, ...] = ()) -> None:
        self.paths = paths
        self.fail_repos = fail_repos
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], env: dict[str, str], **kwargs: Any) -> engine.CommandResult:
        self.calls.append(cmd)
        sub = cmd[1]
        on_line = kwargs.get("on_line")
        repo_arg = cmd[cmd.index("--") + 1] if "--" in cmd else ""
        if any(repo_arg.startswith(bad) for bad in self.fail_repos):
            if on_line:
                on_line("Repository does not exist.")
            return engine.CommandResult(exit_code=2, stderr="Repository does not exist.")
        if sub == "create" and on_line:
            on_line("10.00 MB O 5.00 MB C 1.00 MB D 42 N etc/hosts")
            on_line("Number of files: 42")
            on_line("This archive:               10.00 MB              5.00 MB              1.00 MB")
        if sub == "extract" and kwargs.get("capture_stdout"):
            return engine.CommandResult(exit_code=0, stdout=self.paths.canary_file.read_text(encoding="utf-8"))
        return engine.CommandResult(exit_code=0)


def _daemon(paths: SystemPaths, cfg: dict[str, Any], clock: FakeClock, runner: FakeRunner) -> Daemon:
    key = paths.keys_dir / "acc1"
    key.write_text("KEY", encoding="utf-8")
    for account in cfg.get("accounts") or []:
        if account.get("ssh_key"):
            account["ssh_key"] = str(key)
    save_system_config(paths, extract_system_config(cfg))
    events: list[tuple[str, Any]] = []
    daemon = Daemon(paths, runner=runner, clock=clock, hostname="host", path_exists=lambda _p: True)
    daemon.broadcast = lambda event, data: events.append((event, data))
    daemon.events = events  # type: ignore[attr-defined]
    return daemon


def test_backup_success_updates_status_and_canary(sys_paths: SystemPaths) -> None:
    clock = FakeClock(datetime(2026, 10, 29, 3, 15))
    runner = FakeRunner(sys_paths)
    daemon = _daemon(sys_paths, make_config(dests=2), clock, runner)

    daemon.run_backup(WorkItem(kind="backup", job="System", params={}))

    entry = daemon.status["jobs"]["System"]
    assert entry["last_success_at"] == "2026-10-29T03:15:00"
    assert entry["last_exit_code"] == 0 and entry["last_error"] == ""
    assert [r["ok"] for r in entry["last_dest_results"]] == [True, True]
    assert entry["canary_last_result"] == "ok"
    assert entry["last_check_at"] and entry["last_compact_at"]
    assert entry["last_files_count"] == 42 and entry["last_dedupe_size"] == "1.00 MB"
    subs = [c[1] for c in runner.calls]
    assert subs == ["create", "create", "extract", "extract", "prune", "check", "compact"]
    assert sys_paths.status_file.is_file()
    assert (sys_paths.logs_dir / "System.log").is_file()
    assert any(event == "job_finished" for event, _ in daemon.events)  # type: ignore[attr-defined]
    assert daemon.status["last"]["job"] == "System"


def test_backup_partial_failure(sys_paths: SystemPaths) -> None:
    clock = FakeClock(datetime(2026, 10, 29, 3, 15))
    daemon = _daemon(sys_paths, make_config(dests=2), clock, FakeRunner(sys_paths, fail_repos=("/mnt/usb",)))

    daemon.run_backup(WorkItem(kind="backup", job="System", params={}))

    entry = daemon.status["jobs"]["System"]
    assert entry["last_exit_code"] == 2
    assert entry["last_error"].startswith("Teilfehler")
    assert entry["last_success_at"]
    assert entry["canary_last_result"] == "ok"


def test_backup_total_failure_keeps_last_success(sys_paths: SystemPaths) -> None:
    clock = FakeClock(datetime(2026, 10, 29, 3, 15))
    daemon = _daemon(sys_paths, make_config(), clock, FakeRunner(sys_paths, fail_repos=("u1@",)))
    daemon.status["jobs"]["System"] = {"last_success_at": "2026-10-28T03:15:00"}

    daemon.run_backup(WorkItem(kind="backup", job="System", params={}))

    entry = daemon.status["jobs"]["System"]
    assert entry["last_success_at"] == "2026-10-28T03:15:00"
    assert entry["last_error"].startswith("Backup fehlgeschlagen")
    assert "canary_last_result" not in entry


def test_scheduler_fires_daily_job_once(sys_paths: SystemPaths) -> None:
    clock = FakeClock(datetime(2026, 10, 29, 3, 0))
    daemon = _daemon(sys_paths, make_config(), clock, FakeRunner(sys_paths))
    daemon.status["jobs"]["System"] = {"last_success_at": "2026-10-28T03:15:30"}
    daemon.recompute_schedule()
    assert daemon.next_runs["System"] == datetime(2026, 10, 29, 3, 15)

    assert daemon.scheduler_tick() == []
    clock.now = datetime(2026, 10, 29, 3, 15, 20)
    assert daemon.scheduler_tick() == ["System"]
    assert daemon.scheduler_tick() == []
    assert [item.trigger for item in daemon.queue] == ["schedule"]


def test_scheduler_skips_missed_run_without_catchup(sys_paths: SystemPaths) -> None:
    clock = FakeClock(datetime(2026, 10, 29, 3, 0))
    daemon = _daemon(sys_paths, make_config(catchup_missed=False), clock, FakeRunner(sys_paths))
    daemon.status["jobs"]["System"] = {"last_success_at": "2026-10-28T03:15:30"}
    daemon.recompute_schedule()

    clock.now = datetime(2026, 10, 29, 8, 0)
    assert daemon.scheduler_tick() == []
    assert daemon.next_runs["System"] == datetime(2026, 10, 30, 3, 15)


def test_scheduler_catches_up_missed_run(sys_paths: SystemPaths) -> None:
    clock = FakeClock(datetime(2026, 10, 29, 3, 0))
    daemon = _daemon(sys_paths, make_config(), clock, FakeRunner(sys_paths))
    daemon.status["jobs"]["System"] = {"last_success_at": "2026-10-28T03:15:30"}
    daemon.recompute_schedule()
    clock.now = datetime(2026, 10, 29, 8, 0)
    assert daemon.scheduler_tick() == ["System"]


def test_failed_run_does_not_loop(sys_paths: SystemPaths) -> None:
    clock = FakeClock(datetime(2026, 10, 29, 3, 15))
    daemon = _daemon(sys_paths, make_config(), clock, FakeRunner(sys_paths, fail_repos=("u1@",)))
    daemon.status["jobs"]["System"] = {"last_success_at": "2026-10-20T03:15:00"}
    daemon.run_backup(WorkItem(kind="backup", job="System", params={}))
    next_run = daemon.next_runs["System"]
    assert next_run is not None and next_run - clock.now > timedelta(hours=1)


def test_manual_schedule_has_no_next_run(sys_paths: SystemPaths) -> None:
    daemon = _daemon(sys_paths, make_config(schedule_type="manual"), FakeClock(datetime.now()), FakeRunner(sys_paths))
    assert daemon.next_runs["System"] is None
    assert daemon.snapshot()["jobs"]["System"]["next_run_at"] is None


def test_enqueue_rejects_duplicate_and_cancel_queued(sys_paths: SystemPaths) -> None:
    daemon = _daemon(sys_paths, make_config(), FakeClock(datetime.now()), FakeRunner(sys_paths))
    item = daemon.handle("run_job", {"job": "System"}, USER)
    with pytest.raises(IpcError) as exc:
        daemon.handle("run_job", {"job": "System"}, USER)
    assert exc.value.code == "busy"
    assert daemon.handle("cancel", {"id": item["id"]}, USER)["queued"] is True
    assert daemon.queue == []
    with pytest.raises(IpcError):
        daemon.handle("run_job", {"job": "Unbekannt"}, USER)


def test_config_roundtrip_masks_and_preserves_secrets(sys_paths: SystemPaths) -> None:
    daemon = _daemon(sys_paths, make_config(), FakeClock(datetime.now()), FakeRunner(sys_paths))
    got = daemon.handle("get_config", {}, USER)
    assert got["config"]["accounts"][0]["passphrase"] == SECRET_MASK

    cfg = got["config"]
    cfg["jobs"][0]["schedule_time"] = "04:30"
    saved = daemon.handle("save_config", {"config": cfg}, USER)
    assert saved["config"]["jobs"][0]["schedule_time"] == "04:30"
    assert daemon.cfg["accounts"][0]["passphrase"] == "geheim"

    cfg["jobs"][0]["compression"] = "bogus"
    with pytest.raises(IpcError) as exc:
        daemon.handle("save_config", {"config": cfg}, USER)
    assert exc.value.code == "invalid"
    assert daemon.cfg["jobs"][0]["schedule_time"] == "04:30"


def test_import_legacy_merges_status(sys_paths: SystemPaths, tmp_path: Path) -> None:
    daemon = _daemon(sys_paths, {"accounts": [], "destinations": [], "jobs": []}, FakeClock(datetime.now()), FakeRunner(sys_paths))
    key = tmp_path / "id_ed25519"
    key.write_text("KEY", encoding="utf-8")
    cfg = make_config()
    cfg["accounts"][0]["ssh_key"] = str(key)
    daemon.handle("import_legacy", {"config": cfg, "status": {"last_success_at": "2026-10-01T03:00:00"}}, USER)
    assert daemon.status["jobs"]["System"]["last_success_at"] == "2026-10-01T03:00:00"
    assert daemon.cfg["accounts"][0]["ssh_key"].startswith(str(sys_paths.keys_dir))
    assert daemon.cfg["accounts"][0]["passphrase"] == "geheim"


def test_extract_validation(sys_paths: SystemPaths) -> None:
    daemon = _daemon(sys_paths, make_config(), FakeClock(datetime.now()), FakeRunner(sys_paths))
    for params in (
        {"job": "System", "archive": "a", "target": "relative"},
        {"job": "System", "archive": "--evil", "target": "/tmp/x"},
        {"job": "System", "archive": "a", "target": "/tmp/x", "paths": ["../etc"]},
    ):
        with pytest.raises(IpcError):
            daemon.handle("extract", params, USER)
    item = daemon.handle("extract", {"job": "System", "archive": "a", "target": "/tmp/x/", "paths": ["/etc"]}, USER)
    assert item["kind"] == "extract"
    assert daemon.queue[0].params["target"] == "/tmp/x"


def test_run_extract_uses_target_cwd(sys_paths: SystemPaths, tmp_path: Path) -> None:
    runner = FakeRunner(sys_paths)
    captured: dict[str, Any] = {}

    def spy(cmd: list[str], env: dict[str, str], **kwargs: Any) -> engine.CommandResult:
        captured.update(kwargs)
        return runner(cmd, env, **kwargs)

    daemon = _daemon(sys_paths, make_config(), FakeClock(datetime.now()), runner)
    daemon.runner = spy
    target = tmp_path / "restore"
    daemon.run_extract(WorkItem(kind="extract", job="System", params={"archive": "a", "target": str(target)}))
    assert captured["cwd"] == str(target)
    assert daemon.status["last_restore"]["ok"] is True


def test_worker_thread_runs_and_shutdown(sys_paths: SystemPaths) -> None:
    clock = FakeClock(datetime(2026, 10, 29, 3, 15))
    daemon = _daemon(sys_paths, make_config(schedule_type="manual"), clock, FakeRunner(sys_paths))
    daemon.start_threads()
    try:
        daemon.handle("run_job", {"job": "System"}, USER)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if daemon.status["jobs"].get("System", {}).get("last_success_at") and daemon.current is None:
                break
            time.sleep(0.05)
        assert daemon.status["jobs"]["System"]["last_success_at"]
        assert daemon.get_logs(channel="backup")
    finally:
        daemon.shutdown(timeout=2)
    assert all(not t.is_alive() for t in daemon._threads if t.name == "worker")


def test_cancel_running_sets_stop_event(sys_paths: SystemPaths) -> None:
    started = threading.Event()

    def slow(cmd: list[str], env: dict[str, str], **kwargs: Any) -> engine.CommandResult:
        started.set()
        stop: threading.Event = kwargs["stop_event"]
        stop.wait(5)
        return engine.CommandResult(exit_code=2, stopped=stop.is_set())

    daemon = _daemon(sys_paths, make_config(schedule_type="manual"), FakeClock(datetime.now()), FakeRunner(sys_paths))
    daemon.runner = slow
    daemon.start_threads()
    try:
        daemon.handle("run_job", {"job": "System"}, USER)
        assert started.wait(5)
        daemon.handle("cancel", {}, USER)
        deadline = time.monotonic() + 5
        while daemon.current is not None and time.monotonic() < deadline:
            time.sleep(0.05)
        entry = daemon.status["jobs"]["System"]
        assert entry["last_error"] == "Backup wurde abgebrochen."
        assert not entry.get("last_success_at")
    finally:
        daemon.shutdown(timeout=2)
