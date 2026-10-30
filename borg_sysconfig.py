"""System configuration of the root backup service (``/etc/borg-backup-gui``).

Shared by the daemon (root) and the GUI (user). Standard library only: the
service runs with the distribution ``python3`` without a virtualenv.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 4
SECRET_MASK = "••••••••"
ACCOUNT_SECRET_FIELDS = ("passphrase", "s3_secret_key", "smb_password")
ACCOUNT_TYPES = ("ssh", "s3", "local", "usb", "smb", "nfs")
MOUNTED_ACCOUNT_TYPES = ("usb", "smb", "nfs")
SCHEDULE_TYPES = ("manual", "interval", "daily")
DEFAULT_SSH_PORT = 23

ACCOUNT_FIELDS = (
    "id",
    "name",
    "type",
    "ssh_key",
    "ssh_port",
    "passphrase",
    "s3_access_key",
    "s3_secret_key",
    "s3_endpoint_url",
    "s3_region",
    "volume_uuid",
    "volume_label",
    "smb_server",
    "smb_share",
    "smb_username",
    "smb_domain",
    "smb_password",
    "nfs_server",
    "nfs_export",
    "mount_options",
)
DESTINATION_FIELDS = ("id", "name", "account_id", "storage", "local_path")
GLOBAL_BOOL_FIELDS = {
    "canary_enabled": True,
    "repo_access_confirmed": False,
    "maintain_all_destinations": False,
}

_COMPRESSION_RE = re.compile(
    r"^(none|lz4|zstd(,\d{1,2})?|zlib(,\d)?|lzma(,\d)?|auto,(lz4|zstd(,\d{1,2})?|zlib(,\d)?|lzma(,\d)?))$"
)
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]")


@dataclass(frozen=True)
class SystemPaths:
    """Filesystem layout of the service. ``root`` prefixes all paths (tests / dev)."""

    etc_dir: Path
    state_dir: Path
    run_dir: Path

    @classmethod
    def default(cls) -> SystemPaths:
        prefix = os.environ.get("BORG_BACKUP_GUI_ROOT", "").rstrip("/")
        return (
            cls.under(prefix)
            if prefix
            else cls(
                etc_dir=Path("/etc/borg-backup-gui"),
                state_dir=Path("/var/lib/borg-backup-gui"),
                run_dir=Path("/run/borg-backup-gui"),
            )
        )

    @classmethod
    def under(cls, prefix: str | Path) -> SystemPaths:
        base = Path(prefix)
        return cls(
            etc_dir=base / "etc" / "borg-backup-gui",
            state_dir=base / "var" / "lib" / "borg-backup-gui",
            run_dir=base / "run" / "borg-backup-gui",
        )

    @property
    def config_file(self) -> Path:
        return self.etc_dir / "config.json"

    @property
    def keys_dir(self) -> Path:
        return self.etc_dir / "keys"

    @property
    def known_hosts(self) -> Path:
        return self.etc_dir / "known_hosts"

    @property
    def status_file(self) -> Path:
        return self.state_dir / "status.json"

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def borg_base_dir(self) -> Path:
        return self.state_dir / "borg"

    @property
    def canary_file(self) -> Path:
        return self.state_dir / "canary-check.txt"

    @property
    def socket_path(self) -> Path:
        return self.run_dir / "control.sock"

    @property
    def mount_dir(self) -> Path:
        return self.run_dir / "mount"

    @property
    def mounts_dir(self) -> Path:
        """Service-managed USB/SMB/NFS mount points for backup destinations."""
        return self.state_dir / "mounts"

    @property
    def creds_dir(self) -> Path:
        return self.etc_dir / "creds"

    def ensure(self) -> None:
        for path, mode in (
            (self.etc_dir, 0o700),
            (self.keys_dir, 0o700),
            (self.creds_dir, 0o700),
            (self.state_dir, 0o700),
            (self.logs_dir, 0o700),
            (self.borg_base_dir, 0o700),
            (self.mounts_dir, 0o700),
            (self.run_dir, 0o755),
            (self.mount_dir, 0o755),
        ):
            path.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(path, mode)
            except OSError:
                pass


def safe_name(value: str, fallback: str = "item") -> str:
    cleaned = _SAFE_ID_RE.sub("_", str(value or "").strip()).strip("._")
    return cleaned[:80] or fallback


def empty_system_config() -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "accounts": [],
        "destinations": [],
        "jobs": [],
    }
    cfg.update(GLOBAL_BOOL_FIELDS)
    return cfg


def _as_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return min(high, max(low, number))


def _normalize_account(raw: dict[str, Any]) -> dict[str, Any]:
    account: dict[str, Any] = {}
    for key in ACCOUNT_FIELDS:
        if key == "ssh_port":
            continue
        account[key] = str(raw.get(key) or "").strip() if key not in ACCOUNT_SECRET_FIELDS else str(raw.get(key) or "")
    account["type"] = account["type"] or "ssh"
    account["ssh_port"] = _as_int(raw.get("ssh_port"), DEFAULT_SSH_PORT, 1, 65535)
    return account


def _normalize_sources(raw_job: dict[str, Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    raw_sources = raw_job.get("sources")
    if isinstance(raw_sources, list) and raw_sources:
        for item in raw_sources:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip()
            if not path:
                continue
            excludes = [str(e).strip() for e in (item.get("excludes") or []) if str(e).strip()]
            sources.append({"path": path, "label": str(item.get("label") or path), "excludes": excludes})
        if sources:
            return sources
    includes = [str(p).strip() for p in (raw_job.get("include_folders") or []) if str(p).strip()] or ["/"]
    excludes = [str(e).strip() for e in (raw_job.get("exclude_folders") or []) if str(e).strip()]
    for index, path in enumerate(includes):
        sources.append({"path": path, "label": path, "excludes": excludes if index == 0 else []})
    return sources


def _normalize_job(raw: dict[str, Any]) -> dict[str, Any]:
    sources = _normalize_sources(raw)
    includes: list[str] = []
    excludes: list[str] = []
    for src in sources:
        if src["path"] not in includes:
            includes.append(src["path"])
        for exc in src["excludes"]:
            if exc not in excludes:
                excludes.append(exc)
    return {
        "name": str(raw.get("name") or "").strip(),
        "sources": sources,
        "include_folders": includes,
        "exclude_folders": excludes,
        "compression": str(raw.get("compression") or "lz4").strip(),
        "schedule_type": str(raw.get("schedule_type") or "manual").strip().lower(),
        "schedule_interval": _as_int(raw.get("schedule_interval"), 3, 1, 168),
        "schedule_time": str(raw.get("schedule_time") or "03:15").strip(),
        "catchup_missed": bool(raw.get("catchup_missed", True)),
        "prune_enabled": bool(raw.get("prune_enabled", False)),
        "validate_enabled": bool(raw.get("validate_enabled", True)),
        "validate_interval": _as_int(raw.get("validate_interval"), 3, 1, 52),
        "optimize_enabled": bool(raw.get("optimize_enabled", False)),
        "optimize_interval": _as_int(raw.get("optimize_interval"), 3, 1, 52),
        "destination_ids": [str(d) for d in (raw.get("destination_ids") or []) if str(d).strip()],
    }


def extract_system_config(data: dict[str, Any]) -> dict[str, Any]:
    """Normalized service config from a GUI/legacy config (unknown keys dropped)."""
    cfg = empty_system_config()
    cfg["accounts"] = [_normalize_account(a) for a in (data.get("accounts") or []) if isinstance(a, dict)]
    cfg["destinations"] = [
        {key: str(d.get(key) or "").strip() for key in DESTINATION_FIELDS}
        for d in (data.get("destinations") or [])
        if isinstance(d, dict)
    ]
    cfg["jobs"] = [_normalize_job(j) for j in (data.get("jobs") or []) if isinstance(j, dict)]
    for key, default in GLOBAL_BOOL_FIELDS.items():
        cfg[key] = bool(data.get(key, default))
    return cfg


def _has_control_chars(value: str) -> bool:
    return any(ch in value for ch in ("\n", "\r", "\x00"))


def validate_system_config(cfg: dict[str, Any]) -> list[str]:
    """Return human readable (German) validation errors; empty list means valid."""
    errors: list[str] = []
    account_ids: set[str] = set()
    for account in cfg.get("accounts") or []:
        acc_id = str(account.get("id") or "")
        label = account.get("name") or acc_id or "?"
        if not acc_id:
            errors.append("Account ohne ID.")
        elif acc_id in account_ids:
            errors.append(f"Account-ID doppelt: {acc_id}")
        account_ids.add(acc_id)
        acc_type = str(account.get("type") or "")
        if acc_type not in ACCOUNT_TYPES:
            errors.append(f"Account „{label}“: unbekannter Typ {account.get('type')!r}.")
        key = str(account.get("ssh_key") or "")
        if key and not key.startswith("/"):
            errors.append(f"Account „{label}“: SSH-Key muss ein absoluter Pfad sein.")
        if acc_type == "usb":
            if not str(account.get("volume_uuid") or "").strip() and not str(account.get("volume_label") or "").strip():
                errors.append(f"Account „{label}“: USB braucht UUID oder Label.")
        elif acc_type == "smb":
            if not str(account.get("smb_server") or "").strip() or not str(account.get("smb_share") or "").strip():
                errors.append(f"Account „{label}“: SMB braucht Server und Freigabe.")
            if not str(account.get("smb_username") or "").strip():
                errors.append(f"Account „{label}“: SMB braucht Benutzername.")
        elif acc_type == "nfs":
            if not str(account.get("nfs_server") or "").strip() or not str(account.get("nfs_export") or "").strip():
                errors.append(f"Account „{label}“: NFS braucht Server und Export-Pfad.")
        for field in ACCOUNT_FIELDS:
            value = account.get(field)
            if isinstance(value, str) and _has_control_chars(value) and field not in ACCOUNT_SECRET_FIELDS:
                errors.append(f"Account „{label}“: ungültige Zeichen in {field}.")

    accounts_by_id = {str(a.get("id") or ""): a for a in (cfg.get("accounts") or [])}
    dest_ids: set[str] = set()
    for dest in cfg.get("destinations") or []:
        dest_id = str(dest.get("id") or "")
        label = dest.get("name") or dest_id or "?"
        if not dest_id:
            errors.append("Ziel ohne ID.")
        elif dest_id in dest_ids:
            errors.append(f"Ziel-ID doppelt: {dest_id}")
        dest_ids.add(dest_id)
        account_id = str(dest.get("account_id") or "")
        if account_id not in account_ids:
            errors.append(f"Ziel „{label}“: Account fehlt.")
        repo = str(dest.get("storage") or dest.get("local_path") or "")
        if repo.startswith("-") or _has_control_chars(repo):
            errors.append(f"Ziel „{label}“: ungültiges Repository.")
        account = accounts_by_id.get(account_id) or {}
        if account.get("type") == "local" and repo and not repo.startswith("/"):
            errors.append(f"Ziel „{label}“: lokaler Pfad muss absolut sein.")
        if account.get("type") in MOUNTED_ACCOUNT_TYPES and (".." in repo or repo.startswith("/")):
            errors.append(f"Ziel „{label}“: Unterordner darf kein absoluter Pfad und kein '..' sein.")

    job_names: set[str] = set()
    for job in cfg.get("jobs") or []:
        name = str(job.get("name") or "")
        if not name:
            errors.append("Backup-Volumen ohne Namen.")
        elif name in job_names:
            errors.append(f"Backup-Volumen doppelt: {name}")
        if _has_control_chars(name):
            errors.append(f"Backup-Volumen „{name}“: ungültige Zeichen im Namen.")
        job_names.add(name)
        for dest_id in job.get("destination_ids") or []:
            if dest_id not in dest_ids:
                errors.append(f"Backup-Volumen „{name}“: Ziel {dest_id} existiert nicht.")
        for path in job.get("include_folders") or []:
            if not str(path).startswith("/") or _has_control_chars(str(path)):
                errors.append(f"Backup-Volumen „{name}“: Quelle {path!r} muss ein absoluter Pfad sein.")
        for pattern in job.get("exclude_folders") or []:
            if _has_control_chars(str(pattern)):
                errors.append(f"Backup-Volumen „{name}“: ungültiges Exclude-Muster.")
        if not _COMPRESSION_RE.match(str(job.get("compression") or "")):
            errors.append(f"Backup-Volumen „{name}“: Kompression {job.get('compression')!r} unbekannt.")
        if job.get("schedule_type") not in SCHEDULE_TYPES:
            errors.append(f"Backup-Volumen „{name}“: Zeitplan-Typ unbekannt.")
        if job.get("schedule_type") == "daily" and not _TIME_RE.match(str(job.get("schedule_time") or "")):
            errors.append(f"Backup-Volumen „{name}“: Uhrzeit muss HH:MM sein.")
    return errors


def mask_secrets(cfg: dict[str, Any]) -> dict[str, Any]:
    masked = copy.deepcopy(cfg)
    for account in masked.get("accounts") or []:
        for field in ACCOUNT_SECRET_FIELDS:
            if account.get(field):
                account[field] = SECRET_MASK
    return masked


def restore_masked_secrets(incoming: dict[str, Any], stored: dict[str, Any]) -> dict[str, Any]:
    """Replace mask placeholders with the stored secret of the same account id."""
    merged = copy.deepcopy(incoming)
    by_id = {str(a.get("id")): a for a in stored.get("accounts") or [] if isinstance(a, dict)}
    for account in merged.get("accounts") or []:
        previous = by_id.get(str(account.get("id")), {})
        for field in ACCOUNT_SECRET_FIELDS:
            if account.get(field) == SECRET_MASK:
                account[field] = str(previous.get(field) or "")
    return merged


def config_fingerprint(cfg: dict[str, Any]) -> str:
    payload = json.dumps(cfg, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def write_json_atomic(path: Path, payload: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def load_system_config(paths: SystemPaths) -> dict[str, Any]:
    try:
        with open(paths.config_file, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return empty_system_config()
    if not isinstance(data, dict):
        return empty_system_config()
    return extract_system_config(data)


def save_system_config(paths: SystemPaths, cfg: dict[str, Any]) -> None:
    write_json_atomic(paths.config_file, cfg, 0o600)


def import_ssh_keys(cfg: dict[str, Any], paths: SystemPaths) -> list[str]:
    """Copy SSH keys referenced outside ``keys/`` into the root-only key store.

    Returns errors for keys that cannot be read. Updates ``ssh_key`` in place.
    """
    errors: list[str] = []
    keys_dir = paths.keys_dir
    keys_dir.mkdir(parents=True, exist_ok=True)
    for account in cfg.get("accounts") or []:
        if account.get("type") != "ssh":
            continue
        key = str(account.get("ssh_key") or "").strip()
        if not key:
            continue
        source = Path(key)
        try:
            if source.resolve().parent == keys_dir.resolve():
                continue
        except OSError:
            pass
        if not source.is_file():
            errors.append(f"SSH-Key für „{account.get('name') or account.get('id')}“ nicht gefunden: {key}")
            continue
        target = keys_dir / safe_name(str(account.get("id") or "key"), "key")
        try:
            shutil.copyfile(source, target)
            os.chmod(target, 0o600)
        except OSError as exc:
            errors.append(f"SSH-Key {key} konnte nicht übernommen werden: {exc}")
            continue
        account["ssh_key"] = str(target)
    return errors


def prune_unused_keys(cfg: dict[str, Any], paths: SystemPaths) -> None:
    used = {str(a.get("ssh_key") or "") for a in cfg.get("accounts") or []}
    try:
        entries = list(paths.keys_dir.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_file() and str(entry) not in used:
            try:
                entry.unlink()
            except OSError:
                pass


def prune_unused_creds(cfg: dict[str, Any], paths: SystemPaths) -> None:
    used_ids = {
        safe_name(str(a.get("id") or ""), "acc")
        for a in (cfg.get("accounts") or [])
        if str(a.get("type") or "") == "smb"
    }
    try:
        entries = list(paths.creds_dir.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_file() and entry.stem not in used_ids:
            try:
                entry.unlink()
            except OSError:
                pass


def _mask_value(value: Any) -> str:
    return SECRET_MASK if value else ""


def assign_legacy_ssh_ports(data: dict[str, Any]) -> None:
    """2.x used ``ssh -p 23`` only for the first target of a volume; extra targets used ssh's default."""
    destinations = {str(d.get("id")): d for d in data.get("destinations") or [] if isinstance(d, dict)}
    primary_accounts: set[str] = set()
    for job in data.get("jobs") or []:
        dest_ids = [str(d) for d in (job.get("destination_ids") or [])] if isinstance(job, dict) else []
        if dest_ids and dest_ids[0] in destinations:
            primary_accounts.add(str(destinations[dest_ids[0]].get("account_id") or ""))
    for account in data.get("accounts") or []:
        if isinstance(account, dict) and account.get("type", "ssh") == "ssh" and not account.get("ssh_port"):
            account["ssh_port"] = DEFAULT_SSH_PORT if str(account.get("id")) in primary_accounts else 22


