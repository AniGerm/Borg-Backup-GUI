#!/bin/bash
set -euo pipefail

# === Farben für bessere Lesbarkeit ===
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

error() { echo -e "${RED}[FEHLER]${NC} $*" >&2; exit 1; }
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }

# === Root-Check ===
[ "$EUID" -ne 0 ] && error "Dieses Skript muss als root ausgeführt werden (sudo)."


# === Abhängigkeiten prüfen ===
command -v borg  >/dev/null || error "borgbackup ist nicht installiert. Installiere es mit: apt install borgbackup"
command -v rsync >/dev/null || error "rsync ist nicht installiert. Installiere es mit: apt install rsync"

echo "=== Borg Restore – Interaktiver Modus ==="
echo

# === Konfiguration ===
STORAGE="uXXXXXX@uXXXXXX.your-storagebox.de:23/./backup"
SSH_KEY_SOURCE=""  # Optional: Pfad zum SSH-Key (z.B. USB-Stick oder Live-System)

# ==============================================
# 1. Festplatten auswählen
# ==============================================
echo "=== 1. Verfügbare Festplatten ==="
lsblk -d -o NAME,SIZE,MODEL,TYPE | grep -v "loop\|rom"
echo

read -r -p "Root-Disk (z.B. nvme0n1 oder sda): " ROOTDISK
[ -b "/dev/$ROOTDISK" ] || error "/dev/$ROOTDISK existiert nicht."

echo
echo "Soll die EFI-Partition:"
echo "1) Auf derselben Festplatte liegen ($ROOTDISK)"
echo "2) Auf einer anderen Festplatte"
read -r -p "Auswahl (1/2): " EFIOPT

if [ "$EFIOPT" = "1" ]; then
    EFIDISK=$ROOTDISK
    SINGLE_DISK=true
else
    echo "=== Verfügbare Festplatten für EFI ==="
    lsblk -d -o NAME,SIZE,MODEL,TYPE | grep -v "loop\|rom"
    read -r -p "EFI-Disk (z.B. sda): " EFIDISK
    [ -b "/dev/$EFIDISK" ] || error "/dev/$EFIDISK existiert nicht."
    SINGLE_DISK=false
fi

# NVMe-Laufwerke haben "p" als Prefix für Partitionen (nvme0n1p1)
PART_PREFIX=""
if [[ "$ROOTDISK" == nvme* ]]; then
    PART_PREFIX="p"
fi
EFI_PART_PREFIX=""
if [[ "$EFIDISK" == nvme* ]]; then
    EFI_PART_PREFIX="p"
fi

ROOTPART="${ROOTDISK}${PART_PREFIX}1"
EFIPART="${EFIDISK}${EFI_PART_PREFIX}1"

echo
echo "=============================="
echo "Root-Disk:     $ROOTDISK"
echo "EFI-Disk:      $EFIDISK"
echo "Single-Disk:   $SINGLE_DISK"
echo "=============================="
read -r -p "Bestätigen? ALLE DATEN WERDEN GELÖSCHT! (j/n): " OK
[ "$OK" != "j" ] && exit 1

# ==============================================
# 2. Partitionen anlegen
# ==============================================
echo
info "=== 2. Partitionen anlegen ==="

if [ "$SINGLE_DISK" = true ]; then
    # EINE Disk: GPT-Label, dann EFI (512M) + Root (Rest) in EINEM Durchgang
    parted /dev/"$ROOTDISK" mklabel gpt -s
    parted -a opt /dev/"$ROOTDISK" mkpart EFI fat32 1MiB 513MiB -s
    parted -a opt /dev/"$ROOTDISK" mkpart primary ext4 513MiB 100% -s
    parted /dev/"$ROOTDISK" set 1 esp on -s
    mkfs.fat -F32 /dev/"${ROOTDISK}${PART_PREFIX}1"
    mkfs.ext4 -F /dev/"${ROOTDISK}${PART_PREFIX}2"
    ROOTPART="${ROOTDISK}${PART_PREFIX}2"
    EFIPART="${ROOTDISK}${PART_PREFIX}1"
else
    # ZWEI Disks: Root auf ROOTDISK, EFI auf EFIDISK
    (parted /dev/"$ROOTDISK" mklabel gpt -s
    parted -a opt /dev/"$ROOTDISK" mkpart primary ext4 1MiB 100% -s
    mkfs.ext4 -F /dev/"$ROOTPART")

    (parted /dev/"$EFIDISK" mklabel gpt -s
    parted -a opt /dev/"$EFIDISK" mkpart EFI fat32 1MiB 513MiB -s
    parted /dev/"$EFIDISK" set 1 esp on -s
    mkfs.fat -F32 /dev/"$EFIPART")
fi

# ==============================================
# 3. Mounten
# ==============================================
echo
info "=== 3. Mounten ==="
mount /dev/"$ROOTPART" /mnt
mkdir -p /mnt/boot/efi
mount /dev/"$EFIPART" /mnt/boot/efi

