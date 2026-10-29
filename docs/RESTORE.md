# Komplett-Wiederherstellung (Bare Metal)

Die GUI stellt einzelne Archive oder Pfade über den Systemdienst wieder her.
Eine komplette Neuinstallation eines Systems gehört auf ein Live-Medium — nicht
in die laufende Installation.

## Empfohlener Ablauf

1. Live-USB starten (Ubuntu/Debian Installer oder Rescue).
2. Netzwerk und Festplatten prüfen; Zielpartitionen anlegen und mounten
   (z. B. `/mnt` als Root der neuen Installation).
3. Borg und SSH-Client im Live-System installieren.
4. Repository erreichbar machen (SSH-Key / Passphrase / S3-Credentials).
5. Archiv auflisten und extrahieren:

```bash
export BORG_PASSPHRASE='…'
borg list user@host:./backup
mkdir -p /mnt
cd /mnt
borg extract user@host:./backup::ARCHIVNAME
```

6. Bootloader und fstab anpassen, chroot, `update-grub` / `grub-install`.
7. Neustart vom Festplattenlaufwerk.

## Skript im Paket

`/usr/lib/borg-backup-gui/restore.sh` ist ein fortgeschrittenes Hilfsskript
für erfahrene Administratoren. Es ersetzt keine sorgfältige Planung von
Partitionierung und Bootloader.

## Hinweis zu `/` als GUI-Ziel

Die GUI warnt, wenn das Ziel `/` ist. Das überschreibt Dateien des laufenden
Systems und ist nur in Notfällen sinnvoll. Für einen echten Neuaufbau immer
ein Live-System verwenden.