def strip_user_secrets(data: dict[str, Any]) -> dict[str, Any]:
    """Copy of a GUI config whose secrets are replaced by :data:`SECRET_MASK` (for ``~/.config``).

    The mask means "unchanged, keep what the service stores"; an empty value clears the secret.
    """
    clean = copy.deepcopy(data)
    for key in ("borg_passphrase", "s3_secret_key"):
        if key in clean:
            clean[key] = _mask_value(clean[key])
    for account in clean.get("accounts") or []:
        if isinstance(account, dict):
            for field in ACCOUNT_SECRET_FIELDS:
                account[field] = _mask_value(account.get(field))
    for profile in clean.get("profiles") or []:
        if isinstance(profile, dict):
            for field in ("passphrase", "s3_secret_key"):
                if field in profile:
                    profile[field] = _mask_value(profile[field])
    return clean


def has_plain_secrets(data: dict[str, Any]) -> bool:
    """True if any secret in ``data`` is a real value (not empty, not the mask)."""
    values = [data.get("borg_passphrase"), data.get("s3_secret_key")]
    for account in data.get("accounts") or []:
        if isinstance(account, dict):
            values += [account.get(f) for f in ACCOUNT_SECRET_FIELDS]
    return any(v and v != SECRET_MASK for v in values)


