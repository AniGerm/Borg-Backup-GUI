from __future__ import annotations

import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

import borg_engine as engine
from borg_sysconfig import SystemPaths, extract_system_config
from tests.conftest import make_config

NOW = datetime(2026, 10, 29, 3, 15, 0)


def _plan(paths: SystemPaths, cfg: dict, **kwargs):  # type: ignore[no-untyped-def]
    return engine.plan_backup(
        extract_system_config(cfg), "System", paths, kwargs.pop("status", {}), now=NOW, hostname="host", **kwargs
    )


def test_plan_creates_one_task_per_destination_with_safe_argv(sys_paths: SystemPaths) -> None:
    plan = _plan(sys_paths, make_config(dests=2), path_exists=lambda _p: True)

    creates = [t for t in plan.tasks if t.kind == "create"]
    assert [t.dest_id for t in creates] == ["d1", "d2"]
    assert plan.archive_name == "host-2026-10-29_03-15-00"
    cmd = creates[0].cmd
    sep = cmd.index("--")
    assert cmd[sep + 1] == "u1@u1.example.de:./borg::host-2026-10-29_03-15-00"
    assert cmd[sep + 2] == "/"
    assert "--exclude=/proc" in cmd[:sep]
    assert f"--exclude={sys_paths.borg_base_dir}" in cmd[:sep]
    assert f"--exclude={sys_paths.logs_dir}" in cmd[:sep]
    assert "--compression=zstd,3" in cmd
    assert creates[1].cmd[creates[1].cmd.index("--") + 1].startswith("/mnt/usb/borg::")


def test_plan_includes_canary_file_and_pattern(sys_paths: SystemPaths) -> None:
    plan = _plan(sys_paths, make_config(), path_exists=lambda _p: True)

    assert plan.canary is not None
    assert Path(plan.canary.path).is_file()
    create = plan.tasks[0].cmd
    assert plan.canary.path in create[create.index("--") :]
    assert f"--pattern=+{plan.canary.rel_path}" in create


def test_plan_maintenance_on_primary_only_by_default(sys_paths: SystemPaths) -> None:
    plan = _plan(sys_paths, make_config(dests=2), path_exists=lambda _p: True)
    kinds = [(t.kind, t.dest_id) for t in plan.tasks if t.kind != "create"]
    assert kinds == [("prune", "d1"), ("check", "d1"), ("compact", "d1")]
    prune = next(t for t in plan.tasks if t.kind == "prune").cmd
    assert prune[-2:] == ["--", "u1@u1.example.de:./borg"]
    assert "--keep-daily=7" in prune


def test_plan_maintenance_all_destinations(sys_paths: SystemPaths) -> None:
    cfg = make_config(dests=2)
    cfg["maintain_all_destinations"] = True
    plan = _plan(sys_paths, cfg, path_exists=lambda _p: True)
    assert {t.dest_id for t in plan.tasks if t.kind == "prune"} == {"d1", "d2"}


def test_plan_skips_check_and_compact_when_not_due(sys_paths: SystemPaths) -> None:
    status = {"last_check_at": "2026-10-20T03:00:00", "last_compact_at": "2026-10-28T03:00:00"}
    plan = _plan(sys_paths, make_config(), path_exists=lambda _p: True, status=status)
    assert [t.kind for t in plan.tasks] == ["create", "prune"]
    assert not plan.run_check and not plan.run_compact


def test_plan_reports_missing_sources(sys_paths: SystemPaths) -> None:
    cfg = make_config(sources=[{"path": "/data"}, {"path": "/mnt/gone"}])
    plan = _plan(sys_paths, cfg, path_exists=lambda p: p != "/mnt/gone")
    assert plan.missing_sources == ["/mnt/gone"]
    create = plan.tasks[0].cmd
    assert "/data" in create and "/mnt/gone" not in create


def test_plan_fails_without_available_sources(sys_paths: SystemPaths) -> None:
    with pytest.raises(engine.EngineError):
        _plan(sys_paths, make_config(), path_exists=lambda _p: False)


def test_plan_unknown_job(sys_paths: SystemPaths) -> None:
    with pytest.raises(engine.EngineError):
        engine.plan_backup(extract_system_config(make_config()), "Nope", sys_paths)


