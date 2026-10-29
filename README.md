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
| Full-System (`/`) + geführtes sudo | eingeschränkt / nein | ja |
| Canary Restore-Test | nein | ja |
| Multi-Ziel + Accounts | Profile | ja |
| PDF-Protokoll + Retention | nein | ja |

### Installation

**Debian/Ubuntu (.deb)** — von [GitHub Releases](https://github.com/AniGerm/Borg-Backup-GUI/releases):

```bash
sudo apt install ./borg-backup-gui_*_amd64.deb
borg-backup-gui
```

Das Paket liefert App-Menü, Icon und **Autostart** (`/etc/xdg/autostart/`).  
Config bleibt unter `~/.config/borg-backup-gui/`.

**Von Git-Checkout auf .deb umsteigen** (eine Aktion):

```bash
cd /pfad/zu/Borg-Backup-GUI
./packaging/migrate-to-deb.sh
```

Stoppt die Checkout-GUI, baut/installiert das Deb, entfernt alte User-Launcher, prüft Icon/Autostart und startet die System-App.

**Aus dem Quellcode (Entwicklung):**

```bash
git clone https://github.com/AniGerm/Borg-Backup-GUI.git
cd Borg-Backup-GUI
./start_gui.sh
```

Voraussetzungen: Linux, Python 3.10+, `borgbackup`, `python3-tk`.

### Schnellstart

1. App starten → **First-Run-Assistent** (Ziel, Passphrase, Quellen, sudo-Hinweis).
2. Optional: `install_nopasswd_rule.sh` für passwortfreie Systembackups (eng begrenzte sudoers-Regel).
3. Backup-Tab → Quellen prüfen → **Backup starten**.
4. Dashboard: Canary = OK; Protokoll-Tab für PDFs.

### Updates

Die App prüft optional GitHub Releases (`AniGerm/Borg-Backup-GUI`) auf ein neueres  
`borg-backup-gui_*.deb`, lädt es herunter und installiert es per `pkexec`  
(`apply-update.sh` — analog Inbox/LaborCheck). Schalter im Dashboard: **Software-Update**.

### Sicherheit

- Secrets liegen in `~/.config/borg-backup-gui/config.json` (Modus `0600`) — siehe [SECURITY.md](SECURITY.md).
- NOPASSWD-Setup ist **opt-in**, nie stillschweigend durch das Deb-Paket.

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

### Install

Download the `.deb` from [Releases](https://github.com/AniGerm/Borg-Backup-GUI/releases) (includes app menu, icon, and login autostart), or migrate from a git checkout:

```bash
./packaging/migrate-to-deb.sh
```

For development:

```bash
git clone https://github.com/AniGerm/Borg-Backup-GUI.git
cd Borg-Backup-GUI
./start_gui.sh
```

### Quick start

1. Launch → first-run wizard (destination, passphrase, sources, privilege hint).
2. Optionally run `install_nopasswd_rule.sh` for unattended system backups.
3. Start a backup; confirm Canary OK on the dashboard.

### Security

Passphrases and S3 keys are stored in plaintext under `~/.config/borg-backup-gui/` with file mode `0600`. See [SECURITY.md](SECURITY.md).

### License

[GPL-3.0](LICENSE)
