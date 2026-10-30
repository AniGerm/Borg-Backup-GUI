"""GitHub Releases auto-update (Inbox / DigitizeMyBook style).

Public repo: check ``releases/latest`` for ``borg-backup-gui_*.deb``, download to
cache, install via ``pkexec`` + ``apply-update.sh`` (or apt/dpkg fallback).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

GITHUB_REPO = "AniGerm/Borg-Backup-GUI"
DEB_NAME_RE = re.compile(r"^borg-backup-gui_.*\.deb$", re.IGNORECASE)
APPLY_UPDATE = Path("/usr/lib/borg-backup-gui/apply-update.sh")
LATEST_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
USER_AGENT = "BorgBackupGUI-Updater/3 (+https://github.com/AniGerm/Borg-Backup-GUI)"
# Unauthenticated GitHub API is ~60 req/h/IP — keep network checks rare.
MIN_CHECK_INTERVAL_SEC = 6 * 3600
RATE_LIMIT_BACKOFF_SEC = 3600
_CACHE_STATE_KEYS = (
    "phase",
    "available",
    "latest_version",
    "deb_name",
    "deb_url",
    "deb_size",
    "sha256_url",
    "error",
    "checked_at",
    "rate_limited_until",
)

_lock = threading.Lock()
_state: dict[str, Any] = {
    "phase": "idle",
    "current_version": None,
    "latest_version": None,
    "available": False,
    "deb_name": None,
    "deb_url": None,
    "deb_size": 0,
    "download_path": None,
    "download_percent": 0,
    "sha256_url": None,
    "error": None,
    "checked_at": None,
    "rate_limited_until": None,
    "repo": GITHUB_REPO,
}


@dataclass(frozen=True)
class DebReleaseInfo:
    version: str
    deb_url: str
    deb_name: str
    size: int
    sha256_url: str | None = None
    sha256_expected: str | None = None


def app_version(fallback: str = "0.0.0") -> str:
    """Read version without importing the Tk GUI module (avoids circular import)."""
    for path in (
        Path("/usr/lib/borg-backup-gui/VERSION"),
        Path(__file__).resolve().parent / "VERSION",
        Path(__file__).resolve().parent / "borg_backup_gui.py",
    ):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if path.name == "borg_backup_gui.py":
            match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M)
            if match:
                return match.group(1)
            continue
        stripped = text.strip()
        if stripped:
            return stripped.splitlines()[0].strip()
    return fallback


def is_version_newer(remote: str, local: str) -> bool:
    def parse(value: str) -> list[int]:
        parts = re.sub(r"^[vV]", "", value.strip()).split(".")
        out: list[int] = []
        for part in parts:
            match = re.match(r"^(\d+)", part)
            out.append(int(match.group(1)) if match else 0)
        return out

    left, right = parse(remote), parse(local)
    width = max(len(left), len(right))
    left += [0] * (width - len(left))
    right += [0] * (width - len(right))
    return left > right


def release_to_deb_info(release: dict[str, Any]) -> DebReleaseInfo | None:
    asset = None
    sha_asset = None
    for item in release.get("assets") or []:
        name = str(item.get("name") or "")
        if DEB_NAME_RE.match(name):
            asset = item
        elif name.upper() in {"SHA256SUMS", "SHA256SUMS.TXT"} or name.lower().endswith(".sha256"):
            sha_asset = item
    if not asset:
        return None
    url = str(asset.get("browser_download_url") or "")
    if not url:
        return None
    tag = str(release.get("tag_name") or "").lstrip("vV")
    return DebReleaseInfo(
        version=tag,
        deb_url=url,
        deb_name=str(asset.get("name") or ""),
        size=int(asset.get("size") or 0),
        sha256_url=str(sha_asset.get("browser_download_url") or "") if sha_asset else None,
    )


def _cache_dir() -> Path:
    path = Path.home() / ".cache" / "borg-backup-gui" / "updates"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _check_cache_path() -> Path:
    return _cache_dir() / "last-check.json"


def _load_check_cache() -> dict[str, Any]:
    path = _check_cache_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_check_cache(snapshot: dict[str, Any]) -> None:
    payload = {key: snapshot.get(key) for key in _CACHE_STATE_KEYS}
    path = _check_cache_path()
    try:
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("Update-Cache nicht speicherbar: %s", exc)


def _rate_limit_until_from_error(exc: BaseException) -> float:
    reset_at = 0.0
    headers = getattr(exc, "headers", None)
    if headers is not None:
        raw = headers.get("X-RateLimit-Reset") or headers.get("x-ratelimit-reset")
        if raw:
            try:
                reset_at = float(raw)
            except ValueError:
                reset_at = 0.0
    now = time.time()
    if reset_at > now:
        return reset_at
    return now + RATE_LIMIT_BACKOFF_SEC


def _is_rate_limit_error(exc: BaseException) -> bool:
    code = getattr(exc, "code", None)
    if code == 403:
        return True
    text = str(exc).lower()
    return "rate limit" in text or "api rate limit" in text


def fetch_latest_release_payload() -> dict[str, Any]:
    request = urllib.request.Request(
        LATEST_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unerwartete GitHub-Antwort: {type(payload)}")
    return payload


def fetch_latest_deb_release() -> DebReleaseInfo | None:
    return release_to_deb_info(fetch_latest_release_payload())


def _set_state(**kwargs: Any) -> dict[str, Any]:
    with _lock:
        _state.update(kwargs)
        _state["current_version"] = app_version()
        _state["repo"] = GITHUB_REPO
        return dict(_state)


def get_status() -> dict[str, Any]:
    with _lock:
        snap = dict(_state)
    snap["current_version"] = app_version()
    snap["repo"] = GITHUB_REPO
    return snap


def _apply_cached_result(cache: dict[str, Any], *, cur: str, note: str | None = None) -> dict[str, Any]:
    latest = str(cache.get("latest_version") or "")
    available = False
    phase = str(cache.get("phase") or "up-to-date")
    deb_name = cache.get("deb_name")
    deb_url = cache.get("deb_url")
    deb_size = int(cache.get("deb_size") or 0)
    sha256_url = cache.get("sha256_url")
    error = cache.get("error")
    if latest and phase in {"update-available", "up-to-date"} and not error:
        available = is_version_newer(latest, cur)
        phase = "update-available" if available else "up-to-date"
        if not available:
            deb_name = deb_url = sha256_url = None
            deb_size = 0
    if note and phase == "error":
        error = note
    elif note and not available:
        # Keep success phases but surface that we reused cache.
        pass
    snap = _set_state(
        phase=phase,
        available=available,
        latest_version=latest or None,
        current_version=cur,
        deb_name=deb_name if available else None,
        deb_url=deb_url if available else None,
        deb_size=deb_size if available else 0,
        sha256_url=sha256_url if available else None,
        download_path=None,
        download_percent=0,
        error=error if phase == "error" else None,
        checked_at=cache.get("checked_at"),
        rate_limited_until=cache.get("rate_limited_until"),
    )
    if note and phase != "error":
        snap = _set_state(error=None)
    return snap


def check_for_update(current: str | None = None, *, force: bool = False) -> dict[str, Any]:
    """Query GitHub for a newer .deb. Cached for :data:`MIN_CHECK_INTERVAL_SEC`.

    ``force=True`` (manual button) bypasses the normal interval but still
    respects an active rate-limit backoff.
    """
    cur = (current or app_version()).strip()
    now = time.time()
    cache = _load_check_cache()
    limited_until = float(cache.get("rate_limited_until") or 0)
    if limited_until > now:
        mins = max(1, int((limited_until - now) / 60))
        msg = f"GitHub Rate-Limit — nächster Check in ca. {mins} Min."
        return _set_state(
            phase="error",
            error=msg,
            available=False,
            latest_version=cache.get("latest_version"),
            checked_at=cache.get("checked_at"),
            rate_limited_until=limited_until,
            download_percent=0,
        )
    checked_at = float(cache.get("checked_at") or 0)
    if (
        not force
        and checked_at
        and now - checked_at < MIN_CHECK_INTERVAL_SEC
        and cache.get("phase") in {"up-to-date", "update-available", "error"}
    ):
        return _apply_cached_result(cache, cur=cur)

    _set_state(phase="checking", error=None, download_percent=0, rate_limited_until=None)
    try:
        payload = fetch_latest_release_payload()
        latest = release_to_deb_info(payload)
        tag = str(payload.get("tag_name") or "").lstrip("vV") or "?"
        if not latest:
            asset_names = [str(a.get("name") or "") for a in (payload.get("assets") or [])]
            detail = (
                f"GitHub latest={tag} ohne borg-backup-gui_*.deb"
                + (f" (Assets: {', '.join(asset_names)})" if asset_names else " (0 Assets)")
                + f" — lokal {cur}"
            )
            snap = _set_state(
                phase="error",
                error=detail,
                available=False,
                latest_version=tag,
                checked_at=now,
                rate_limited_until=None,
            )
            _save_check_cache(snap)
            return snap
        newer = is_version_newer(latest.version, cur)
        snap = _set_state(
            phase="update-available" if newer else "up-to-date",
            available=newer,
            latest_version=latest.version,
            current_version=cur,
            deb_name=latest.deb_name if newer else None,
            deb_url=latest.deb_url if newer else None,
            deb_size=latest.size if newer else 0,
            sha256_url=latest.sha256_url if newer else None,
            download_path=None,
            download_percent=0,
            error=None,
            checked_at=now,
            rate_limited_until=None,
        )
        _save_check_cache(snap)
        return snap
    except urllib.error.HTTPError as exc:
        logger.warning("Update-Check HTTP-Fehler: %s", exc)
        if _is_rate_limit_error(exc):
            until = _rate_limit_until_from_error(exc)
            mins = max(1, int((until - time.time()) / 60))
            snap = _set_state(
                phase="error",
                error=f"GitHub Rate-Limit (HTTP {exc.code}) — nächster Check in ca. {mins} Min.",
                available=False,
                checked_at=now,
                rate_limited_until=until,
            )
            merged = {**cache, **{k: snap.get(k) for k in _CACHE_STATE_KEYS}}
            _save_check_cache(merged)
            return snap
        snap = _set_state(
            phase="error",
            error=f"HTTP {exc.code}: {exc.reason}",
            available=False,
            checked_at=now,
            rate_limited_until=None,
        )
        _save_check_cache(snap)
        return snap
    except urllib.error.URLError as exc:
        logger.warning("Update-Check fehlgeschlagen: %s", exc)
        snap = _set_state(phase="error", error=str(exc), available=False, checked_at=now, rate_limited_until=None)
        _save_check_cache(snap)
        return snap
    except Exception as exc:
        logger.warning("Update-Check fehlgeschlagen: %s", exc)
        snap = _set_state(phase="error", error=str(exc), available=False, checked_at=now, rate_limited_until=None)
        _save_check_cache(snap)
        return snap


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _expected_sha256_from_sums(text: str, deb_name: str) -> str | None:
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[-1].endswith(deb_name):
            candidate = parts[0].lower()
            if re.fullmatch(r"[0-9a-f]{64}", candidate):
                return candidate
    return None


def verify_deb_sha256(deb_path: Path, *, sha256_url: str | None, deb_name: str) -> str | None:
    """Return an error message if verification fails; None on success / no sums published."""
    if not sha256_url:
        logger.warning("Kein SHA256SUMS-Asset — Update ohne Hash-Prüfung")
        return None
    try:
        request = urllib.request.Request(sha256_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=60) as response:
            sums = response.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return f"SHA256SUMS konnte nicht geladen werden: {exc}"
    expected = _expected_sha256_from_sums(sums, deb_name)
    if not expected:
        return f"SHA256SUMS enthält keinen Eintrag für {deb_name}"
    actual = _file_sha256(deb_path)
    if actual != expected:
        return f"SHA256-Mismatch: erwartet {expected}, erhalten {actual}"
    return None


def download_update(progress_cb: Any | None = None) -> dict[str, Any]:
    status = get_status()
    if not status.get("available") or not status.get("deb_url") or not status.get("deb_name"):
        return _set_state(phase="error", error="Kein Update zum Herunterladen")
    deb_name = str(status["deb_name"])
    deb_url = str(status["deb_url"])
    sha256_url = status.get("sha256_url")
    dest = _cache_dir() / deb_name
    _set_state(phase="downloading", download_percent=0, error=None, download_path=None)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        request = urllib.request.Request(deb_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=180) as response:
            total = int(response.headers.get("Content-Length") or status.get("deb_size") or 0)
            received = 0
            with tmp.open("wb") as handle:
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    handle.write(chunk)
                    received += len(chunk)
                    percent = int(received * 100 / total) if total else 0
                    percent = min(100, percent)
                    _set_state(download_percent=percent)
                    if progress_cb:
                        progress_cb(percent)
        tmp.replace(dest)
        verify_error = verify_deb_sha256(dest, sha256_url=str(sha256_url) if sha256_url else None, deb_name=deb_name)
        if verify_error:
            dest.unlink(missing_ok=True)
            return _set_state(phase="error", error=verify_error, download_path=None)
        return _set_state(phase="downloaded", download_path=str(dest), download_percent=100, error=None)
    except Exception as exc:
        logger.warning("Update-Download fehlgeschlagen: %s", exc)
        tmp.unlink(missing_ok=True)
        return _set_state(phase="error", error=str(exc), download_path=None)


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def install_update() -> dict[str, Any]:
    status = get_status()
    path = status.get("download_path")
    if not path or not Path(str(path)).is_file():
        return _set_state(phase="error", error="Update noch nicht heruntergeladen")
    deb_path = str(path)
    if not DEB_NAME_RE.match(Path(deb_path).name):
        return _set_state(phase="error", error="Ungültiger Paketname")
    _set_state(phase="installing", error=None)
    log_path = _cache_dir() / "install-update.log"
    helper = APPLY_UPDATE if APPLY_UPDATE.is_file() else None
    local_helper = Path(__file__).resolve().parent / "packaging" / "apply-update.sh"
    if helper is None and local_helper.is_file():
        helper = local_helper

    exit_path = _cache_dir() / "install.exit"
    try:
        exit_path.unlink(missing_ok=True)
    except OSError:
        pass

    if helper is not None:
        install_cmd = f"pkexec {_shell_quote(str(helper))} {_shell_quote(deb_path)}"
    else:
        install_cmd = (
            f"export DEBIAN_FRONTEND=noninteractive; "
            f"pkexec apt-get install -y --reinstall {_shell_quote(deb_path)} "
            f"|| pkexec dpkg -i {_shell_quote(deb_path)}"
        )

    script = f"""#!/bin/bash
