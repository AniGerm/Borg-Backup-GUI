"""Mount helpers for USB / SMB / NFS backup destinations.

Used by the root service. Standard library only.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from borg_model import resolve_repo
from borg_sysconfig import MOUNTED_ACCOUNT_TYPES, SystemPaths, safe_name

MOUNT_MARKER = ".borg-backup-gui-mounted"
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class MountError(RuntimeError):
    """Destination volume/share could not be mounted."""


def mount_point_for_account(account: dict[str, Any], paths: SystemPaths) -> Path:
    return paths.mounts_dir / safe_name(str(account.get("id") or "acc"), "acc")


def absolute_repo_path(account: dict[str, Any], destination: dict[str, Any], paths: SystemPaths) -> str:
    """Full filesystem (or remote) repo path after mounts are ensured."""
    ptype = str(account.get("type") or "")
    if ptype in MOUNTED_ACCOUNT_TYPES:
        sub = resolve_repo(account, destination)
        return str(mount_point_for_account(account, paths) / sub)
    return resolve_repo(account, destination)


def needs_mount(account: dict[str, Any]) -> bool:
    return str(account.get("type") or "") in MOUNTED_ACCOUNT_TYPES


def _run(cmd: list[str], *, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env={"PATH": SAFE_PATH, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
    )


def _is_mounted(path: Path) -> bool:
    try:
        return path.is_dir() and os.path.ismount(path)
    except OSError:
        return False


def _write_marker(mountpoint: Path) -> None:
    try:
        (mountpoint / MOUNT_MARKER).write_text("1\n", encoding="utf-8")
    except OSError:
        pass


def _has_marker(mountpoint: Path) -> bool:
    return (mountpoint / MOUNT_MARKER).is_file()


def list_removable_volumes(*, lsblk_json: str | None = None) -> list[dict[str, str]]:
    """Return removable/USB filesystems suitable as backup destinations."""
    text = lsblk_json
    if text is None:
        try:
            result = _run(
                ["lsblk", "-J", "-b", "-o", "NAME,PATH,TYPE,FSTYPE,SIZE,MOUNTPOINT,LABEL,UUID,RM,HOTPLUG,TRAN"],
                timeout=8,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if result.returncode != 0 or not result.stdout.strip():
            return []
        text = result.stdout
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []

    volumes: list[dict[str, str]] = []
    seen: set[str] = set()

    def walk(nodes: list[dict[str, Any]], *, parent_usb: bool = False) -> None:
        for node in nodes:
            ntype = str(node.get("type") or "")
            tran = str(node.get("tran") or "").lower()
            rm = bool(node.get("rm")) or str(node.get("rm") or "") in {"1", "true", "True"}
            hotplug = bool(node.get("hotplug")) or str(node.get("hotplug") or "") in {"1", "true", "True"}
            is_usb = parent_usb or tran == "usb" or rm or hotplug
            fstype = str(node.get("fstype") or "").strip()
            uuid = str(node.get("uuid") or "").strip()
            label = str(node.get("label") or "").strip()
            mnt = str(node.get("mountpoint") or "").strip()
            size_raw = node.get("size")
            try:
                size_b = int(size_raw) if size_raw is not None else 0
                if size_b >= 1_000_000_000:
                    size = f"{size_b / 1_000_000_000:.1f}G"
                elif size_b >= 1_000_000:
                    size = f"{size_b / 1_000_000:.0f}M"
                else:
                    size = str(size_raw or "")
            except (TypeError, ValueError):
                size = str(size_raw or "")
            if is_usb and fstype and uuid and ntype in {"part", "crypt", "lvm", "disk"}:
                key = uuid
                if key not in seen:
                    seen.add(key)
                    volumes.append(
                        {
                            "uuid": uuid,
                            "label": label or str(node.get("name") or uuid[:8]),
                            "size": size,
                            "fstype": fstype,
                            "mountpoint": mnt,
                            "path": str(node.get("path") or ""),
                            "name": str(node.get("name") or ""),
                        }
                    )
            children = node.get("children") or []
            if isinstance(children, list):
                walk(children, parent_usb=is_usb)

    walk(list(payload.get("blockdevices") or []))
    volumes.sort(key=lambda item: (item.get("label") or "", item.get("uuid") or ""))
    return volumes


def write_smb_credentials(account: dict[str, Any], paths: SystemPaths) -> Path:
    paths.creds_dir.mkdir(parents=True, exist_ok=True)
    path = paths.creds_dir / f"{safe_name(str(account.get('id') or 'acc'), 'acc')}.cred"
    lines = [
        f"username={account.get('smb_username') or ''}",
        f"password={account.get('smb_password') or ''}",
    ]
    domain = str(account.get("smb_domain") or "").strip()
    if domain:
        lines.append(f"domain={domain}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def ensure_usb_mount(account: dict[str, Any], paths: SystemPaths) -> Path:
    mountpoint = mount_point_for_account(account, paths)
    if _is_mounted(mountpoint):
        return mountpoint
    uuid = str(account.get("volume_uuid") or "").strip()
    label = str(account.get("volume_label") or "").strip()
    source = ""
    if uuid:
        candidate = Path(f"/dev/disk/by-uuid/{uuid}")
        if candidate.exists():
            source = str(candidate)
        else:
            raise MountError(f"USB-Datenträger nicht angeschlossen (UUID {uuid}).")
    elif label:
        candidate = Path(f"/dev/disk/by-label/{label}")
        if candidate.exists():
            source = str(candidate)
        else:
            raise MountError(f"USB-Datenträger nicht angeschlossen (Label {label}).")
    else:
        raise MountError("USB-Account ohne UUID/Label.")
    mountpoint.mkdir(parents=True, exist_ok=True)
    options = str(account.get("mount_options") or "").strip()
    cmd = ["mount"]
    if options:
        cmd += ["-o", options]
    cmd += [source, str(mountpoint)]
    result = _run(cmd)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() or f"Exit {result.returncode}"
        raise MountError(f"USB konnte nicht eingehängt werden: {detail}")
    _write_marker(mountpoint)
    return mountpoint


def ensure_smb_mount(account: dict[str, Any], paths: SystemPaths) -> Path:
    mountpoint = mount_point_for_account(account, paths)
    if _is_mounted(mountpoint):
        return mountpoint
    server = str(account.get("smb_server") or "").strip()
    share = str(account.get("smb_share") or "").strip().strip("/")
    if not server or not share:
        raise MountError("SMB-Account unvollständig (Server/Freigabe).")
    creds = write_smb_credentials(account, paths)
    mountpoint.mkdir(parents=True, exist_ok=True)
    extra = str(account.get("mount_options") or "").strip()
    opts = f"credentials={creds},uid=0,gid=0,file_mode=0600,dir_mode=0700"
    if extra:
        opts = f"{opts},{extra}"
    source = f"//{server}/{share}"
    result = _run(["mount", "-t", "cifs", source, str(mountpoint), "-o", opts])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() or f"Exit {result.returncode}"
        raise MountError(f"SMB-Freigabe nicht erreichbar ({source}): {detail}")
    _write_marker(mountpoint)
    return mountpoint


def ensure_nfs_mount(account: dict[str, Any], paths: SystemPaths) -> Path:
    mountpoint = mount_point_for_account(account, paths)
    if _is_mounted(mountpoint):
        return mountpoint
    server = str(account.get("nfs_server") or "").strip()
    export = str(account.get("nfs_export") or "").strip()
    if not server or not export:
        raise MountError("NFS-Account unvollständig (Server/Export).")
    if not export.startswith("/"):
        export = "/" + export
    mountpoint.mkdir(parents=True, exist_ok=True)
    options = str(account.get("mount_options") or "").strip() or "soft,timeo=100,retrans=2"
    source = f"{server}:{export}"
    result = _run(["mount", "-t", "nfs", "-o", options, source, str(mountpoint)])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() or f"Exit {result.returncode}"
        raise MountError(f"NFS-Freigabe nicht erreichbar ({source}): {detail}")
    _write_marker(mountpoint)
    return mountpoint


def ensure_mount(account: dict[str, Any], paths: SystemPaths) -> Path | None:
    """Mount destination media if needed. Returns mountpoint or None for non-mounted types."""
    ptype = str(account.get("type") or "")
    if ptype == "usb":
        return ensure_usb_mount(account, paths)
    if ptype == "smb":
        return ensure_smb_mount(account, paths)
    if ptype == "nfs":
        return ensure_nfs_mount(account, paths)
    return None


def release_mount(account: dict[str, Any], paths: SystemPaths) -> None:
    """Unmount only mounts created by this service (marker present)."""
    if not needs_mount(account):
        return
    mountpoint = mount_point_for_account(account, paths)
    if not _is_mounted(mountpoint):
        return
    if not _has_marker(mountpoint):
        return
    result = _run(["umount", str(mountpoint)])
    if result.returncode != 0:
        _run(["umount", "-l", str(mountpoint)])
    try:
        (mountpoint / MOUNT_MARKER).unlink(missing_ok=True)
    except OSError:
        pass
