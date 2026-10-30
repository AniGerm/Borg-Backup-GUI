"""Tests for GitHub .deb updater helpers (no network)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import borg_updates as bu


def test_is_version_newer() -> None:
    assert bu.is_version_newer("2.0.1", "2.0.0")
    assert bu.is_version_newer("v2.1.0", "2.0.9")
    assert not bu.is_version_newer("2.0.0", "2.0.0")
    assert not bu.is_version_newer("1.9.9", "2.0.0")


def test_check_for_update_uses_cache(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(bu, "_cache_dir", lambda: tmp_path)
    cache = {
        "phase": "up-to-date",
        "available": False,
        "latest_version": "2.0.4",
        "deb_name": None,
        "deb_url": None,
        "deb_size": 0,
        "sha256_url": None,
        "error": None,
        "checked_at": time.time(),
        "rate_limited_until": None,
    }
    (tmp_path / "last-check.json").write_text(json.dumps(cache), encoding="utf-8")
    called = {"n": 0}

    def boom() -> dict:
        called["n"] += 1
        raise AssertionError("network should not be hit")

    monkeypatch.setattr(bu, "fetch_latest_release_payload", boom)
    status = bu.check_for_update(current="2.0.4", force=False)
    assert called["n"] == 0
    assert status["phase"] == "up-to-date"
    assert status["available"] is False


def test_check_for_update_respects_rate_limit_backoff(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(bu, "_cache_dir", lambda: tmp_path)
    until = time.time() + 1800
    cache = {
        "phase": "error",
        "available": False,
        "latest_version": "2.0.4",
        "error": "old",
        "checked_at": time.time() - 10,
        "rate_limited_until": until,
    }
    (tmp_path / "last-check.json").write_text(json.dumps(cache), encoding="utf-8")
    status = bu.check_for_update(current="2.0.4", force=True)
    assert status["phase"] == "error"
    assert "Rate-Limit" in str(status["error"])
    assert status["rate_limited_until"] == until


def test_release_to_deb_info_picks_asset() -> None:
    release = {
        "tag_name": "v2.0.1",
        "assets": [
            {"name": "notes.txt", "browser_download_url": "https://x/notes", "size": 1},
            {
                "name": "borg-backup-gui_2.0.1_amd64.deb",
                "browser_download_url": "https://x/pkg.deb",
                "size": 99,
            },
        ],
    }
    info = bu.release_to_deb_info(release)
    assert info is not None
    assert info.version == "2.0.1"
    assert info.deb_name == "borg-backup-gui_2.0.1_amd64.deb"
    assert info.size == 99


def test_release_to_deb_info_missing() -> None:
    assert bu.release_to_deb_info({"tag_name": "v1", "assets": []}) is None


def test_app_version_reads_gui_file() -> None:
    ver = bu.app_version()
    assert ver[0].isdigit()


def test_expected_sha256_from_sums() -> None:
    text = "aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899  borg-backup-gui_3.0.0_amd64.deb\n"
    # pad to 64 hex
    digest = "a" * 64
    text = f"{digest}  borg-backup-gui_3.0.0_amd64.deb\n"
    assert bu._expected_sha256_from_sums(text, "borg-backup-gui_3.0.0_amd64.deb") == digest
    assert bu._expected_sha256_from_sums(text, "other.deb") is None
