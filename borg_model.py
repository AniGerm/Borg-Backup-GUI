"""Typed helpers for accounts, destinations, jobs, disks and PDF retention."""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

PSEUDO_FS = frozenset(
    {
        "proc",
        "sysfs",
        "devtmpfs",
        "devpts",
        "tmpfs",
        "cgroup",
        "cgroup2",
        "pstore",
        "bpf",
        "tracefs",
        "debugfs",
        "securityfs",
        "hugetlbfs",
        "mqueue",
        "fusectl",
        "configfs",
        "rpc_pipefs",
        "binfmt_misc",
        "autofs",
        "overlay",
        "squashfs",
    }
)


def new_id(prefix: str = "id") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def migrate_profiles_to_model(data: dict[str, Any]) -> dict[str, Any]:
    """Ensure accounts/destinations/jobs exist; migrate from legacy profiles once."""
    data.setdefault("accounts", [])
    data.setdefault("destinations", [])
    data.setdefault("jobs", [])
    data.setdefault("protocol_retention_enabled", True)
    data.setdefault("protocol_retention_value", 3)
    data.setdefault("protocol_retention_unit", "months")

    profiles = data.get("profiles") or []
    if data["accounts"] and data["destinations"] and data["jobs"]:
        return data

    if not profiles:
        # Flat config → one account/destination/job
        acc_id = new_id("acc")
        dest_id = new_id("dest")
        ptype = str(data.get("profile_type", "ssh"))
        account = {
            "id": acc_id,
            "name": str(data.get("profile_name", "Standard")),
            "type": ptype,
            "ssh_key": str(data.get("ssh_key", "")),
            "passphrase": str(data.get("borg_passphrase", "")),
            "s3_access_key": str(data.get("s3_access_key", "")),
            "s3_secret_key": str(data.get("s3_secret_key", "")),
            "s3_endpoint_url": str(data.get("s3_endpoint_url", "https://fsn1.your-objectstorage.com")),
            "s3_region": str(data.get("s3_region", "fsn1")),
        }
        storage = str(data.get("storage", "") or data.get("local_path", ""))
        destination = {
            "id": dest_id,
            "name": f"{account['name']} Repo",
            "account_id": acc_id,
            "storage": storage,
            "local_path": str(data.get("local_path", "")),
        }
        job = {
            "name": str(data.get("profile_name", "Standard")),
            "include_folders": list(data.get("include_folders") or ["/"]),
            "exclude_folders": list(data.get("exclude_folders") or []),
            "compression": str(data.get("compression", "lz4")),
            "schedule_type": str(data.get("schedule_type", "manual")),
            "schedule_interval": int(data.get("schedule_interval", 3) or 3),
            "schedule_time": str(data.get("schedule_time", "03:15")),
            "catchup_missed": bool(data.get("catchup_missed", True)),
            "prune_enabled": bool(data.get("prune_enabled", False)),
            "validate_enabled": bool(data.get("validate_enabled", True)),
            "validate_interval": int(data.get("validate_interval", 3) or 3),
            "optimize_enabled": bool(data.get("optimize_enabled", False)),
            "optimize_interval": int(data.get("optimize_interval", 3) or 3),
            "destination_ids": [dest_id],
        }
        data["accounts"] = [account]
        data["destinations"] = [destination]
        data["jobs"] = [job]
        data["profiles"] = [_job_to_legacy_profile(job, account, destination)]
        return data

    accounts: list[dict[str, Any]] = []
    destinations: list[dict[str, Any]] = []
    jobs: list[dict[str, Any]] = []
    for profile in profiles:
        acc_id = new_id("acc")
        dest_id = new_id("dest")
        ptype = str(profile.get("type", "ssh"))
        account = {
            "id": acc_id,
            "name": str(profile.get("name", "Profil")),
            "type": ptype,
            "ssh_key": str(profile.get("ssh_key", "")),
            "passphrase": str(profile.get("passphrase", "")),
            "s3_access_key": str(profile.get("s3_access_key", "")),
            "s3_secret_key": str(profile.get("s3_secret_key", "")),
            "s3_endpoint_url": str(profile.get("s3_endpoint_url", "")),
            "s3_region": str(profile.get("s3_region", "")),
        }
        destination = {
            "id": dest_id,
            "name": f"{account['name']} Repo",
            "account_id": acc_id,
            "storage": str(profile.get("storage", "") or profile.get("local_path", "")),
            "local_path": str(profile.get("local_path", "")),
        }
        job = {
            "name": str(profile.get("name", "Profil")),
            "include_folders": list(profile.get("include_folders") or ["/"]),
            "exclude_folders": list(profile.get("exclude_folders") or []),
            "compression": str(profile.get("compression", "lz4")),
            "schedule_type": str(profile.get("schedule_type", "manual")),
            "schedule_interval": int(profile.get("schedule_interval", 3) or 3),
            "schedule_time": str(profile.get("schedule_time", "03:15")),
            "catchup_missed": bool(profile.get("catchup_missed", True)),
            "prune_enabled": bool(profile.get("prune_enabled", False)),
            "validate_enabled": bool(profile.get("validate_enabled", True)),
            "validate_interval": int(profile.get("validate_interval", 3) or 3),
            "optimize_enabled": bool(profile.get("optimize_enabled", False)),
            "optimize_interval": int(profile.get("optimize_interval", 3) or 3),
            "destination_ids": [dest_id],
        }
        accounts.append(account)
        destinations.append(destination)
        jobs.append(job)

    data["accounts"] = accounts
    data["destinations"] = destinations
    data["jobs"] = jobs
    return data


