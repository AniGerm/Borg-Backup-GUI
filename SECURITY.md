# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 3.x     | Yes |
| 2.x     | Migration to 3.x only |
| 1.x     | Unsupported |

## Architecture (3.x)

Backups run in the root systemd service `borg-backup-guid`. The desktop GUI is an
unprivileged client. It talks to `/run/borg-backup-gui/control.sock` and every
method is authorized with polkit (`org.borgbackupgui.*`).

- **status / run** (active session): allowed without password by default
- **configure / restore**: `auth_admin_keep` (administrator password)

There is **no** NOPASSWD sudoers rule for `borg`. Legacy files
`/etc/sudoers.d/99-borg-backup-gui` are removed on package upgrade.

## What is stored where

| Data | Location | Mode |
|------|----------|------|
| Accounts, destinations, jobs, passphrases, S3 secrets | `/etc/borg-backup-gui/config.json` | `0600` root |
| SSH private keys (copied on import) | `/etc/borg-backup-gui/keys/` | `0600` root |
| known_hosts | `/etc/borg-backup-gui/known_hosts` | root |
| Runtime status / logs / Borg cache | `/var/lib/borg-backup-gui/` | `0700` root |
| UI preferences (tray, window) | `~/.config/borg-backup-gui/` | user `0600` |

Treat `/etc/borg-backup-gui` like a password store. Do not commit or share it.

## Updates

`.deb` packages from GitHub Releases are verified against a `SHA256SUMS` asset
when present. Prefer HTTPS and official release tags. Prerelease tags
(`v3.0.0-rc1`) are not selected by `releases/latest`.

## Reporting a vulnerability

Please open a **private** security advisory on GitHub. Do not file public issues
for credential leaks or privilege-escalation paths.
