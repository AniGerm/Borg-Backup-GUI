# Changelog

## 2.0.1

- Clearer auto-update status when GitHub latest has no `.deb` asset
- Deb packaging: system autostart (`/etc/xdg/autostart`) + `migrate-to-deb.sh`
- Release pipeline test: GitHub Release with `.deb` for updater verification

## 2.0.0

### Product
- Auto-update via GitHub Releases (`.deb` + `pkexec apply-update.sh`, Inbox-style)
- Positioned as full-system Borg GUI (Canary, multi-destination, PDF protocols)
- GPL-3.0 LICENSE, SECURITY.md, CONTRIBUTING.md, GitHub issue templates
- First-run setup wizard (destination, passphrase, sources, privilege hint)
- Debian package via `packaging/build_deb.sh` and GitHub Actions release workflow
- Legacy/broken entry points moved to `archive/`

### Reliability & security
- Multi-destination create results aggregated (partial failures reported honestly)
- Optional maintenance (prune/check/compact) on all destinations
- `config.json` / `status.json` saved with mode `0600`
- Single-instance file lock (`fcntl`) in addition to Unix socket
- Repo trust flags only after user confirmation
- Protocol PDFs under `~/.config/borg-backup-gui/protokolle/`

### Quality
- Pytest suite for `borg_model` helpers
- QA.sh score gate includes pytest (threshold ≥ 95)

## 1.3.0

- Accounts / destinations / jobs model
- Multi-source disk picker and per-drive excludes
- PDF list + age-based retention
- RAM fix (no `borg create --list`, log cap)
