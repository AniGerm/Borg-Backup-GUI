from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from borg_sysconfig import SystemPaths


@pytest.fixture
def sys_paths(tmp_path: Path) -> SystemPaths:
    paths = SystemPaths.under(tmp_path / "root")
    paths.ensure()
    return paths


def make_config(*, dests: int = 1, **job_overrides: Any) -> dict[str, Any]:
    accounts = [
        {
            "id": "acc1",
            "name": "Storage Box",
            "type": "ssh",
            "ssh_key": "/etc/borg-backup-gui/keys/acc1",
            "ssh_port": 23,
            "passphrase": "geheim",
        },
        {"id": "acc2", "name": "USB", "type": "local", "passphrase": "lokal"},
    ]
    destinations = [{"id": "d1", "name": "Hetzner", "account_id": "acc1", "storage": "u1@u1.example.de:./borg"}]
    if dests > 1:
        destinations.append({"id": "d2", "name": "USB-Platte", "account_id": "acc2", "local_path": "/mnt/usb/borg"})
    job: dict[str, Any] = {
        "name": "System",
        "sources": [{"path": "/", "label": "Root", "excludes": ["/proc", "/sys"]}],
        "compression": "zstd,3",
        "schedule_type": "daily",
        "schedule_time": "03:15",
        "destination_ids": [d["id"] for d in destinations],
        "prune_enabled": True,
        "validate_enabled": True,
        "optimize_enabled": True,
    }
    job.update(job_overrides)
    return {
        "accounts": accounts,
        "destinations": destinations,
        "jobs": [job],
        "canary_enabled": True,
        "repo_access_confirmed": False,
        "maintain_all_destinations": False,
    }
