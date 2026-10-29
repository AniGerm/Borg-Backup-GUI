<h1 align="center">Borg Backup GUI</h1>

<p align="center">
  <strong>System backups with Borg — verified, multi-target, auditable.</strong><br>
  <em>Full-disk Linux backups with Canary restore checks, multiple destinations, and PDF protocols.</em>
</p>

<p align="center">
  <img src="assets/borg-backup-gui.svg" width="128" height="128" alt="Borg Backup GUI Logo">
</p>

<p align="center">
  <a href="#deutsch">Deutsch</a> · <a href="#english">English</a>
</p>

---

<a id="deutsch"></a>

## Deutsch

### Warum diese App?

[Vorta](https://github.com/borgbase/vorta) und [Pika Backup](https://gitlab.gnome.org/World/pika-backup) sind hervorragend für persönliche Home-Ordner.  
**Borg Backup GUI** zielt auf **komplette Systemsicherungen**: Root-Zugriff, Canary-Restore-Test, mehrere Backup-Ziele, DSGVO-taugliche PDF-Protokolle.

| | Vorta / Pika | Borg Backup GUI |
|--|--------------|-----------------|
| Full-System (`/`) + root-Dienst | eingeschränkt / nein | ja (`borg-backup-guid`) |
| Canary Restore-Test | nein | ja |
| Multi-Ziel + Accounts | Profile | ja |
| PDF-Protokoll + Retention | nein | ja |

### Architektur (3.x)

Backups und Zeitpläne laufen im **root-Systemdienst** `borg-backup-guid` (systemd).  
Die GUI steuert ihn über einen Unix-Socket; Autorisierung über **polkit** (kein NOPASSWD-sudo).  
Systemconfig und Secrets: `/etc/borg-backup-gui/` — UI-Präferenzen: `~/.config/borg-backup-gui/`.

**Unterstützte Systeme:** Ubuntu 22.04+/Debian 12+, amd64.

### Installation

**Debian/Ubuntu (.deb)** — von [GitHub Releases](https://github.com/AniGerm/Borg-Backup-GUI/releases):

```bash
sudo apt install ./borg-backup-gui_*_amd64.deb
borg-backup-gui
```

Das Paket liefert App-Menü, Icon, **Autostart** (`--tray`), systemd-Unit und polkit-Policy.

**Von Git-Checkout auf .deb umsteigen** (eine Aktion):

```bash
cd /pfad/zu/Borg-Backup-GUI
./packaging/migrate-to-deb.sh
```

**Aus dem Quellcode (Entwicklung):**

```bash
git clone https://github.com/AniGerm/Borg-Backup-GUI.git
cd Borg-Backup-GUI
./start_gui.sh
```

Voraussetzungen: Linux, Python 3.10+, `borgbackup`, `python3-tk`, polkit (für den Dienst).

### Schnellstart

1. App starten → **First-Run-Assistent** (Ziel, Passphrase, Quellen).
2. Backups laufen im Dienst `borg-backup-guid` — Konfiguration fragt bei Bedarf nach dem Admin-Passwort.
3. Sicherung → Quellen prüfen → **Backup starten**; Zeitplan ohne angemeldete GUI.
4. Wiederherstellung-Tab oder [docs/RESTORE.md](docs/RESTORE.md) für Bare-Metal.

### Updates

Die App prüft optional GitHub Releases auf ein neueres `.deb`, prüft `SHA256SUMS` und  
installiert per `pkexec`. Updates während laufender Backups sind gesperrt. RC-Tags  
(`v3.0.0-rc1`) erscheinen nicht unter `releases/latest`.

### Sicherheit

- Secrets und SSH-Keys nur unter `/etc/borg-backup-gui/` (root `0600`) — siehe [SECURITY.md](SECURITY.md).
- Keine sudoers-/NOPASSWD-Regel; Legacy-Dateien werden beim Paket-Upgrade entfernt.

### Qualitätssicherung

```bash
.venv/bin/pip install -r requirements-dev.txt
./QA.sh
.venv/bin/pytest -q
```

### Lizenz

[GPL-3.0](LICENSE)

---

<a id="english"></a>

## English

### Why this app?

[Vorta](https://github.com/borgbase/vorta) and [Pika Backup](https://gitlab.gnome.org/World/pika-backup) focus on personal home data.  
**Borg Backup GUI** targets **full-system Linux backups**: root access, Canary restore verification, multi-destination jobs, and PDF audit reports.

### Architecture (3.x)

Backups run in the root systemd service `borg-backup-guid`. The GUI is an unprivileged
client (Unix socket + polkit). Secrets live under `/etc/borg-backup-gui/`.  
Supported: Ubuntu 22.04+/Debian 12+, amd64.

### Install

Download the `.deb` from [Releases](https://github.com/AniGerm/Borg-Backup-GUI/releases)
(app menu, tray autostart, systemd unit, polkit policy), or migrate:

```bash
./packaging/migrate-to-deb.sh
```

### Quick start

1. Launch → first-run wizard (destination, passphrase, sources).
2. Backups run in `borg-backup-guid` (polkit); no sudoers rule.
3. Use the Restore tab or [docs/RESTORE.md](docs/RESTORE.md) for bare-metal recovery.

### Security

Passphrases and SSH keys are root-only under `/etc/borg-backup-gui/` (`0600`). See [SECURITY.md](SECURITY.md).

### License

[GPL-3.0](LICENSE)
