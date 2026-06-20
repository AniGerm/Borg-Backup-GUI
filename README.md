<h1 align="center">🔄 Borg Backup GUI</h1>

<p align="center">
  <strong>Desktop-Anwendung für Borg-Backups</strong><br>
  <em>mit Unterstützung für SSH (Storage Box), S3 Object Storage und lokale Laufwerke</em>
</p>

<p align="center">
  <img src="assets/borg-backup-gui.svg" width="128" height="128" alt="Borg Backup GUI Logo">
</p>

---

🇩🇪 **Deutsch** • [🇬🇧 English](#english)

---

## 🇩🇪 Deutsche Version

### Überblick

**Borg Backup GUI** ist eine benutzerfreundliche Desktop-Oberfläche (Tkinter) für [BorgBackup](https://www.borgbackup.org/) – ein leistungsstarkes, deduplizierendes Backup-Programm.

Die Anwendung ermöglicht:

- **Automatische, verschlüsselte Backups** auf verschiedene Ziele
- **Mehrere Backup-Profile** mit unterschiedlichen Konfigurationen
- **S3 Object Storage** (z. B. Hetzner Object Storage, AWS S3, MinIO)
- **SSH / Storage Box** (Hetzner, beliebiger SSH-Server)
- **Lokale Laufwerke** (USB-Festplatten, zweite SSD/HDD)
- **Systemweite Backups** mit automatischer sudo-Erhöhung
- **Tray-Icon** mit Live-Status und Schnellzugriff
- **Zeitplanung** (täglich, stündlich, manuell)
- **Wartung** (Prune, Check, Compact)
- **Archiv-Verwaltung** mit Liste, Mount/Unmount, Löschen
- **Wiederherstellung** einzelner Archive

### Systemvoraussetzungen

| Komponente | Anforderung |
|------------|-------------|
| Betriebssystem | Linux (getestet auf Ubuntu 24.04+) |
| Python | 3.10+ |
| BorgBackup | `apt install borgbackup` |
| Tkinter | `apt install python3-tk` |
| Tray-Icon (optional) | `pip3 install pystray pillow` |

### Installation

```bash
# 1. Repository klonen
git clone https://github.com/AniGerm/Borg-Backup-GUI.git
cd Borg-Backup-GUI

# 2. Starten (erstellt automatisch venv + installiert Abhängigkeiten)
./start_gui.sh

# Oder manuell:
python3 borg_backup_gui.py
```

Beim ersten Start führt die App durch die Einrichtung:
1. **Sudo-NOPASSWD-Regel** für automatische Backups
2. **Desktop-Launcher** fürs App-Menü und Autostart

### Erste Schritte

1. **App starten**: `./start_gui.sh`
2. **Profil anlegen**: Dashboard → "➕ Neues Profil" → Typ wählen
3. **SSH-Profil**: Repository-URL, SSH-Key, Passphrase
4. **S3-Profil**: Bucket-Name, Access Key, Secret Key, Endpoint
5. **Lokales Profil**: Ordner-Pfad (z. B. `/mnt/backup/borg-repo`)
6. **Backup starten**: Backup-Tab → Profil auswählen → "Backup jetzt ausführen"

### Backup-Strategie (Dual-Backup)

Für maximale Sicherheit: **Zwei Profile mit gleichem Zeitplan**

```yaml
Profil 1: "Hetzner Cloud" (Typ: SSH)
  Zeitplan: täglich 03:00

Profil 2: "Lokale Platte" (Typ: local)
  Zeitplan: täglich 03:00
  Pfad: /mnt/usb-backup/borg-repo
```

Beide laufen automatisch nacheinander.

### Projektstruktur

```
borg_backup_gui.py           # Haupt-App (ca. 2900 Zeilen)
borg-backup-gui.desktop      # Desktop-Launcher
start_gui.sh                 # Starter-Skript
install_desktop_launcher.sh  # Launcher-Installation
install_nopasswd_rule.sh     # Sudo-NOPASSWD-Einrichtung
restore.sh                   # CLI-Restore für Disaster Recovery
assets/
  borg-backup-gui.svg         # App-Icon
plans/
  plan.md                    # Technischer Entwicklungsplan
```

### Lizenz

MIT License – siehe [LICENSE](LICENSE).

---

## English

### Overview

**Borg Backup GUI** is a user-friendly desktop interface (Tkinter) for [BorgBackup](https://www.borgbackup.org/) – a powerful, deduplicating backup tool.

The application provides:

- **Automatic, encrypted backups** to multiple destinations
- **Multiple backup profiles** with different configurations
- **S3 Object Storage** (Hetzner Object Storage, AWS S3, MinIO, etc.)
- **SSH / Storage Box** (Hetzner, any SSH server)
- **Local drives** (USB hard drives, secondary SSD/HDD)
- **System-wide backups** with automatic privilege elevation
- **System tray icon** with live status and quick actions
- **Scheduling** (daily, interval, manual)
- **Maintenance** (prune, check, compact)
- **Archive management** with list, mount/unmount, delete
- **Restore** individual archives

### Requirements

| Component | Requirement |
|-----------|-------------|
| OS | Linux (tested on Ubuntu 24.04+) |
| Python | 3.10+ |
| BorgBackup | `apt install borgbackup` |
| Tkinter | `apt install python3-tk` |
| Tray icon (optional) | `pip3 install pystray pillow` |

### Installation

```bash
# 1. Clone repository
git clone https://github.com/AniGerm/Borg-Backup-GUI.git
cd Borg-Backup-GUI

# 2. Start (auto-creates venv + installs dependencies)
./start_gui.sh

# Or manually:
python3 borg_backup_gui.py
```

On first launch, the app guides you through setup:
1. **sudo NOPASSWD rule** for automatic backups
2. **Desktop launcher** for app menu and autostart

### Quick Start

1. **Launch**: `./start_gui.sh`
2. **Create profile**: Dashboard → "New Profile" → choose type
3. **SSH profile**: Repository URL, SSH key, passphrase
4. **S3 profile**: Bucket name, access key, secret key, endpoint
5. **Local profile**: Folder path (e.g., `/mnt/backup/borg-repo`)
6. **Start backup**: Backup tab → select profile → "Run backup now"

### Dual Backup Strategy

For maximum safety: **Two profiles with the same schedule**

```yaml
Profile 1: "Cloud Backup" (Type: SSH)
  Schedule: daily at 03:00

Profile 2: "Local Drive" (Type: local)
  Schedule: daily at 03:00
  Path: /mnt/usb-backup/borg-repo
```

Both run automatically, one after the other.

### Project Structure

```
borg_backup_gui.py           # Main app (~2900 lines)
borg-backup-gui.desktop      # Desktop launcher
start_gui.sh                 # Launch script
install_desktop_launcher.sh  # Launcher installation
install_nopasswd_rule.sh     # Sudo NOPASSWD setup
restore.sh                   # CLI restore for disaster recovery
assets/
  borg-backup-gui.svg         # App icon
plans/
  plan.md                    # Technical development plan
```

### License

MIT License – see [LICENSE](LICENSE).
