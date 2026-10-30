# Changelog

## 3.0.0-rc4

- Backup-Ziele: USB (UUID), SMB/CIFS und NFS mit Auto-Mount durch den Systemdienst
- Konten-Dialog mit Datenträger-Auswahl; Credentials root-only unter `/etc/borg-backup-gui`

## 3.0.0-rc3

- Fix: GUI startete nicht (fehlende Methode „Repository initialisieren“)

## 3.0.0-rc2

- Update-Check: Ergebnis 6 h cachen, Auto-Check erst nach 2 min; bei HTTP 403 Rate-Limit-Backoff

## 3.0.0-rc1

- Root-Systemdienst `borg-backup-guid` (systemd) führt Backups und Zeitpläne aus
- GUI steuert den Dienst über Unix-Socket + polkit (kein NOPASSWD-sudo mehr)
- Systemconfig unter `/etc/borg-backup-gui` (Secrets root-only), Migration aus 2.x
- Wiederherstellung über den Dienst (Archivliste, Pfadfilter, Fortschritt)
- Deb ohne Runtime-pip; Tray via AppIndicator; Autostart im Tray-Modus
- Update prüft `SHA256SUMS` wenn im Release vorhanden

## 2.0.4

- Zeitplan pro Backup-Volumen (Profil/Job) mit Übersicht
- Warnung bei überschneidenden Zeitplänen (Performance)
- Scheduler startet das nächste fällige Volumen; Konflikte werden verschoben
- „Vorta-Feature“-Hinweis entfernt

## 2.0.3

- Release zum Testen des GitHub-Auto-Updates (inkl. Neustart-UX und Versions-Footer)

## 2.0.2

- Update: warte auf Installationsende, dann Neustart-Dialog
- Footer mit aktueller Versionsnummer auf dem Hauptfenster

## 2.0.1

- Clearer auto-update status when GitHub latest has no `.deb` asset
- Deb packaging: system autostart (`/etc/xdg/autostart`) + `migrate-to-deb.sh`
- Release pipeline test: GitHub Release with `.deb` for updater verification
- Repo cleanup: remove legacy archive, old plans, unused assets

## 2.0.0

### Product
- Auto-update via GitHub Releases (`.deb` + `pkexec apply-update.sh`, Inbox-style)
- Positioned as full-system Borg GUI (Canary, multi-destination, PDF protocols)
- GPL-3.0 LICENSE, SECURITY.md, CONTRIBUTING.md, GitHub issue templates
- First-run setup wizard (destination, passphrase, sources, privilege hint)
- Debian package via `packaging/build_deb.sh` and GitHub Actions release workflow
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
