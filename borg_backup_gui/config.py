"""
Konfigurations-Management für die Borg Backup GUI.

Verwaltet Profile (SSH, S3, Lokal), globale Einstellungen,
Status-Daten pro Profil und die Migration vom alten Config-Format.
"""

import datetime
import json
import os
import shutil
from dataclasses import dataclass, field, asdict
from pathlib import Path

# ============================================================
# Pfade
# ============================================================
OLD_CONFIG_DIR = Path.home() / '.config' / 'hetzner-borg-gui'
OLD_CONFIG_FILE = OLD_CONFIG_DIR / 'config.json'
OLD_STATUS_FILE = OLD_CONFIG_DIR / 'status.json'

CONFIG_DIR = Path.home() / '.config' / 'borg-backup-gui'
CONFIG_FILE = CONFIG_DIR / 'config.json'

# ============================================================
# Datenklassen
# ============================================================

@dataclass
class ProfileStatus:
    """Laufzeit-Status eines Backup-Profils."""
    last_run_at: str = ''
    last_success_at: str = ''
    last_exit_code: int | None = None
    last_error: str = ''
    last_duration_sec: float | None = None
    last_files_count: int = 0
    last_source_size: str = ''
    last_dedupe_size: str = ''
    last_item: str = ''
    last_check_at: str = ''
    last_compact_at: str = ''


@dataclass
class Profile:
    """Ein Backup-Profil – definiert Quelle, Ziel und Zeitplan."""
    name: str = 'Standard'
    # Backend-Typ
    type: str = 'ssh'       # 'ssh', 's3', 'local'
    # SSH-spezifisch
    storage: str = 'uXXXXXX@uXXXXXX.your-storagebox.de:./backup'
    ssh_key: str = ''
    # S3-spezifisch
    s3_access_key: str = ''
    s3_secret_key: str = ''
    s3_endpoint_url: str = ''
    s3_region: str = ''
    # Lokal-spezifisch
    local_path: str = ''
    # Gemeinsam
    passphrase: str = ''
    compression: str = 'lz4'
    encryption: str = 'repokey'
    include_folders: list = field(default_factory=lambda: ['/'])
    exclude_folders: list = field(default_factory=lambda: [
        '/dev', '/proc', '/sys', '/tmp', '/run',
        '/mnt', '/media', '/lost+found',
        '/var/cache/apt/archives', '/var/tmp',
        '/home/*/.vagrant.d', '/home/*/snap', '/home/*/.local/share/Trash'
    ])
    # Zeitplan
    schedule_type: str = 'manual'   # 'manual', 'daily', 'interval'
    schedule_interval: int = 3      # Stunden
    schedule_time: str = '03:15'
    catchup_missed: bool = True
    # Wartung
    prune_enabled: bool = False
    validate_enabled: bool = True
    validate_interval: int = 3      # Wochen
    optimize_enabled: bool = False
    optimize_interval: int = 3      # Wochen
    # Aktiv
    enabled: bool = True


# ============================================================
# ConfigManager
# ============================================================