STATUS_JOB_KEYS = (
    "last_run_at",
    "last_success_at",
    "last_exit_code",
    "last_error",
    "last_duration_sec",
    "last_files_count",
    "last_source_size",
    "last_dedupe_size",
    "last_item",
    "last_dest_results",
    "last_check_at",
    "last_compact_at",
    "canary_last_result",
    "canary_last_check",
    "canary_expected_hash",
    "canary_verified_hash",
)


def merge_legacy_status(status: dict[str, Any], legacy: dict[str, Any], job_names: list[str]) -> dict[str, Any]:
    """Carry scheduling history from a 2.x user ``status.json`` into the service status."""
    jobs = status.setdefault("jobs", {})
    raw_jobs = legacy.get("jobs")
    legacy_jobs: dict[str, Any] = raw_jobs if isinstance(raw_jobs, dict) else {}
    for name in job_names:
        target = jobs.setdefault(name, {})
        raw_source = legacy_jobs.get(name)
        source: dict[str, Any] = raw_source if isinstance(raw_source, dict) else {}
        for key in ("last_success_at", "last_check_at", "last_compact_at"):
            value = source.get(key) or (legacy.get(key) if len(job_names) == 1 or key != "last_success_at" else "")
            if value and not target.get(key):
                target[key] = str(value)
    return status