def test_borg_env_is_minimal_and_has_ssh_options(sys_paths: SystemPaths, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LD_PRELOAD", "/tmp/evil.so")
    monkeypatch.setenv("BORG_RSH", "ssh -o ProxyCommand=evil")
    cfg = extract_system_config(make_config())
    target = engine.resolve_targets(cfg, cfg["jobs"][0], sys_paths)[0]
    env = target.env

    assert "LD_PRELOAD" not in env
    assert env["BORG_PASSPHRASE"] == "geheim"
    assert env["BORG_BASE_DIR"] == str(sys_paths.borg_base_dir)
    assert env["BORG_RELOCATED_REPO_ACCESS_IS_OK"] == "no"
    rsh = env["BORG_RSH"]
    assert f"UserKnownHostsFile={sys_paths.known_hosts}" in rsh
    assert "BatchMode=yes" in rsh and "StrictHostKeyChecking=accept-new" in rsh
    assert "-p 23" in rsh
    assert "-i /etc/borg-backup-gui/keys/acc1" in rsh
    assert "ProxyCommand" not in rsh


def test_ssh_url_repo_has_no_port_flag(sys_paths: SystemPaths) -> None:
    account = {"type": "ssh", "ssh_port": 23, "ssh_key": ""}
    assert "-p" not in engine.ssh_command(account, sys_paths, "ssh://u1@host:23/./borg").split()


def test_trust_flags_only_when_confirmed(sys_paths: SystemPaths) -> None:
    cfg = make_config()
    cfg["repo_access_confirmed"] = True
    cfg = extract_system_config(cfg)
    env = engine.resolve_targets(cfg, cfg["jobs"][0], sys_paths)[0].env
    assert env["BORG_RELOCATED_REPO_ACCESS_IS_OK"] == "yes"
    assert env["BORG_UNKNOWN_UNENCRYPTED_REPO_ACCESS_IS_OK"] == "yes"


@pytest.mark.parametrize("bad", ["", "-x", "a::b", "a/b", "--help"])
def test_archive_ref_rejects_injection(bad: str) -> None:
    with pytest.raises(engine.EngineError):
        engine.archive_ref("repo", bad)


def test_extract_cmd_normalizes_and_rejects_traversal() -> None:
    cmd = engine.extract_cmd("repo", "arch", ["/home/max/Dokumente"])
    assert cmd[-3:] == ["--", "repo::arch", "home/max/Dokumente"]
    with pytest.raises(engine.EngineError):
        engine.extract_cmd("repo", "arch", ["/home/../etc"])
    with pytest.raises(engine.EngineError):
        engine.extract_cmd("repo", "arch", ["--dry-run"])


def test_init_repo_cmd_validates_encryption() -> None:
    assert engine.init_repo_cmd("/r")[-3:] == ["--encryption=repokey-blake2", "--", "/r"]
    with pytest.raises(engine.EngineError):
        engine.init_repo_cmd("/r", "rot13")


def test_canary_roundtrip(sys_paths: SystemPaths) -> None:
    info = engine.write_canary(sys_paths, "System", NOW)
    text = Path(info.path).read_text(encoding="utf-8")
    ok, stored, _msg = engine.check_canary_content(text, info.expected_hash)
    assert ok and stored == info.expected_hash

    tampered = text.replace("System", "Other")
    assert not engine.check_canary_content(tampered, info.expected_hash)[0]
    older = engine.write_canary(sys_paths, "System", datetime(2026, 1, 1))
    assert not engine.check_canary_content(Path(older.path).read_text(), info.expected_hash)[0]
    assert not engine.check_canary_content("", info.expected_hash)[0]


def test_progress_and_stats_parsing() -> None:
    progress = engine.parse_progress_line("1.20 GB O 800.00 MB C 120.00 MB D 4711 N home/max/file.txt")
    assert progress is not None
    assert progress.files_count == 4711
    assert progress.source_size == "1.20 GB"
    assert progress.dedupe_size == "120.00 MB"
    assert progress.last_item == "home/max/file.txt"
    assert engine.parse_progress_line("Creating archive at ...") is None
    extract = engine.parse_extract_progress(" 42.5% Extracting: etc/hosts")
    assert extract is not None and extract.extra["percent"] == 42.5 and extract.last_item == "etc/hosts"
    assert engine.parse_extract_progress("etc/hosts") is None

    stats = engine.LiveStats()
    assert engine.apply_stats_line(stats, "Number of files: 12345")
    assert engine.apply_stats_line(
        stats, "This archive:               10.00 GB              6.00 GB            1.00 GB"
    )
    assert stats.files_count == 12345
    assert stats.source_size == "10.00 GB" and stats.dedupe_size == "1.00 GB"


def test_maintenance_due() -> None:
    assert engine.maintenance_due("", 3, NOW)
    assert not engine.maintenance_due("2026-10-20T00:00:00", 3, NOW)
    assert engine.maintenance_due("2026-09-01T00:00:00", 3, NOW)


def test_run_command_streams_lines_and_exit_code() -> None:
    lines: list[str] = []
    script = "import sys; print('a'); sys.stderr.write('b\\rc\\n'); sys.exit(1)"
    result = engine.run_command([sys.executable, "-c", script], {"PATH": "/usr/bin:/bin"}, on_line=lines.append)
    assert result.exit_code == 1
    assert result.ok
    assert sorted(lines) == ["a", "b", "c"]


def test_run_command_capture_stdout_separately() -> None:
    script = "import sys; print('payload'); sys.stderr.write('log\\n')"
    lines: list[str] = []
    result = engine.run_command(
        [sys.executable, "-c", script], {"PATH": "/usr/bin:/bin"}, on_line=lines.append, capture_stdout=True
    )
    assert result.stdout.strip() == "payload"
    assert lines == ["log"]


def test_run_command_timeout_stops_process() -> None:
    begin = time.monotonic()
    result = engine.run_command(
        [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(60)"],
        {"PATH": "/usr/bin:/bin"},
        timeout=1.5,
    )
    assert result.stopped and not result.ok
    assert "Zeitüberschreitung" in (result.error or "")
    # Hard timeout: SIGTERM + TIMEOUT_KILL_GRACE_SEC (not CANCEL_GRACE_SEC).
    assert time.monotonic() - begin < 8


def test_run_command_missing_binary() -> None:
    result = engine.run_command(["/nonexistent/borg"], {})
    assert result.exit_code == -1 and result.error
    assert "Exit-Code" not in engine.describe_failure(result)
