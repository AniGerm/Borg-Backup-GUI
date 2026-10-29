from __future__ import annotations

import os
import stat
from pathlib import Path

from borg_sysconfig import (
    SECRET_MASK,
    SystemPaths,
    assign_legacy_ssh_ports,
    config_fingerprint,
    extract_system_config,
    has_plain_secrets,
    import_ssh_keys,
    load_system_config,
    mask_secrets,
    merge_legacy_status,
    restore_masked_secrets,
    save_system_config,
    strip_user_secrets,
    validate_system_config,
)
from tests.conftest import make_config


def test_extract_normalizes_and_drops_unknown_keys() -> None:
    raw = make_config()
    raw["profiles"] = [{"name": "old"}]
    raw["borg_passphrase"] = "flat"
    raw["jobs"][0]["unknown"] = 1
    cfg = extract_system_config(raw)
    assert "profiles" not in cfg and "borg_passphrase" not in cfg
    job = cfg["jobs"][0]
    assert "unknown" not in job
    assert job["include_folders"] == ["/"]
    assert job["exclude_folders"] == ["/proc", "/sys"]
    assert cfg["schema_version"] == 3
    assert cfg["accounts"][1]["ssh_port"] == 23


def test_extract_legacy_flat_includes() -> None:
    raw = make_config()
    raw["jobs"][0].pop("sources")
    raw["jobs"][0]["include_folders"] = ["/home", "/etc"]
    raw["jobs"][0]["exclude_folders"] = ["*.tmp"]
    job = extract_system_config(raw)["jobs"][0]
    assert job["include_folders"] == ["/home", "/etc"]
    assert job["exclude_folders"] == ["*.tmp"]


def test_validate_accepts_sample() -> None:
    assert validate_system_config(extract_system_config(make_config(dests=2))) == []


def test_validate_rejects_bad_values() -> None:
    raw = make_config(compression="evil; rm -rf /", schedule_time="25:99")
    raw["jobs"][0]["sources"] = [{"path": "relative/path"}]
    raw["destinations"][0]["storage"] = "--remote-path=evil"
    raw["accounts"][0]["ssh_key"] = "relative_key"
    errors = validate_system_config(extract_system_config(raw))
    text = "\n".join(errors)
    assert "Kompression" in text
    assert "Uhrzeit" in text
    assert "absoluter Pfad" in text
    assert "ungültiges Repository" in text
    assert "SSH-Key" in text


def test_validate_rejects_unknown_destination_and_duplicates() -> None:
    raw = make_config(destination_ids=["nope"])
    raw["jobs"].append(dict(raw["jobs"][0]))
    errors = validate_system_config(extract_system_config(raw))
    assert any("existiert nicht" in e for e in errors)
    assert any("doppelt" in e for e in errors)


def test_mask_and_restore_secrets() -> None:
    stored = extract_system_config(make_config(dests=2))
    masked = mask_secrets(stored)
    assert masked["accounts"][0]["passphrase"] == SECRET_MASK
    assert stored["accounts"][0]["passphrase"] == "geheim"

    masked["accounts"][1]["passphrase"] = "neu"
    restored = restore_masked_secrets(masked, stored)
    assert restored["accounts"][0]["passphrase"] == "geheim"
    assert restored["accounts"][1]["passphrase"] == "neu"


def test_restore_mask_for_unknown_account_is_empty() -> None:
    incoming = {"accounts": [{"id": "new", "passphrase": SECRET_MASK}]}
    assert restore_masked_secrets(incoming, {"accounts": []})["accounts"][0]["passphrase"] == ""


def test_fingerprint_stable_and_sensitive() -> None:
    cfg = extract_system_config(make_config())
    assert config_fingerprint(cfg) == config_fingerprint(extract_system_config(make_config()))
    cfg["jobs"][0]["schedule_time"] = "04:00"
    assert config_fingerprint(cfg) != config_fingerprint(extract_system_config(make_config()))


def test_save_and_load_roundtrip_with_mode(sys_paths: SystemPaths) -> None:
    cfg = extract_system_config(make_config())
    save_system_config(sys_paths, cfg)
    assert stat.S_IMODE(os.stat(sys_paths.config_file).st_mode) == 0o600
    assert load_system_config(sys_paths) == cfg


def test_load_missing_or_corrupt(sys_paths: SystemPaths) -> None:
    assert load_system_config(sys_paths)["jobs"] == []
    sys_paths.config_file.write_text("{broken", encoding="utf-8")
    assert load_system_config(sys_paths)["jobs"] == []


def test_import_ssh_keys_copies_into_keystore(sys_paths: SystemPaths, tmp_path: Path) -> None:
    key = tmp_path / "id_ed25519"
    key.write_text("PRIVATE", encoding="utf-8")
    cfg = extract_system_config(make_config())
    cfg["accounts"][0]["ssh_key"] = str(key)
    assert import_ssh_keys(cfg, sys_paths) == []
    stored = Path(cfg["accounts"][0]["ssh_key"])
    assert stored.parent == sys_paths.keys_dir
    assert stored.read_text(encoding="utf-8") == "PRIVATE"
    assert stat.S_IMODE(os.stat(stored).st_mode) == 0o600
    assert import_ssh_keys(cfg, sys_paths) == []


def test_import_ssh_keys_reports_missing(sys_paths: SystemPaths) -> None:
    cfg = extract_system_config(make_config())
    cfg["accounts"][0]["ssh_key"] = "/nonexistent/key"
    assert import_ssh_keys(cfg, sys_paths)


def test_strip_user_secrets_masks_everything() -> None:
    raw = make_config()
    raw["borg_passphrase"] = "flat"
    raw["s3_secret_key"] = ""
    raw["profiles"] = [{"name": "p", "passphrase": "x"}]
    clean = strip_user_secrets(raw)
    assert clean["borg_passphrase"] == SECRET_MASK
    assert clean["s3_secret_key"] == ""
    assert clean["accounts"][0]["passphrase"] == SECRET_MASK
    assert clean["profiles"][0]["passphrase"] == SECRET_MASK
    assert has_plain_secrets(raw)
    assert not has_plain_secrets(clean)


def test_assign_legacy_ssh_ports() -> None:
    raw = make_config()
    raw["accounts"].append({"id": "acc3", "type": "ssh", "name": "Extra"})
    raw["destinations"].append({"id": "d3", "account_id": "acc3", "storage": "x@host:repo"})
    raw["jobs"][0]["destination_ids"] = ["d1", "d3"]
    raw["accounts"][0].pop("ssh_port")
    assign_legacy_ssh_ports(raw)
    ports = {a["id"]: a.get("ssh_port") for a in raw["accounts"]}
    assert ports == {"acc1": 23, "acc2": None, "acc3": 22}


def test_merge_legacy_status() -> None:
    legacy = {
        "last_success_at": "2026-10-01T03:00:00",
        "last_check_at": "2026-09-01T03:00:00",
        "jobs": {"Daten": {"last_success_at": "2026-10-02T03:00:00"}},
    }
    status = merge_legacy_status({"jobs": {}}, legacy, ["System", "Daten"])
    assert status["jobs"]["Daten"]["last_success_at"] == "2026-10-02T03:00:00"
    assert "last_success_at" not in status["jobs"]["System"]
    assert status["jobs"]["System"]["last_check_at"] == "2026-09-01T03:00:00"
    single = merge_legacy_status({"jobs": {}}, legacy, ["System"])
    assert single["jobs"]["System"]["last_success_at"] == "2026-10-01T03:00:00"
