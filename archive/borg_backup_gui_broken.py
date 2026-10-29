#!/usr/bin/env python3
"""
Borg Backup GUI – Komplettes Backup-Management mit Borg.
Unterstützt SSH (Storage Box), S3 (Object Storage) und lokale Laufwerke.

Start: python3 borg_backup_gui.py
"""

from borg_backup_gui.app import main

if __name__ == '__main__':
    raise SystemExit(main())
