"""Unit tests for borg_model helpers (no live Borg required)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

from borg_model import (
    build_borg_env,
    compute_next_run,
    ensure_sources,
    find_schedule_overlaps,
    format_schedule_summary,
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


def test_compute_next_run_daily_and_manual() -> None:
    now = datetime(2026, 10, 29, 10, 0, 0)
    assert compute_next_run(schedule_type="manual", now=now) is None
    nxt = compute_next_run(schedule_type="daily", schedule_time="03:15", catchup_missed=False, now=now)
    assert nxt == datetime(2026, 10, 30, 3, 15, 0)
    assert format_schedule_summary({"schedule_type": "daily", "schedule_time": "03:15"}) == "täglich 03:15"


def test_compute_next_run_interval() -> None:
    now = datetime(2026, 10, 29, 12, 0, 0)
    last = now - timedelta(hours=1)
    nxt = compute_next_run(
        schedule_type="interval",
        schedule_interval=3,
        catchup_missed=False,
        last_success=last,
        now=now,
    )
    assert nxt == last + timedelta(hours=3)


def test_find_schedule_overlaps() -> None:
    now = datetime(2026, 10, 29, 3, 15, 0)
    overlaps = find_schedule_overlaps(
        [("A", now), ("B", now + timedelta(minutes=20)), ("C", now + timedelta(hours=3))],
        window_minutes=60,
    )
    assert overlaps == [("A", "B", 20)]