def _job_to_legacy_profile(job: dict[str, Any], account: dict[str, Any], destination: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": job.get("name", "Standard"),
        "type": account.get("type", "ssh"),
        "storage": destination.get("storage", ""),
        "ssh_key": account.get("ssh_key", ""),
        "passphrase": account.get("passphrase", ""),
        "compression": job.get("compression", "lz4"),
        "include_folders": list(job.get("include_folders") or ["/"]),
        "exclude_folders": list(job.get("exclude_folders") or []),
        "sources": list(job.get("sources") or []),
        "schedule_type": job.get("schedule_type", "manual"),
        "schedule_interval": job.get("schedule_interval", 3),
        "schedule_time": job.get("schedule_time", "03:15"),
        "catchup_missed": job.get("catchup_missed", True),
        "prune_enabled": job.get("prune_enabled", False),
        "validate_enabled": job.get("validate_enabled", True),
        "validate_interval": job.get("validate_interval", 3),
        "optimize_enabled": job.get("optimize_enabled", False),
        "optimize_interval": job.get("optimize_interval", 3),
        "s3_access_key": account.get("s3_access_key", ""),
        "s3_secret_key": account.get("s3_secret_key", ""),
        "s3_endpoint_url": account.get("s3_endpoint_url", ""),
        "s3_region": account.get("s3_region", ""),
        "local_path": destination.get("local_path", ""),
        "destination_ids": list(job.get("destination_ids") or []),
    }


def sync_profiles_mirror(data: dict[str, Any]) -> None:
    """Rebuild legacy profiles[] from jobs for backward compatibility."""
    accounts = {a["id"]: a for a in data.get("accounts", []) if a.get("id")}
    destinations = {d["id"]: d for d in data.get("destinations", []) if d.get("id")}
    mirrored: list[dict[str, Any]] = []
    for job in data.get("jobs", []):
        dest_ids = list(job.get("destination_ids") or [])
        if not dest_ids:
            continue
        dest = destinations.get(dest_ids[0], {})
        acc = accounts.get(str(dest.get("account_id", "")), {})
        mirrored.append(_job_to_legacy_profile(job, acc, dest))
    if mirrored:
        data["profiles"] = mirrored


def get_job(data: dict[str, Any], name: str) -> dict[str, Any] | None:
    for job in data.get("jobs", []):
        if isinstance(job, dict) and job.get("name") == name:
            return cast(dict[str, Any], job)
    return None


def get_account(data: dict[str, Any], account_id: str) -> dict[str, Any] | None:
    for account in data.get("accounts", []):
        if isinstance(account, dict) and account.get("id") == account_id:
            return cast(dict[str, Any], account)
    return None


def get_destination(data: dict[str, Any], dest_id: str) -> dict[str, Any] | None:
    for dest in data.get("destinations", []):
        if isinstance(dest, dict) and dest.get("id") == dest_id:
            return cast(dict[str, Any], dest)
    return None


