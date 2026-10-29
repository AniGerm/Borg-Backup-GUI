"""Unit tests for borg_model helpers (no live Borg required)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

from borg_model import (
    build_borg_env,
    ensure_sources,
    list_expired_protocol_pdfs,
    migrate_profiles_to_model,
    migrate_protocol_pdfs_into_subdir,
    protocol_dir,
    retention_cutoff,
    sources_from_legacy,
    suggest_excludes_for_path,
    sync_sources_to_flat,
)


def test_suggest_excludes_root_contains_proc() -> None:
    excludes = suggest_excludes_for_path("/")
    assert "/proc" in excludes
    assert "/dev" in excludes


def test_sources_from_legacy_assigns_excludes() -> None:
    sources = sources_from_legacy(
        ["/", "/mnt/data"],
        ["/proc", "/mnt/data/tmp", "/home/*/.cache"],
    )
    by_path = {s["path"]: s for s in sources}
    assert "/proc" in by_path["/"]["excludes"] or "/home/*/.cache" in by_path["/"]["excludes"]
    assert "/mnt/data/tmp" in by_path["/mnt/data"]["excludes"]


def test_ensure_sources_and_flat_sync() -> None:
    data: dict = {
        "include_folders": ["/"],
        "exclude_folders": ["/tmp"],
    }
    sources = ensure_sources(data)
    assert sources
    assert data["sources"][0]["path"] == "/"
    sync_sources_to_flat(data)
    assert "/" in data["include_folders"]
    assert "/tmp" in data["exclude_folders"]


def test_retention_cutoff_days() -> None:
    now = datetime(2026, 10, 29, 12, 0, 0)
    cut = retention_cutoff(7, "days", now=now)
    assert cut == now - timedelta(days=7)


def test_list_expired_protocol_pdfs(tmp_path: Path) -> None:
    pdir = protocol_dir(tmp_path)
    old = pdir / "backup-protokoll_old.pdf"
    new = pdir / "backup-protokoll_new.pdf"
    old.write_bytes(b"%PDF-1.4")
    new.write_bytes(b"%PDF-1.4")
    old_mtime = (datetime.now() - timedelta(days=40)).timestamp()
    os.utime(old, (old_mtime, old_mtime))
    expired = list_expired_protocol_pdfs(tmp_path, enabled=True, value=30, unit="days")
    names = {e["name"] for e in expired}
    assert "backup-protokoll_old.pdf" in names
    assert "backup-protokoll_new.pdf" not in names


def test_migrate_protocol_pdfs_into_subdir(tmp_path: Path) -> None:
    loose = tmp_path / "backup-protokoll_legacy.pdf"
    loose.write_bytes(b"%PDF")
    moved = migrate_protocol_pdfs_into_subdir(tmp_path)
    assert moved
    assert not loose.exists()
    assert (tmp_path / "protokolle" / "backup-protokoll_legacy.pdf").exists()


def test_migrate_profiles_to_model_creates_accounts() -> None:
    data = {
        "profile_name": "Standard",
        "profile_type": "ssh",
        "storage": "user@host:./backup",
        "borg_passphrase": "secret",
        "include_folders": ["/"],
        "exclude_folders": ["/tmp"],
    }
    out = migrate_profiles_to_model(data)
    assert out["accounts"]
    assert out["destinations"]
    assert out["jobs"]
    assert out["accounts"][0]["passphrase"] == "secret"


def test_build_borg_env_trust_flags() -> None:
    account = {"type": "ssh", "passphrase": "x", "ssh_key": "/tmp/key"}
    dest = {"storage": "u@h:./b"}
    env_off = build_borg_env(account, dest, trust_repo_access=False)
    assert "BORG_UNKNOWN_UNENCRYPTED_REPO_ACCESS_IS_OK" not in env_off
    env_on = build_borg_env(account, dest, trust_repo_access=True)
    assert env_on.get("BORG_UNKNOWN_UNENCRYPTED_REPO_ACCESS_IS_OK") == "yes"
    assert "BORG_PASSPHRASE" in env_on
