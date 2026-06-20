"""
Backend-Abstraktion für Borg Backup GUI.

Jedes Backend (SSH, S3, Local) liefert die korrekten
Umgebungsvariablen und Repository-URLs für Borg-Befehle.
"""

import os
import re
import shutil
import subprocess
from abc import ABC, abstractmethod

from borg_backup_gui.config import Profile
from borg_backup_gui.runner import BORG_BIN


class BackendBase(ABC):
    """Abstrakte Basis für alle Backend-Typen."""

    @abstractmethod
    def get_env(self, profile: Profile) -> dict:
        """Liefert die Umgebungsvariablen für Borg-Befehle."""

    @abstractmethod
    def get_repo_url(self, profile: Profile) -> str:
        """Liefert die bereinigte Repository-URL."""

    @abstractmethod
    def get_label(self) -> str:
        """Anzeigename des Backends."""

    def needs_root_for_create(self) -> bool:
        """Ob 'borg create' root-Rechte braucht (ja, für System-Backups)."""
        return True

    def get_privilege_check_cmd(self) -> list[str]:
        """Befehl zum Prüfen der sudo-NOPASSWD-Regel."""
        return [BORG_BIN, '--version']

    def get_privilege_check_env(self, profile: Profile) -> dict:
        """Env für Privilegien-Prüfung."""
        return self.get_env(profile)


class SSHBackend(BackendBase):
    """SSH-basiertes Backend (Hetzner Storage Box, beliebiger SSH-Server)."""

    def get_env(self, profile: Profile) -> dict:
        env = os.environ.copy()

        # SSH-Konfiguration: Port 23 für Hetzner Storage Box
        ssh_cmd = (
            "ssh -p 23"
            " -o StrictHostKeyChecking=accept-new"
            " -o BatchMode=yes"
            " -o ConnectTimeout=30"
            " -o ServerAliveInterval=15"
            " -o ServerAliveCountMax=3"
        )
        if profile.ssh_key:
            ssh_cmd += f" -i {profile.ssh_key}"
        env['BORG_RSH'] = ssh_cmd
        env['BORG_RELOCATED_REPO_ACCESS_IS_OK'] = 'yes'
        env['BORG_CACHE_DIR'] = os.path.expanduser('~/.cache/borg')

        if profile.passphrase:
            env['BORG_PASSPHRASE'] = profile.passphrase

        return env

    def get_repo_url(self, profile: Profile) -> str:
        repo = profile.storage.strip()
        # Korrigiert versehentliches Scp-Format mit Port im Pfad
        if '://' not in repo and ':' in repo:
            host_part, path_part = repo.split(':', 1)
            if path_part.startswith('23/'):
                repo = f'{host_part}:{path_part[3:]}'
        return repo

    def get_label(self) -> str:
        return 'SSH / Storage Box'


class S3Backend(BackendBase):
    """S3-kompatibles Backend (Hetzner Object Storage, AWS S3, MinIO, ...).
    
    Borg unterstützt S3 nativ. Kein BORG_RSH nötig.
    """

    def get_env(self, profile: Profile) -> dict:
        env = os.environ.copy()
        env['AWS_ACCESS_KEY_ID'] = profile.s3_access_key
        env['AWS_SECRET_ACCESS_KEY'] = profile.s3_secret_key

        if profile.s3_endpoint_url:
            env['AWS_ENDPOINT_URL'] = profile.s3_endpoint_url
        if profile.s3_region:
            env['AWS_DEFAULT_REGION'] = profile.s3_region

        env['BORG_CACHE_DIR'] = os.path.expanduser('~/.cache/borg')
        if profile.passphrase:
            env['BORG_PASSPHRASE'] = profile.passphrase

        # Kein BORG_RSH! Würde S3 stören.
        return env

    def get_repo_url(self, profile: Profile) -> str:
        bucket = profile.storage.strip()
        if not bucket.startswith('s3://'):
            bucket = f's3://{bucket}'
        return bucket

    def get_label(self) -> str:
        return 'S3 Object Storage'


class LocalBackend(BackendBase):
    """Lokales Dateisystem-Backend."""

    def get_env(self, profile: Profile) -> dict:
        env = os.environ.copy()
        env['BORG_CACHE_DIR'] = os.path.expanduser('~/.cache/borg')
        if profile.passphrase:
            env['BORG_PASSPHRASE'] = profile.passphrase
        return env

    def get_repo_url(self, profile: Profile) -> str:
        return profile.local_path.strip() or '/tmp/borg-repo'

    def get_label(self) -> str:
        return 'Lokales Laufwerk'


# ============================================================
# Factory
# ============================================================

BACKEND_MAP = {
    'ssh': SSHBackend,
    's3': S3Backend,
    'local': LocalBackend,
}


def get_backend(profile: Profile | None) -> BackendBase:
    """Liefert das passende Backend für ein Profil."""
    if profile is None:
        return SSHBackend()
    backend_cls = BACKEND_MAP.get(profile.type, SSHBackend)
    return backend_cls()


def get_command_with_privilege(
    cmd: list[str],
    env: dict,
    needs_root: bool = False,
    is_root: bool = False,
) -> tuple[list[str], dict]:
    """Wrap command mit non-interactive sudo wenn nötig."""
    if not needs_root or is_root:
        return cmd, env

    if shutil.which('sudo'):
        # Prüfung gegen den Borg-Befehl selbst
        check = subprocess.run(
            ['sudo', '-n', '-E', BORG_BIN, '--version'],
            capture_output=True, text=True,
            env=env
        )
        if check.returncode != 0:
            raise PermissionError(
                'Passwortfreies sudo ist nicht eingerichtet. '
                'Für automatische Backups muss eine einmalige NOPASSWD-Regel '
                'für dieses Programm eingerichtet werden.'
            )

        # Root-Läufe bekommen eigenen Cache
        env = env.copy()
        env['HOME'] = '/root'
        env['XDG_CONFIG_HOME'] = '/root/.config'
        env['XDG_CACHE_HOME'] = '/root/.cache'
        env['BORG_CACHE_DIR'] = '/root/.cache/borg'

        wrapped = ['sudo', '-n', '-E']
        wrapped.extend(cmd)
        return wrapped, env

    return cmd, env