def destinations_for_job(data: dict[str, Any], job: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for dest_id in job.get("destination_ids") or []:
        dest = get_destination(data, str(dest_id))
        if dest:
            result.append(dest)
    return result


def account_usage_count(data: dict[str, Any], account_id: str) -> int:
    return sum(1 for d in data.get("destinations", []) if d.get("account_id") == account_id)


def account_summary(account: dict[str, Any]) -> str:
    ptype = account.get("type", "ssh")
    if ptype == "ssh":
        return account.get("ssh_key", "") or "SSH"
    if ptype == "s3":
        return str(account.get("s3_endpoint_url", "") or "S3")
    return "local"


def resolve_repo(account: dict[str, Any], destination: dict[str, Any]) -> str:
    ptype = account.get("type", "ssh")
    if ptype == "local":
        return str(destination.get("local_path") or destination.get("storage") or "")
    if ptype == "s3":
        bucket = str(destination.get("storage", "")).strip()
        if bucket.startswith("s3:"):
            return bucket
        return f"s3:{bucket}"
    return str(destination.get("storage", ""))


def build_borg_env(
    account: dict[str, Any],
    destination: dict[str, Any],
    *,
    trust_repo_access: bool = False,
) -> dict[str, str]:
    """Build Borg process environment for an account/destination pair.

    Repo trust flags are only set when the user confirmed them (first-run / init).
    """
    env = os.environ.copy()
    passphrase = str(account.get("passphrase", "") or "")
    if passphrase:
        env["BORG_PASSPHRASE"] = passphrase
    if trust_repo_access:
        env["BORG_UNKNOWN_UNENCRYPTED_REPO_ACCESS_IS_OK"] = "yes"
        env["BORG_RELOCATED_REPO_ACCESS_IS_OK"] = "yes"
    ptype = account.get("type", "ssh")
    if ptype == "s3":
        env["AWS_ACCESS_KEY_ID"] = str(account.get("s3_access_key", ""))
        env["AWS_SECRET_ACCESS_KEY"] = str(account.get("s3_secret_key", ""))
        endpoint = str(account.get("s3_endpoint_url", "")).strip()
        if endpoint:
            env["AWS_ENDPOINT_URL"] = endpoint
        region = str(account.get("s3_region", "")).strip()
        if region:
            env["AWS_DEFAULT_REGION"] = region
    elif ptype == "ssh":
        key = str(account.get("ssh_key", "")).strip()
        if key:
            # Quote path for spaces; still a single argv token for ssh -i.
            env["BORG_RSH"] = f'ssh -i "{key}" -o BatchMode=yes -o StrictHostKeyChecking=accept-new'
    _ = destination  # reserved for future per-dest env
    return env


def list_backup_mounts() -> list[dict[str, str]]:
    """Discover real block/RAID/LVM mounts suitable as backup sources."""
    mounts: list[dict[str, str]] = []
    seen: set[str] = set()

    # Prefer lsblk JSON when available
    try:
        result = subprocess.run(
            ["lsblk", "-J", "-o", "NAME,PATH,TYPE,FSTYPE,SIZE,MOUNTPOINT,LABEL,UUID"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            payload = json.loads(result.stdout)

            def walk(nodes: list[dict[str, Any]], parent_type: str = "") -> None:
                for node in nodes:
                    mnt = str(node.get("mountpoint") or "").strip()
                    ntype = str(node.get("type") or parent_type)
                    if mnt and mnt not in seen and mnt != "[SWAP]":
                        if mnt == "/" or mnt.startswith(("/boot", "/snap")):
                            pass  # still allow / as optional source
                        label = str(node.get("label") or node.get("name") or mnt)
                        size = str(node.get("size") or "")
                        seen.add(mnt)
                        mounts.append(
                            {
                                "path": mnt,
                                "label": label,
                                "size": size,
                                "type": ntype,
                                "fstype": str(node.get("fstype") or ""),
                            }
                        )
                    children = node.get("children") or []
                    if isinstance(children, list):
                        walk(children, ntype)

            walk(list(payload.get("blockdevices") or []))
    except (OSError, json.JSONDecodeError, subprocess.SubprocessError):
        pass

    # Fallback / supplement from /proc/mounts
    try:
        with open("/proc/mounts", encoding="utf-8") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) < 3:
                    continue
                src, mnt, fstype = parts[0], parts[1], parts[2]
                if fstype in PSEUDO_FS:
                    continue
                if mnt in seen:
                    continue
                if not (mnt.startswith(("/mnt", "/media", "/data", "/srv")) or mnt == "/"):
                    continue
                if src.startswith("/dev/") or "md" in src or "mapper" in src:
                    seen.add(mnt)
                    mounts.append(
                        {
                            "path": mnt,
                            "label": os.path.basename(mnt) or mnt,
                            "size": "",
                            "type": "mount",
                            "fstype": fstype,
                        }
                    )
    except OSError:
        pass

    mounts.sort(key=lambda item: item["path"])
    return mounts


def filter_available_includes(paths: list[str]) -> tuple[list[str], list[str]]:
    """Return (available, missing) include paths."""
    available: list[str] = []
    missing: list[str] = []
    for path in paths:
        if os.path.exists(path):
            available.append(path)
        else:
            missing.append(path)
    return available, missing


DEFAULT_ROOT_EXCLUDES: list[str] = [
    "/dev",
    "/proc",
    "/sys",
    "/tmp",
    "/run",
    "/mnt",
    "/media",
    "/lost+found",
    "/var/cache/apt/archives",
    "/var/tmp",
    "/home/*/.cache",
    "/home/*/.local/share/Trash",
    "/home/*/snap",
    "/home/*/.vagrant.d",
]


def mount_lookup() -> dict[str, dict[str, str]]:
    return {m["path"]: m for m in list_backup_mounts()}


def suggest_excludes_for_path(path: str) -> list[str]:
    """Auto-Vorschläge für Exclude-Pfade relativ zu einem Include-Laufwerk."""
    path = path.rstrip("/") or "/"
    if path == "/":
        return list(DEFAULT_ROOT_EXCLUDES)

    suggestions = [
        f"{path}/lost+found",
        f"{path}/tmp",
        f"{path}/temp",
        f"{path}/.Trash",
        f"{path}/.Trash-1000",
        f"{path}/**/.cache",
        f"{path}/**/node_modules",
        f"{path}/**/__pycache__",
        f"{path}/**/.venv",
        f"{path}/**/venv",
    ]
    # Nur existierende Pfade + sinnvolle Globs behalten
    kept: list[str] = []
    for item in suggestions:
        if "*" in item or os.path.exists(item):
            kept.append(item)
    return kept


def enrich_source(path: str, excludes: list[str] | None = None) -> dict[str, Any]:
    info = mount_lookup().get(path, {})
    label = str(info.get("label") or (os.path.basename(path) if path != "/" else "System"))
    if path == "/" and not info.get("label"):
        label = "System Root"
    return {
        "path": path,
        "label": label,
        "size": str(info.get("size") or ""),
        "type": str(info.get("type") or ("disk" if path == "/" else "mount")),
        "fstype": str(info.get("fstype") or ""),
        "excludes": list(excludes or []),
    }


def sources_from_legacy(include_folders: list[str], exclude_folders: list[str]) -> list[dict[str, Any]]:
    """Migrate flat include/exclude lists into per-drive sources."""
    includes = [p for p in include_folders if str(p).strip()] or ["/"]
    excludes = [e for e in exclude_folders if str(e).strip()]
    sources: list[dict[str, Any]] = []
    if len(includes) == 1:
        sources.append(enrich_source(includes[0], excludes))
        return sources

    # Mehrere Includes: Excludes dem passenden Prefix zuordnen, Rest an Root/erstes Laufwerk
    assigned: dict[str, list[str]] = {p: [] for p in includes}
    leftovers: list[str] = []
    for exc in excludes:
        matched = None
        for inc in sorted(includes, key=len, reverse=True):
            if inc == "/":
                continue
            if exc == inc or exc.startswith(inc.rstrip("/") + "/") or exc.startswith(inc + "/**"):
                matched = inc
                break
        if matched:
            assigned[matched].append(exc)
        else:
            leftovers.append(exc)
    rootish = "/" if "/" in assigned else includes[0]
    assigned[rootish].extend(leftovers)
    for path in includes:
        sources.append(enrich_source(path, assigned.get(path, [])))
    return sources


def ensure_sources(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Ensure config has sources[]; migrate from include/exclude folders if needed."""
    sources = data.get("sources")
    if isinstance(sources, list) and sources:
        normalized: list[dict[str, Any]] = []
        for item in sources:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip()
            if not path:
                continue
            base = enrich_source(path, list(item.get("excludes") or []))
            if item.get("label"):
                base["label"] = str(item["label"])
            normalized.append(base)
        if normalized:
            data["sources"] = normalized
            sync_sources_to_flat(data)
            return normalized

    includes = list(data.get("include_folders") or ["/"])
    excludes = list(data.get("exclude_folders") or [])
    normalized = sources_from_legacy(includes, excludes)
    data["sources"] = normalized
    sync_sources_to_flat(data)
    return normalized


def sync_sources_to_flat(data: dict[str, Any]) -> None:
    """Derive include_folders / exclude_folders from sources for Borg CLI."""
    sources = data.get("sources") or []
    includes: list[str] = []
    excludes: list[str] = []
    seen_ex: set[str] = set()
    for src in sources:
        if not isinstance(src, dict):
            continue
        path = str(src.get("path") or "").strip()
        if path and path not in includes:
            includes.append(path)
        for exc in src.get("excludes") or []:
            exc_s = str(exc).strip()
            if exc_s and exc_s not in seen_ex:
                seen_ex.add(exc_s)
                excludes.append(exc_s)
    data["include_folders"] = includes or ["/"]
    data["exclude_folders"] = excludes


def flatten_source_excludes(sources: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    includes: list[str] = []
    excludes: list[str] = []
    seen: set[str] = set()
    for src in sources:
        path = str(src.get("path") or "").strip()
        if path:
            includes.append(path)
        for exc in src.get("excludes") or []:
            exc_s = str(exc).strip()
            if exc_s and exc_s not in seen:
                seen.add(exc_s)
                excludes.append(exc_s)
    return includes or ["/"], excludes


def retention_cutoff(value: int, unit: str, now: datetime | None = None) -> datetime:
    now = now or datetime.now()
    unit = (unit or "months").lower()
    if unit in ("day", "days", "tage", "tag"):
        return now - timedelta(days=max(0, value))
    if unit in ("year", "years", "jahre", "jahr"):
        return now - timedelta(days=max(0, value) * 365)
    # months default
    return now - timedelta(days=max(0, value) * 30)


PROTOCOL_SUBDIR = "protokolle"


def protocol_dir(config_dir: Path) -> Path:
    """Dedicated folder for protocol PDFs (not mixed with config files)."""
    path = Path(config_dir) / PROTOCOL_SUBDIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def migrate_protocol_pdfs_into_subdir(config_dir: Path) -> list[str]:
    """Move legacy backup-protokoll_*.pdf from config root into protokolle/."""
    root = Path(config_dir)
    dest = protocol_dir(root)
    moved: list[str] = []
    for path in sorted(root.glob("backup-protokoll_*.pdf")):
        target = dest / path.name
        try:
            if target.exists():
                # Keep both: rename with suffix rather than overwrite
                stem, suffix = path.stem, path.suffix
                n = 1
                while True:
                    candidate = dest / f"{stem}_migrated{n}{suffix}"
                    if not candidate.exists():
                        target = candidate
                        break
                    n += 1
            path.replace(target)
            moved.append(target.name)
        except OSError:
            continue
    return moved


def list_protocol_pdfs(config_dir: Path) -> list[dict[str, Any]]:
    migrate_protocol_pdfs_into_subdir(config_dir)
    files: list[dict[str, Any]] = []
    for path in sorted(protocol_dir(config_dir).glob("backup-protokoll_*.pdf"), reverse=True):
        try:
            stat = path.stat()
            files.append(
                {
                    "path": str(path),
                    "name": path.name,
                    "mtime": datetime.fromtimestamp(stat.st_mtime),
                    "size": stat.st_size,
                }
            )
        except OSError:
            continue
    return files


def list_expired_protocol_pdfs(
    config_dir: Path,
    enabled: bool,
    value: int,
    unit: str,
) -> list[dict[str, Any]]:
    """Return protocol PDFs older than the retention cutoff (no deletion)."""
    if not enabled:
        return []
    cutoff = retention_cutoff(int(value or 0), unit)
    expired: list[dict[str, Any]] = []
    for item in list_protocol_pdfs(config_dir):
        mtime = item["mtime"]
        if isinstance(mtime, datetime) and mtime < cutoff:
            expired.append(item)
    return expired


def purge_old_protocol_pdfs(
    config_dir: Path,
    enabled: bool,
    value: int,
    unit: str,
) -> list[str]:
    deleted: list[str] = []
    for item in list_expired_protocol_pdfs(config_dir, enabled, value, unit):
        try:
            Path(str(item["path"])).unlink(missing_ok=True)
            deleted.append(str(item["name"]))
        except OSError:
            continue
    return deleted


def format_size(num: int) -> str:
    size = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"