trap '' HUP
exec >>{_shell_quote(str(log_path))} 2>&1
echo "==== $(date -Is) borg-backup-gui update ===="
set +e
{install_cmd}
rc=$?
echo "INSTALL_EXIT=$rc"
echo "$rc" > {_shell_quote(str(exit_path))}
exit $rc
"""  # noqa: S608
    script_path = _cache_dir() / "run-install.sh"
    script_path.write_text(script, encoding="utf-8")
    script_path.chmod(0o755)
    try:
        subprocess.Popen(
            ["setsid", "bash", str(script_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=os.environ.copy(),
        )
    except FileNotFoundError:
        subprocess.Popen(
            ["bash", str(script_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=os.environ.copy(),
        )
    return _set_state(phase="installing", error=None, download_path=deb_path)


def install_exit_path() -> Path:
    return _cache_dir() / "install.exit"


def poll_install_finished(timeout_sec: float = 300.0, interval_sec: float = 0.5) -> dict[str, Any]:
    """Block until install.exit appears or timeout. Returns status snapshot."""
    deadline = time.time() + max(5.0, timeout_sec)
    path = install_exit_path()
    while time.time() < deadline:
        if path.is_file():
            try:
                code = int(path.read_text(encoding="utf-8").strip() or "1")
            except ValueError:
                code = 1
            if code == 0:
                return _set_state(phase="installed", error=None)
            return _set_state(phase="error", error=f"Installation fehlgeschlagen (Exit {code})")
        time.sleep(interval_sec)
    return _set_state(phase="error", error="Installation: Timeout (pkexec abgebrochen?)")


def download_and_install(progress_cb: Any | None = None) -> dict[str, Any]:
    """Convenience: download then kick off install."""
    st = download_update(progress_cb=progress_cb)
    if st.get("phase") != "downloaded":
        return st
    return install_update()
