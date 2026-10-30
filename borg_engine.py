"""Headless Borg task engine used by the root service.

Builds borg command lines and environments from the system config, runs them
with streaming output and cooperative cancellation, and implements the canary
round-trip check. Standard library only, no Tk.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from borg_model import resolve_repo
from borg_mounts import absolute_repo_path
from borg_sysconfig import SystemPaths

BORG_BIN = shutil.which("borg") or "/usr/bin/borg"
PRUNE_KEEP = ("--keep-daily=7", "--keep-weekly=4", "--keep-monthly=6")
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
CANCEL_GRACE_SEC = 30.0
# Hard timeouts escalate faster than user cancel (borg checkpoint grace).
TIMEOUT_KILL_GRACE_SEC = 2.0
OK_EXIT_CODES = (0, 1)

_PROGRESS_RE = re.compile(
    r"^([0-9.,]+\s*[KMGTPE]?i?B)\s+O\s+([0-9.,]+\s*[KMGTPE]?i?B)\s+C\s+([0-9.,]+\s*[KMGTPE]?i?B)\s+D\s+(\d+)\s+N\s*(.*)$"
)
_SIZE = r"([0-9.,]+\s*[KMGTPE]?i?B)"
_ARCHIVE_TOTALS_RE = re.compile(rf"This archive:\s+{_SIZE}\s+{_SIZE}\s+{_SIZE}")
_FILES_RE = re.compile(r"Number of files:\s*(\d+)")
_EXTRACT_PROGRESS_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)%\s+Extracting:\s*(.*)$")


class EngineError(ValueError):
    """Invalid job or destination configuration."""


@dataclass
class Task:
    kind: str
    label: str
    cmd: list[str]
    env: dict[str, str]
    dest_id: str = ""
    dest_name: str = ""
    repo: str = ""


@dataclass
class Target:
    dest_id: str
    dest_name: str
    repo: str
    env: dict[str, str]


@dataclass
class CanaryInfo:
    path: str
    rel_path: str
    expected_hash: str


@dataclass
class BackupPlan:
    job_name: str
    archive_name: str
    tasks: list[Task]
    targets: list[Target]
    missing_sources: list[str]
    canary: CanaryInfo | None = None
    run_check: bool = False
    run_compact: bool = False


@dataclass
class CommandResult:
    exit_code: int
    stopped: bool = False
    error: str = ""
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code in OK_EXIT_CODES and not self.stopped and not self.error


@dataclass
class LiveStats:
    files_count: int = 0
    source_size: str = ""
    compressed_size: str = ""
    dedupe_size: str = ""
    last_item: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def find_job(cfg: dict[str, Any], name: str) -> dict[str, Any] | None:
    for job in cfg.get("jobs") or []:
        if str(job.get("name") or "") == name:
            return dict(job)
    return None


def find_by_id(items: list[dict[str, Any]] | None, item_id: str) -> dict[str, Any] | None:
    for item in items or []:
        if str(item.get("id") or "") == item_id:
            return dict(item)
    return None


def is_scp_style(repo: str) -> bool:
    return "://" not in repo and not repo.startswith("/") and ":" in repo


def ssh_command(account: dict[str, Any], paths: SystemPaths, repo: str) -> str:
    parts = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={paths.known_hosts}",
        "-o",
        "ConnectTimeout=30",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
    ]
    if is_scp_style(repo):
        parts += ["-p", str(int(account.get("ssh_port") or 23))]
    key = str(account.get("ssh_key") or "").strip()
    if key:
        parts += ["-i", key, "-o", "IdentitiesOnly=yes"]
    return shlex.join(parts)


def borg_env(
    account: dict[str, Any],
    destination: dict[str, Any],
    paths: SystemPaths,
    *,
    trust_repo_access: bool = False,
) -> dict[str, str]:
    """Minimal environment for one destination (never inherits the caller's env)."""
    env = {
        "PATH": SAFE_PATH,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": str(paths.state_dir),
        "BORG_BASE_DIR": str(paths.borg_base_dir),
        "BORG_DISPLAY_PASSPHRASE": "no",
        "BORG_RELOCATED_REPO_ACCESS_IS_OK": "no",
        "BORG_UNKNOWN_UNENCRYPTED_REPO_ACCESS_IS_OK": "no",
        "PYTHONUNBUFFERED": "1",
    }
    passphrase = str(account.get("passphrase") or "")
    if passphrase:
        env["BORG_PASSPHRASE"] = passphrase
    if trust_repo_access:
        env["BORG_RELOCATED_REPO_ACCESS_IS_OK"] = "yes"
        env["BORG_UNKNOWN_UNENCRYPTED_REPO_ACCESS_IS_OK"] = "yes"
    acc_type = str(account.get("type") or "ssh")
    if acc_type == "s3":
        for src, dst in (
            ("s3_access_key", "AWS_ACCESS_KEY_ID"),
            ("s3_secret_key", "AWS_SECRET_ACCESS_KEY"),
            ("s3_endpoint_url", "AWS_ENDPOINT_URL"),
            ("s3_region", "AWS_DEFAULT_REGION"),
        ):
            value = str(account.get(src) or "").strip()
            if value:
                env[dst] = value
    elif acc_type == "ssh":
        env["BORG_RSH"] = ssh_command(account, paths, resolve_repo(account, destination))
    return env


def resolve_targets(cfg: dict[str, Any], job: dict[str, Any], paths: SystemPaths) -> list[Target]:
    trust = bool(cfg.get("repo_access_confirmed"))
    targets: list[Target] = []
    for dest_id in job.get("destination_ids") or []:
        dest = find_by_id(cfg.get("destinations"), str(dest_id))
        if not dest:
            raise EngineError(f"Ziel {dest_id} existiert nicht.")
        account = find_by_id(cfg.get("accounts"), str(dest.get("account_id") or ""))
        if not account:
            raise EngineError(f"Ziel „{dest.get('name') or dest_id}“ hat keinen Account.")
        repo = absolute_repo_path(account, dest, paths).strip()
        if not repo:
            raise EngineError(f"Ziel „{dest.get('name') or dest_id}“ hat kein Repository.")
        if repo.startswith("-"):
            raise EngineError(f"Ziel „{dest.get('name') or dest_id}“: ungültiges Repository.")
        targets.append(
            Target(
                dest_id=str(dest_id),
                dest_name=str(dest.get("name") or repo),
                repo=repo,
                env=borg_env(account, dest, paths, trust_repo_access=trust),
            )
        )
    if not targets:
        raise EngineError(f"Backup-Volumen „{job.get('name')}“ hat kein Ziel.")
    return targets


def target_for_destination(cfg: dict[str, Any], dest_id: str, paths: SystemPaths) -> Target:
    return resolve_targets(cfg, {"name": dest_id, "destination_ids": [dest_id]}, paths)[0]


def primary_target(cfg: dict[str, Any], job_name: str, paths: SystemPaths) -> Target:
    job = find_job(cfg, job_name)
    if not job:
        raise EngineError(f"Backup-Volumen „{job_name}“ existiert nicht.")
    return resolve_targets(cfg, job, paths)[0]


def archive_ref(repo: str, archive: str) -> str:
    if not archive or archive.startswith("-") or "::" in archive or "/" in archive:
        raise EngineError(f"Ungültiger Archivname: {archive!r}")
    return f"{repo}::{archive}"


def _parse_iso(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def maintenance_due(last_at: Any, interval_weeks: Any, now: datetime) -> bool:
    last = _parse_iso(last_at)
    try:
        weeks = max(1, int(interval_weeks))
    except (TypeError, ValueError):
        weeks = 3
    return last is None or now > last + timedelta(weeks=weeks)


def write_canary(paths: SystemPaths, job_name: str, now: datetime | None = None) -> CanaryInfo:
    """Write the canary file (same format as 2.x) and return its expected hash."""
    stamp = (now or datetime.now()).isoformat(timespec="seconds")
    content_lines = ["BORG-CANARY-CHECK", f"Profile: {job_name}", f"Timestamp: {stamp}"]
    content_text = "\n".join(content_lines) + "\n"
    expected = hashlib.sha256(content_text.encode()).hexdigest()
    final_text = "\n".join([*content_lines, f"SHA256: {expected}"]) + "\n"
    path = paths.canary_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(final_text, encoding="utf-8")
    return CanaryInfo(path=str(path), rel_path=str(path).lstrip("/"), expected_hash=expected)


def check_canary_content(text: str, expected_hash: str) -> tuple[bool, str, str]:
    """Return ``(ok, stored_hash, message)`` for extracted canary content."""
    if not text.strip():
        return False, "", "Canary-Datei leer oder nicht im Archiv"
    stored = ""
    data_lines: list[str] = []
    for line in text.strip().split("\n"):
        if line.startswith("SHA256:"):
            stored = line.split("SHA256:", 1)[1].strip()
        else:
            data_lines.append(line)
    if not stored:
        return False, "", "Kein SHA256-Eintrag in extrahierter Datei"
    computed = hashlib.sha256(("\n".join(data_lines) + "\n").encode()).hexdigest()
    if computed != stored:
        return False, stored, "Inhalt passt nicht zum gespeicherten Hash"
    if expected_hash and stored != expected_hash:
        return False, stored, "Archiv enthält eine ältere Canary-Datei"
    return True, stored, "Hash verifiziert"


def plan_backup(
    cfg: dict[str, Any],
    job_name: str,
    paths: SystemPaths,
    job_status: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
    hostname: str | None = None,
    path_exists: Callable[[str], bool] = os.path.exists,
    write_canary_file: bool = True,
) -> BackupPlan:
    """Build the ordered task list for one backup run of ``job_name``."""
    job = find_job(cfg, job_name)
    if not job:
        raise EngineError(f"Backup-Volumen „{job_name}“ existiert nicht.")
    current = now or datetime.now()
    status = job_status or {}
    targets = resolve_targets(cfg, job, paths)

    includes: list[str] = []
    missing: list[str] = []
    for path in job.get("include_folders") or ["/"]:
        path = str(path).strip()
        if not path.startswith("/"):
            raise EngineError(f"Quelle {path!r} muss ein absoluter Pfad sein.")
        if path in includes or path in missing:
            continue
        (includes if path_exists(path) else missing).append(path)
    if not includes:
        raise EngineError("Keine der Quellen ist verfügbar (nicht gemountet?).")

    excludes = [str(e).strip() for e in job.get("exclude_folders") or [] if str(e).strip()]
    for internal in (paths.borg_base_dir, paths.logs_dir):
        if str(internal) not in excludes:
            excludes.append(str(internal))

    compression = str(job.get("compression") or "lz4").strip()
    archive_name = f"{hostname or os.uname().nodename}-{current:%Y-%m-%d_%H-%M-%S}"

    canary: CanaryInfo | None = None
    if cfg.get("canary_enabled", True):
        if write_canary_file:
            canary = write_canary(paths, job_name, current)
        else:
            canary = CanaryInfo(str(paths.canary_file), str(paths.canary_file).lstrip("/"), "")
        if canary.path not in includes:
            includes.append(canary.path)

    tasks: list[Task] = []
    for index, target in enumerate(targets):
        cmd = [
            BORG_BIN,
            "create",
            "--lock-wait=60",
            "--progress",
            "-v",
            "--stats",
            "--show-rc",
            f"--compression={compression}",
        ]
        if canary:
            cmd.append(f"--pattern=+{canary.rel_path}")
        cmd += [f"--exclude={pattern}" for pattern in excludes]
        cmd += ["--", archive_ref(target.repo, archive_name), *includes]
        label = f"Backup → {target.dest_name}" if index else f"Starte Backup: {archive_name}"
        tasks.append(Task("create", label, cmd, target.env, target.dest_id, target.dest_name, target.repo))

    run_prune = bool(job.get("prune_enabled"))
    run_check = bool(job.get("validate_enabled")) and maintenance_due(
        status.get("last_check_at"), job.get("validate_interval"), current
    )
    run_compact = bool(job.get("optimize_enabled")) and maintenance_due(
        status.get("last_compact_at"), job.get("optimize_interval"), current
    )
    maintained = targets if cfg.get("maintain_all_destinations") else targets[:1]
    for index, target in enumerate(maintained):
        suffix = f" → {target.dest_name}" if index else ""
        common = (target.env, target.dest_id, target.dest_name, target.repo)
        if run_prune:
            cmd = [BORG_BIN, "prune", "--lock-wait=60", "-v", "--list", "--stats", *PRUNE_KEEP, "--", target.repo]
            tasks.append(Task("prune", f"Prune{suffix}", cmd, *common))
        if run_check:
            cmd = [BORG_BIN, "check", "--lock-wait=60", "-v", "--repository-only", "--", target.repo]
            tasks.append(Task("check", f"Check{suffix}", cmd, *common))
        if run_compact:
            cmd = [BORG_BIN, "compact", "--lock-wait=60", "-v", "--", target.repo]
            tasks.append(Task("compact", f"Compact{suffix}", cmd, *common))

    return BackupPlan(
        job_name=job_name,
        archive_name=archive_name,
        tasks=tasks,
        targets=targets,
        missing_sources=missing,
        canary=canary,
        run_check=run_check,
        run_compact=run_compact,
    )


def canary_verify_cmd(repo: str, archive: str, rel_path: str) -> list[str]:
    return [BORG_BIN, "extract", "--lock-wait=60", "--stdout", "--", archive_ref(repo, archive), rel_path]


def list_archives_cmd(repo: str) -> list[str]:
    return [BORG_BIN, "list", "--lock-wait=30", "--json", "--", repo]


def archive_info_cmd(repo: str, archive: str) -> list[str]:
    return [BORG_BIN, "info", "--lock-wait=30", "--json", "--", archive_ref(repo, archive)]


def delete_archive_cmd(repo: str, archive: str) -> list[str]:
    return [BORG_BIN, "delete", "--lock-wait=60", "-v", "--stats", "--", archive_ref(repo, archive)]


def mount_cmd(repo: str, archive: str, mountpoint: str, uid: int, gid: int) -> list[str]:
    return [
        BORG_BIN,
        "mount",
        "--lock-wait=60",
        "-o",
        f"allow_other,uid={int(uid)},gid={int(gid)}",
        "--",
        archive_ref(repo, archive),
        mountpoint,
    ]


def umount_cmd(mountpoint: str) -> list[str]:
    return [BORG_BIN, "umount", "--", mountpoint]


def init_repo_cmd(repo: str, encryption: str = "repokey-blake2") -> list[str]:
    if encryption not in ("repokey-blake2", "repokey", "keyfile-blake2", "keyfile", "none"):
        raise EngineError(f"Unbekannte Verschlüsselung: {encryption}")
    return [BORG_BIN, "init", "-v", f"--encryption={encryption}", "--", repo]


def extract_cmd(repo: str, archive: str, include_paths: list[str] | None = None) -> list[str]:
    cmd = [BORG_BIN, "extract", "--lock-wait=60", "--progress", "--", archive_ref(repo, archive)]
    for path in include_paths or []:
        rel = str(path).strip().lstrip("/")
        if not rel or rel.startswith("-") or any(c in rel for c in "\n\r\x00"):
            raise EngineError(f"Ungültiger Pfadfilter: {path!r}")
        if ".." in Path(rel).parts:
            raise EngineError(f"Pfadfilter darf kein '..' enthalten: {path!r}")
        cmd.append(rel)
    return cmd


def parse_progress_line(line: str) -> LiveStats | None:
    match = _PROGRESS_RE.match(line.strip())
    if not match:
        return None
    return LiveStats(
        files_count=int(match.group(4)),
        source_size=match.group(1),
        compressed_size=match.group(2),
        dedupe_size=match.group(3),
        last_item=match.group(5)[:200],
    )


def parse_extract_progress(line: str, previous: LiveStats | None = None) -> LiveStats | None:
    match = _EXTRACT_PROGRESS_RE.match(line)
    if not match:
        return None
    stats = previous or LiveStats()
    stats.extra["percent"] = float(match.group(1))
    stats.last_item = match.group(2)[:200]
    return stats


def apply_stats_line(stats: LiveStats, line: str) -> bool:
    """Update ``stats`` from ``--stats`` summary lines. Returns True if changed."""
    changed = False
    files = _FILES_RE.search(line)
    if files:
        stats.files_count = int(files.group(1))
        changed = True
    totals = _ARCHIVE_TOTALS_RE.search(line)
    if totals:
        stats.source_size = totals.group(1)
        stats.compressed_size = totals.group(2)
        stats.dedupe_size = totals.group(3)
        changed = True
    return changed


def _split_lines(buffer: str) -> tuple[list[str], str]:
    parts = re.split(r"[\r\n]", buffer)
    return parts[:-1], parts[-1]


def run_command(
    cmd: list[str],
    env: dict[str, str],
    *,
    on_line: Callable[[str], None] | None = None,
    stop_event: threading.Event | None = None,
    cwd: str | None = None,
    timeout: float | None = None,
    capture_stdout: bool = False,
) -> CommandResult:
    """Run ``cmd`` streaming stderr (and stdout unless captured) line by line.

    ``stop_event`` sends SIGINT (borg writes a checkpoint), escalating to
    SIGTERM/SIGKILL after :data:`CANCEL_GRACE_SEC`.
    """
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if capture_stdout else subprocess.STDOUT,
            env=env,
            cwd=cwd,
            start_new_session=True,
        )
    except OSError as exc:
        return CommandResult(exit_code=-1, error=str(exc))

    stdout_chunks: list[bytes] = []
    stderr_tail: list[str] = []

    def _emit(text: str) -> None:
        stderr_tail.append(text)
        if len(stderr_tail) > 200:
            del stderr_tail[:100]
        if on_line:
            on_line(text)

    def _reader(stream: Any, is_stdout_capture: bool) -> None:
        buffer = ""
        while True:
            chunk = stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)
            if not chunk:
                break
            if is_stdout_capture:
                stdout_chunks.append(chunk)
                continue
            buffer += chunk.decode("utf-8", errors="replace")
            lines, buffer = _split_lines(buffer)
            for line in lines:
                if line.strip():
                    _emit(line)
        if buffer.strip():
            _emit(buffer)

    threads: list[threading.Thread] = []
    if capture_stdout:
        threads.append(threading.Thread(target=_reader, args=(proc.stdout, True), daemon=True))
        threads.append(threading.Thread(target=_reader, args=(proc.stderr, False), daemon=True))
    else:
        threads.append(threading.Thread(target=_reader, args=(proc.stdout, False), daemon=True))
    for thread in threads:
        thread.start()

    stopped = False
    user_cancel = False
    started = time.monotonic()
    interrupt_at: float | None = None
    while proc.poll() is None:
        now = time.monotonic()
        if stop_event is not None and stop_event.is_set() and interrupt_at is None:
            stopped = True
            user_cancel = True
            interrupt_at = now
            _signal(proc, signal.SIGINT)
        elif timeout is not None and now - started > timeout and interrupt_at is None:
            stopped = True
            interrupt_at = now
            # Timeouts are hard limits — SIGTERM then quick SIGKILL (not borg cancel grace).
            _signal(proc, signal.SIGTERM)
        if interrupt_at is not None:
            grace = CANCEL_GRACE_SEC if user_cancel else TIMEOUT_KILL_GRACE_SEC
            if now - interrupt_at > grace:
                _signal(proc, signal.SIGKILL)
        time.sleep(0.2)
    for thread in threads:
        thread.join(timeout=5)
    code = int(proc.returncode)
    error = ""
    if timeout is not None and stopped and not (stop_event and stop_event.is_set()):
        error = f"Zeitüberschreitung nach {int(timeout)} s"
    return CommandResult(
        exit_code=code,
        stopped=stopped,
        error=error,
        stdout=b"".join(stdout_chunks).decode("utf-8", errors="replace"),
        stderr="\n".join(stderr_tail),
    )


def _signal(proc: subprocess.Popen[bytes], sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.send_signal(sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    if sig == signal.SIGKILL:
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass


def run_capture(cmd: list[str], env: dict[str, str], timeout: float = 120.0) -> CommandResult:
    return run_command(cmd, env, timeout=timeout, capture_stdout=True)


def describe_failure(result: CommandResult) -> str:
    if result.error:
        return result.error
    if result.stopped:
        return "abgebrochen"
    lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
    interesting = [line for line in lines if not line.startswith("terminating with")]
    hint = (interesting or lines or [""])[-1][:300]
    return f"Exit-Code {result.exit_code}" + (f": {hint}" if hint else "")
