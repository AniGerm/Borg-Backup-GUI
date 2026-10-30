"""Unit tests for USB/SMB/NFS mount helpers (no real mounts)."""

from __future__ import annotations

import borg_mounts as mounts
from borg_sysconfig import extract_system_config, validate_system_config

LSBLK_SAMPLE = """
{
  "blockdevices": [
    {
      "name": "sda",
      "path": "/dev/sda",
      "type": "disk",
      "tran": "usb",
      "rm": true,
      "hotplug": true,
      "children": [
        {
          "name": "sda1",
          "path": "/dev/sda1",
          "type": "part",
          "fstype": "ext4",
          "size": 32000000000,
          "mountpoint": null,
          "label": "BACKUP",
          "uuid": "11111111-2222-3333-4444-555555555555",
          "rm": true,
          "hotplug": true
        }
      ]
    }
  ]
}
"""


def test_list_removable_volumes_parses_lsblk() -> None:
    vols = mounts.list_removable_volumes(lsblk_json=LSBLK_SAMPLE)
    assert len(vols) == 1
    assert vols[0]["uuid"].startswith("11111111")
    assert vols[0]["label"] == "BACKUP"
    assert "G" in vols[0]["size"]


def test_absolute_repo_path_for_usb(sys_paths) -> None:
    account = {"id": "acc-usb", "type": "usb", "volume_uuid": "aaaa"}
    dest = {"id": "d1", "local_path": "borg-repo", "storage": "borg-repo"}
    path = mounts.absolute_repo_path(account, dest, sys_paths)
    assert path.endswith("/mounts/acc-usb/borg-repo")


def test_write_smb_credentials(sys_paths) -> None:
    account = {
        "id": "acc-smb",
        "type": "smb",
        "smb_username": "max",
        "smb_password": "geheim",
        "smb_domain": "HOME",
    }
    path = mounts.write_smb_credentials(account, sys_paths)
    text = path.read_text(encoding="utf-8")
    assert "username=max" in text
    assert "password=geheim" in text
    assert "domain=HOME" in text
    assert (path.stat().st_mode & 0o777) == 0o600


def test_validate_usb_smb_nfs_accounts() -> None:
    raw = {
        "accounts": [
            {"id": "u1", "name": "Stick", "type": "usb"},
            {
                "id": "s1",
                "name": "NAS",
                "type": "smb",
                "smb_server": "nas.local",
                "smb_share": "backup",
                "smb_username": "max",
                "smb_password": "x",
            },
            {
                "id": "n1",
                "name": "NFS",
                "type": "nfs",
                "nfs_server": "nas.local",
                "nfs_export": "/export/borg",
            },
        ],
        "destinations": [
            {"id": "d1", "name": "USB", "account_id": "u1", "storage": "borg-repo", "local_path": "borg-repo"},
            {"id": "d2", "name": "SMB", "account_id": "s1", "storage": "borg-repo", "local_path": "borg-repo"},
            {"id": "d3", "name": "NFS", "account_id": "n1", "storage": "borg-repo", "local_path": "borg-repo"},
        ],
        "jobs": [{"name": "System", "destination_ids": ["d1"], "sources": [{"path": "/", "excludes": []}]}],
    }
    cfg = extract_system_config(raw)
    errors = "\n".join(validate_system_config(cfg))
    assert "UUID oder Label" in errors
    raw["accounts"][0]["volume_uuid"] = "abcd"
    cfg = extract_system_config(raw)
    assert validate_system_config(cfg) == []


def test_reject_absolute_subdir_on_mounted_types() -> None:
    raw = {
        "accounts": [{"id": "u1", "name": "Stick", "type": "usb", "volume_uuid": "abcd"}],
        "destinations": [
            {"id": "d1", "name": "USB", "account_id": "u1", "storage": "/mnt/bad", "local_path": "/mnt/bad"}
        ],
        "jobs": [{"name": "System", "destination_ids": ["d1"], "sources": [{"path": "/", "excludes": []}]}],
    }
    errors = "\n".join(validate_system_config(extract_system_config(raw)))
    assert "Unterordner" in errors
