"""Integration smoke with a real local borg repository (no network)."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

import borg_engine as engine
from borg_sysconfig import SystemPaths, empty_system_config, save_system_config

BORG = shutil.which("borg")


@pytest.mark.skipif(not BORG, reason="borg binary not installed")
def test_engine_backup_and_extract_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paths = SystemPaths.under(root)
        paths.ensure()
        source = root / "src"
        source.mkdir()
        (source / "hello.txt").write_text("canary-data\n", encoding="utf-8")
        repo = root / "repo"
        passphrase = "test-passphrase-not-secret"
        env = {
            "PATH": engine.SAFE_PATH,
            "LANG": "C.UTF-8",
            "BORG_PASSPHRASE": passphrase,
            "BORG_UNKNOWN_UNENCRYPTED_REPO_ACCESS_IS_OK": "yes",
            "BORG_RELOCATED_REPO_ACCESS_IS_OK": "yes",
            "BORG_BASE_DIR": str(paths.borg_base_dir),
        }
        init = engine.run_capture([BORG or "borg", "init", "--encryption=repokey-blake2", str(repo)], env, timeout=120)
        assert init.ok, init.stderr

        cfg = empty_system_config()
        cfg["accounts"] = [
            {
                "id": "acc1",
                "name": "local",
                "type": "local",
                "ssh_key": "",
                "ssh_port": 22,
                "passphrase": passphrase,
                "s3_access_key": "",
                "s3_secret_key": "",
                "s3_endpoint_url": "",
                "s3_region": "",
            }
        ]
        cfg["destinations"] = [
            {
                "id": "d1",
                "name": "localrepo",
                "account_id": "acc1",
                "storage": str(repo),
                "local_path": str(repo),
            }
        ]
        cfg["jobs"] = [
            {
                "name": "Smoke",
                "sources": [{"path": str(source), "label": "src", "excludes": []}],
                "include_folders": [str(source)],
                "exclude_folders": [],
                "compression": "lz4",
                "schedule_type": "manual",
                "schedule_interval": 3,
                "schedule_time": "03:15",
                "catchup_missed": False,
                "prune_enabled": False,
                "validate_enabled": False,
                "validate_interval": 3,
                "optimize_enabled": False,
                "optimize_interval": 3,
                "destination_ids": ["d1"],
            }
        ]
        cfg["canary_enabled"] = False
        cfg["repo_access_confirmed"] = True
        save_system_config(paths, cfg)

        plan = engine.plan_backup(
            cfg,
            "Smoke",
            paths,
            hostname="testhost",
            job_status={},
            path_exists=lambda p: Path(p).exists(),
        )
        create = [t for t in plan.tasks if t.kind == "create"]
        assert len(create) == 1
        result = engine.run_command(create[0].cmd, create[0].env, timeout=180)
        assert result.ok, engine.describe_failure(result)

        target = engine.primary_target(cfg, "Smoke", paths)
        short = engine.run_capture([BORG or "borg", "list", "--short", target.repo], target.env, timeout=60)
        assert short.ok, short.stderr
        name = short.stdout.strip().splitlines()[-1]
        out = root / "out"
        out.mkdir()
        extract = engine.run_command(
            engine.extract_cmd(target.repo, name, []),
            target.env,
            cwd=str(out),
            timeout=120,
        )
        assert extract.ok, engine.describe_failure(extract)
        restored = list(out.rglob("hello.txt"))
        assert restored
        assert restored[0].read_text(encoding="utf-8") == "canary-data\n"