class ConfigManager:
    """Lädt, speichert und migriert die gesamte Konfiguration."""

    def __init__(self):
        self.profiles: list[Profile] = []
        self.status_map: dict[str, ProfileStatus] = {}  # profile_name -> status
        self.global_settings: dict = {
            'tray_enabled': True,
            'tray_icon_style': 'disk',
            'promptless_privilege': True,
            'privilege_mode': 'auto',
            'active_profile': None,
        }
        self._load_or_migrate()

    def _load_or_migrate(self):
        """Lädt Config – migriert von alter Hetzner-Config falls nötig."""
        if CONFIG_FILE.exists():
            self._load()
        elif OLD_CONFIG_FILE.exists():
            self._migrate_from_v1()
        else:
            # Neuanlage: ein leeres Profil
            self.profiles = [Profile()]
            self._update_status_for('Standard')
            self.save()

    def _load(self):
        """Lädt Config aus neuem Pfad."""
        try:
            with open(CONFIG_FILE, 'r') as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            self.profiles = [Profile()]
            return

        # Globale Einstellungen
        global_data = data.get('global', {})
        for key in self.global_settings:
            if key in global_data:
                self.global_settings[key] = global_data[key]

        # Profile
        self.profiles = []
        for pdata in data.get('profiles', []):
            profile = Profile(**{k: v for k, v in pdata.items() if k != 'status'})
            self.profiles.append(profile)
            # Status laden
            if 'status' in pdata and pdata['status']:
                self.status_map[profile.name] = ProfileStatus(**pdata['status'])

    def save(self):
        """Speichert Config im neuen Pfad."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            'version': 2,
            'global': self.global_settings,
            'profiles': []
        }
        for profile in self.profiles:
            pdata = asdict(profile)
            status = self.status_map.get(profile.name)
            if status:
                pdata['status'] = {k: v for k, v in asdict(status).items() if v}
            data['profiles'].append(pdata)

        with open(CONFIG_FILE, 'w') as f:
            json.dump(data, f, indent=4)

    def _migrate_from_v1(self) -> bool:
        """Migriert alte Hetzner-Config ins neue Format. Alte Dateien bleiben erhalten."""
        try:
            with open(OLD_CONFIG_FILE, 'r') as f:
                old = json.load(f)
            with open(OLD_STATUS_FILE, 'r') as f:
                old_status = json.load(f)
        except Exception:
            self.profiles = [Profile()]
            self._update_status_for('Standard')
            self.save()
            return False

        # Ein Profil aus alter Config bauen
        profile = Profile(
            name='Standard',
            type='ssh',
            storage=str(old.get('storage', Profile().storage)),
            ssh_key=str(old.get('ssh_key', '')),
            passphrase=str(old.get('borg_passphrase', '')),
            compression=str(old.get('compression', 'lz4')),
            encryption=str(old.get('encryption', 'repokey')),
            include_folders=old.get('include_folders', ['/']),
            exclude_folders=old.get('exclude_folders', []),
            schedule_type=str(old.get('schedule_type', 'manual')),
            schedule_interval=int(old.get('schedule_interval', 3)),
            schedule_time=str(old.get('schedule_time', '03:15')),
            catchup_missed=bool(old.get('catchup_missed', True)),
            prune_enabled=bool(old.get('prune_enabled', False)),
            validate_enabled=bool(old.get('validate_enabled', True)),
            validate_interval=int(old.get('validate_interval', 3)),
            optimize_enabled=bool(old.get('optimize_enabled', False)),
            optimize_interval=int(old.get('optimize_interval', 3)),
            enabled=True,
        )
        self.profiles = [profile]

        # Global
        self.global_settings['tray_enabled'] = bool(old.get('tray_enabled', True))
        self.global_settings['tray_icon_style'] = str(old.get('tray_icon_style', 'disk'))
        self.global_settings['promptless_privilege'] = bool(old.get('promptless_privilege', True))
        self.global_settings['privilege_mode'] = str(old.get('privilege_mode', 'auto'))

        # Status migrieren
        status = ProfileStatus(
            last_run_at=str(old_status.get('last_run_at', '')),
            last_success_at=str(old_status.get('last_success_at', '')),
            last_exit_code=old_status.get('last_exit_code'),
            last_error=str(old_status.get('last_error', '')),
            last_duration_sec=old_status.get('last_duration_sec'),
            last_files_count=int(old_status.get('last_files_count', 0)),
            last_source_size=str(old_status.get('last_source_size', '')),
            last_dedupe_size=str(old_status.get('last_dedupe_size', '')),
            last_item=str(old_status.get('last_item', '')),
        )
        self.status_map['Standard'] = status

        self.save()
        return True

    # ---- Profil-Operationen ----

    def get_profiles(self) -> list[Profile]:
        return self.profiles

    def get_active_profile(self) -> Profile | None:
        name = self.global_settings.get('active_profile')
        if name:
            for p in self.profiles:
                if p.name == name:
                    return p
        if self.profiles:
            return self.profiles[0]
        return None

    def set_active_profile(self, name: str):
        for p in self.profiles:
            if p.name == name:
                self.global_settings['active_profile'] = name
                self.save()
                return

    def add_profile(self, profile: Profile):
        # Name-Konflikte vermeiden
        existing_names = {p.name for p in self.profiles}
        name = profile.name
        counter = 1
        while name in existing_names:
            name = f'{profile.name} ({counter})'
            counter += 1
        profile.name = name
        self.profiles.append(profile)
        self._update_status_for(name)
        self.save()

    def remove_profile(self, name: str):
        self.profiles = [p for p in self.profiles if p.name != name]
        self.status_map.pop(name, None)
        if self.global_settings.get('active_profile') == name:
            self.global_settings['active_profile'] = None
        self.save()

    def update_profile(self, profile: Profile):
        for i, p in enumerate(self.profiles):
            if p.name == profile.name:
                # Status erhalten
                old_status = self.status_map.pop(p.name, None)
                self.profiles[i] = profile
                if old_status:
                    self.status_map[profile.name] = old_status
                self.save()
                return

    # ---- Status ----

    def _update_status_for(self, profile_name: str):
        if profile_name not in self.status_map:
            self.status_map[profile_name] = ProfileStatus()

    def get_status(self, profile_name: str) -> ProfileStatus:
        if profile_name not in self.status_map:
            self.status_map[profile_name] = ProfileStatus()
        return self.status_map[profile_name]

    def save_status(self, profile_name: str):
        self._update_status_for(profile_name)
        self.save()

    # ---- Hilfsmethoden ----

    def get_storage_for_display(self, profile: Profile | None = None) -> str:
        """Liefert eine lesbare Repräsentation des Speicherorts."""
        p = profile or self.get_active_profile()
        if not p:
            return 'Kein Profil'
        if p.type == 'local':
            return p.local_path or '(nicht gesetzt)'
        if p.type == 's3':
            return f's3://{p.storage}' if p.storage else '(nicht gesetzt)'
        return p.storage or '(nicht gesetzt)'

    def get_backend_type_label(self, profile: Profile | None = None) -> str:
        p = profile or self.get_active_profile()
        if not p:
            return '-'
        labels = {'ssh': 'SSH / Storage Box', 's3': 'S3 Object Storage', 'local': 'Lokales Laufwerk'}
        return labels.get(p.type, p.type)
