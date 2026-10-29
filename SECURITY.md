# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 2.x     | Yes       |
| 1.x     | Best-effort fixes only |

## What this app stores

Configuration lives in `~/.config/borg-backup-gui/config.json` and may contain:

- Borg repository passphrase
- S3 access key / secret
- SSH key **paths** (not the private key file itself)

The file is written with mode `0600`. Treat the config directory like a password store. Do not commit it or share it.

## Privilege model

Full-system backups need root file access. The optional script `install_nopasswd_rule.sh` installs a **narrow** sudoers rule: NOPASSWD only for the `borg` binary, with selected environment variables kept. This is opt-in and never installed silently by the `.deb` postinst.

Risks if the GUI account is compromised: an attacker can run `borg` as root with attacker-controlled env. Mitigate by locking the desktop session and keeping the machine updated.

## Reporting a vulnerability

Please open a **private** security advisory on GitHub or email the maintainer listed in the repository. Do not file public issues for credential leaks or RCE paths.