# ==============================================
# 4. SSH-Key laden
# ==============================================
echo
info "=== 4. SSH-Key laden ==="

# SSH-Key suchen: Konfigurierter Pfad → /root → Home → USB-Sticks
if [ -n "$SSH_KEY_SOURCE" ] && [ -f "$SSH_KEY_SOURCE" ]; then
    SSH_KEY="$SSH_KEY_SOURCE"
elif [ -f "/root/.ssh/id_ed25519" ]; then
    SSH_KEY="/root/.ssh/id_ed25519"
elif [ -f "$HOME/.ssh/id_ed25519" ]; then
    SSH_KEY="$HOME/.ssh/id_ed25519"
else
    SSH_KEY=$(find /media /mnt /run/media -name "id_ed25519" -maxdepth 4 2>/dev/null | head -n 1 || true)
fi

if [ -z "${SSH_KEY:-}" ] || [ ! -f "${SSH_KEY:-}" ]; then
    warn "Kein SSH-Key gefunden."
    read -r -p "Pfad zum SSH-Key (Enter für Passwort-Abfrage): " SSH_KEY
fi

if [ -n "${SSH_KEY:-}" ] && [ -f "${SSH_KEY:-}" ]; then
    eval "$(ssh-agent)" > /dev/null
    ssh-add "$SSH_KEY" || warn "Konnte Key nicht laden – versuche Passwort-Abfrage."
    info "SSH-Key geladen: $SSH_KEY"
else
    warn "Kein SSH-Key – Du wirst nach dem Passwort gefragt."
fi

# ==============================================
# 5. Borg-Archiv ermitteln
# ==============================================
echo
info "=== 5. Neuestes Borg-Archiv ermitteln ==="
LATEST=$(borg list "$STORAGE" --last 1 --short 2>/dev/null)
[ -z "$LATEST" ] && error "Kein Archiv in $STORAGE gefunden."
echo "Gefundenes Archiv: $LATEST"

# ==============================================
# 6. Archiv mounten
# ==============================================
echo
info "=== 6. Archiv mounten ==="
mkdir -p /mnt/borg
borg mount "${STORAGE}::${LATEST}" /mnt/borg || error "Borg mount fehlgeschlagen."

# ==============================================
# 7. Restore via rsync
# ==============================================
echo
info "=== 7. Restore via rsync ==="
rsync -aHAXx --delete \
    --exclude="/dev" \
    --exclude="/proc" \
    --exclude="/sys" \
    --exclude="/tmp" \
    --exclude="/run" \
    --exclude="/mnt" \
    --exclude="/media" \
    /mnt/borg/ /mnt/

# ==============================================
# 8. fstab mit neuen UUIDs aktualisieren
# ==============================================
echo
info "=== 8. fstab mit neuen UUIDs aktualisieren ==="
ROOT_UUID=$(blkid -s UUID -o value "/dev/$ROOTPART")
EFI_UUID=$(blkid -s UUID -o value "/dev/$EFIPART")

# Backup der alten fstab
[ -f /mnt/etc/fstab ] && cp /mnt/etc/fstab /mnt/etc/fstab.bak.$(date +%Y%m%d)

cat > /mnt/etc/fstab <<FSTAB
# /etc/fstab – neu erstellt am $(date)
UUID=$ROOT_UUID  /           ext4  defaults,noatime  0 1
UUID=$EFI_UUID   /boot/efi   vfat  umask=0077        0 2
tmpfs            /tmp        tmpfs defaults,noatime  0 0
FSTAB

info "fstab mit neuen UUIDs aktualisiert."

# ==============================================
# 9. Chroot vorbereiten
# ==============================================
echo
info "=== 9. Chroot vorbereiten ==="
mount --bind /dev  /mnt/dev
mount --bind /proc /mnt/proc
mount --bind /sys  /mnt/sys
mount --bind /run  /mnt/run 2>/dev/null || true

# ==============================================
# 10. GRUB installieren (UEFI)
# ==============================================
echo
info "=== 10. GRUB installieren ==="
chroot /mnt grub-install --target=x86_64-efi \
    --efi-directory=/boot/efi \
    --bootloader-id=Debian \
    --recheck
chroot /mnt update-grub

# ==============================================
# 11. Aufräumen
# ==============================================
echo
info "=== 11. Aufräumen ==="
borg umount /mnt/borg || umount -l /mnt/borg
umount /mnt/boot/efi
umount /mnt/run 2>/dev/null || true
umount /mnt/dev
umount /mnt/proc
umount /mnt/sys
umount /mnt

echo
echo "=============================="
echo -e "${GREEN}=== Restore abgeschlossen! ===${NC}"
echo "=============================="
echo
echo "Root: UUID=$ROOT_UUID  ($ROOTPART)"
echo "EFI:  UUID=$EFI_UUID   ($EFIPART)"
echo
echo "Neustart mit: systemctl reboot"