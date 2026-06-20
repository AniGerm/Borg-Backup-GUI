"""
Tray-Icon-Manager für Borg Backup GUI.

Unterstützt AyatanaAppIndicator3 (Linux-native) und pystray (Fallback).
"""

import datetime
import os
import threading
from pathlib import Path

from borg_backup_gui.config import CONFIG_DIR

try:
    from PIL import Image, ImageDraw
    import pystray
except Exception:
    Image = None
    ImageDraw = None
    pystray = None

try:
    from PIL import ImageTk
except Exception:
    ImageTk = None

try:
    import gi
    gi.require_version('AyatanaAppIndicator3', '0.1')
    gi.require_version('Gtk', '3.0')
    from gi.repository import AyatanaAppIndicator3, Gtk, GLib
except Exception:
    AyatanaAppIndicator3 = None
    Gtk = None
    GLib = None


class TrayIconManager:
    """Verwaltet Tray-Icon mit Live-Status und Menü."""

    def __init__(self, app):
        self.app = app
        self.master = app.root
        self.config = app.config

        self.tray_icon = None
        self.native_indicator = None
        self.native_indicator_thread = None
        self.native_indicator_supported = bool(AyatanaAppIndicator3 and Gtk)
        self.native_indicator_failed = False
        self.tray_supported = bool(pystray and Image and ImageDraw)
        self.is_root = os.geteuid() == 0
        self.tray_runtime_available = self.tray_supported and not self.is_root

        self.tray_ready = False
        self.tray_menu_signature = None
        self.tray_icon_signature = None
        self.tray_native_menu_items = []
        self.tray_running_animation_id = None
        self.tray_running_animation_phase = 0
        self.tray_hint_shown = False
        self.tray_live_refresh_id = None
        self.tray_live_refresh_glib_id = None
        self.tray_icon_path = None

    def setup(self, force_restart=False):
        """Tray-Icon starten/stoppen je nach Konfiguration."""
        if not self.tray_runtime_available:
            return

        tray_enabled = bool(self.config.global_settings.get('tray_enabled', True))
        if force_restart:
            self.stop()

        if tray_enabled and self.tray_icon is None and self.native_indicator is None:
            self._start()
        if not tray_enabled and (self.tray_icon is not None or self.native_indicator is not None):
            self.stop()

    def _start(self):
        if not self.tray_runtime_available:
            return

        self.tray_ready = False

        if self.native_indicator_supported and not self.native_indicator_failed and self._start_native():
            self.master.after(2000, self._ensure_ready_or_fallback)
            self._schedule_live_refresh(1000)
            return

        if not (pystray and Image and ImageDraw):
            self.tray_ready = False
            return

        self._schedule_live_refresh(1000)
        initial_color, _ = self._compute_status()
        image = self._build_icon_image(initial_color, self._status_kind(initial_color))

        try:
            self.tray_icon = pystray.Icon(
                'borg_backup_gui', image, 'Borg Backup GUI', self._build_pystray_menu()
            )
            self.tray_icon.run_detached()
            self.tray_ready = True
        except Exception:
            self.tray_icon = None
            self.tray_ready = False

    def _ensure_ready_or_fallback(self):
        if self.tray_ready or not self.tray_runtime_available:
            return

        if self.native_indicator is not None:
            indicator = self.native_indicator
            self.native_indicator = None
            self.native_indicator_failed = True
            try:
                if GLib is not None:
                    def _deactivate():
                        try:
                            indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.PASSIVE)
                        except Exception:
                            pass
                        return False
                    GLib.idle_add(_deactivate)
                else:
                    indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.PASSIVE)
            except Exception:
                pass

        if self.tray_icon is None:
            self._start()

    def stop(self):
        self.tray_ready = False
        self.native_indicator_failed = False
        self.tray_icon_signature = None
        self._cancel_live_refresh()

        if self.native_indicator is not None:
            indicator = self.native_indicator
            self.native_indicator = None
            try:
                if GLib is not None:
                    def _deactivate():
                        try:
                            indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.PASSIVE)
                        except Exception:
                            pass
                        return False
                    GLib.idle_add(_deactivate)
                else:
                    indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.PASSIVE)
            except Exception:
                pass

        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
            self.tray_icon = None

    # ---- Native Indicator ----

    def _start_native(self):
        if self.native_indicator is not None:
            return True

        try:
            indicator = AyatanaAppIndicator3.Indicator.new(
                'borg_backup_gui',
                '',
                AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS
            )
            self.native_indicator = indicator

            if Gtk is not None and Gtk.main_level() == 0:
                self.native_indicator_thread = threading.Thread(target=Gtk.main, daemon=True)
                self.native_indicator_thread.start()

            initial_color, _ = self._compute_status()
            if GLib is not None:
                menu = self._build_native_menu()
                status_kind = self._status_kind(initial_color)
                rgb = tuple(int(initial_color[i:i + 2], 16) for i in (1, 3, 5))
                startup_img = self._build_icon_image((*rgb, 255), status_kind, self.tray_running_animation_phase)

                def _activate():
                    try:
                        icon_path = self._tray_icon_path
                        startup_img.save(icon_path, 'PNG')
                        indicator.set_icon(icon_path)
                        indicator.set_menu(menu)
                        indicator.set_title('Borg Backup GUI')
                        indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
                        self.tray_ready = True
                    except Exception:
                        self.tray_ready = False
                        self.native_indicator = None
                        self.native_indicator_failed = True
                        self.master.after(0, self._start)
                    self._update_icon(initial_color)
                    return False

                GLib.idle_add(_activate)
            else:
                indicator.set_menu(self._build_native_menu())
                indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
                self.tray_ready = True
                self._update_icon(initial_color)

            return True
        except Exception:
            self.native_indicator = None
            self.tray_ready = False
            self.native_indicator_failed = True
            return False

    # ---- Menüs ----

    def _status_lines(self):
        """Aktuelle Statuszeilen fürs Tray-Menü."""
        backup_running = getattr(self.app, 'backup_running', False)
        if backup_running:
            return self._live_summary_lines()

        status_title = getattr(self.app, 'status_title_var', None)
        title = status_title.get() if status_title else 'Borg Backup GUI'
        last_success = self._format_time(self.app.config.get_status(
            self.app.config.global_settings.get('active_profile', '')
        ).last_success_at if self.app.config.get_active_profile() else '')

        if last_success == '-':
            last_success = 'noch nie'

        next_backup = getattr(self.app, 'next_backup_dt', None)
        next_text = next_backup.strftime('%d.%m.%Y %H:%M') if next_backup else 'deaktiviert'

        status = self.app.config.get_status(
            self.app.config.global_settings.get('active_profile', '')
        ) if self.app.config.get_active_profile() else None
        last_error = status.last_error.strip() if status and status.last_error else 'keine Meldung'

        return (
            f'Live: {title}',
            f'Letzter Lauf: {last_success}',
            f'Geplant: {next_text}',
            f'Status: {last_error[:110]}'
        )

    def _live_summary_lines(self):
        """Statuszeilen während eines laufenden Backups."""
        backup_running = getattr(self.app, 'backup_running', False)
        runtime = None
        if backup_running and hasattr(self.app, 'current_backup_started_at') and self.app.current_backup_started_at:
            runtime = (datetime.datetime.now() - self.app.current_backup_started_at).total_seconds()

        task_label = ''
        if hasattr(self.app, 'backup_current_task') and self.app.backup_current_task:
            task_label = self.app.backup_current_task.get('label', '')

        status = self.app.config.get_status(
            self.app.config.global_settings.get('active_profile', '')
        ) if self.app.config.get_active_profile() else None
        files_count = status.last_files_count if status else 0
        source_size = status.last_source_size if status else ''
        dedupe_size = status.last_dedupe_size if status else ''
        last_item = status.last_item if status else ''

        return (
            'Live: Backup läuft',
            f'Laufzeit: {self._format_runtime(runtime)}',
            f'Fortschritt: {files_count} Dateien | Quelle {source_size} | Dedupe {dedupe_size}',
            f'Aktuell: {task_label or (last_item[:100] if last_item else "arbeitet...")}'
        )

    def _backup_action_label(self):
        return 'Backup abbrechen' if getattr(self.app, 'backup_running', False) else 'Backup jetzt starten'

    def _build_pystray_menu(self):
        return pystray.Menu(
            pystray.MenuItem(lambda _item: self._status_lines()[0], lambda *_: None, enabled=False),
            pystray.MenuItem(lambda _item: self._status_lines()[1], lambda *_: None, enabled=False),
            pystray.MenuItem(lambda _item: self._status_lines()[2], lambda *_: None, enabled=False),
            pystray.MenuItem(lambda _item: self._status_lines()[3], lambda *_: None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Fenster anzeigen', lambda *_: self.master.event_generate("<<ShowWindow>>", when="tail")),
            pystray.MenuItem(lambda _item: self._backup_action_label(), self._on_backup_toggle),
            pystray.MenuItem('Beenden', lambda *_: self.master.event_generate("<<QuitApp>>", when="tail"))
        )

    def _build_native_menu(self):
        summary = self._status_lines()
        menu = Gtk.Menu()

        items = []
        for i in range(4):
            item = Gtk.MenuItem(label=summary[i])
            item.set_sensitive(False)
            menu.append(item)
            items.append(item)

        menu.append(Gtk.SeparatorMenuItem())

        show_item = Gtk.MenuItem(label='Fenster anzeigen')
        show_item.connect('activate', lambda *_: self.master.after(0, lambda: self.master.event_generate("<<ShowWindow>>")))
        menu.append(show_item)

        backup_item = Gtk.MenuItem(label=self._backup_action_label())
        backup_item.connect('activate', lambda *_: self.master.after(0, self._on_backup_toggle))
        menu.append(backup_item)
        items.append(backup_item)

        menu.append(Gtk.SeparatorMenuItem())

        quit_item = Gtk.MenuItem(label='Beenden')
        quit_item.connect('activate', lambda *_: self.master.after(0, lambda: self.master.event_generate("<<QuitApp>>")))
        menu.append(quit_item)

        menu.show_all()
        self.tray_native_menu_items = items
        return menu

    def _update_menu_labels(self):
        summary = self._status_lines()
        action_label = self._backup_action_label()

        if self.native_indicator is not None and self.tray_native_menu_items and GLib is not None:
            labels = list(summary) + [action_label]

            def _do_labels():
                try:
                    for item, label in zip(self.tray_native_menu_items[:4], labels[:4]):
                        item.set_label(label)
                    if len(self.tray_native_menu_items) >= 5:
                        self.tray_native_menu_items[4].set_label(labels[4])
                except Exception:
                    pass
                return False

            GLib.idle_add(_do_labels)

    def _on_backup_toggle(self, *_args):
        backup_running = getattr(self.app, 'backup_running', False)
        if backup_running:
            self.master.event_generate("<<TrayBackupToggle>>", when="tail")
        else:
            self.master.event_generate("<<TrayBackupToggle>>", when="tail")

    # ---- Icon ----

    def _status_kind(self, color_hex):
        if getattr(self.app, 'backup_running', False):
            return 'running'
        if color_hex == '#28a745':
            return 'ok'
        if color_hex == '#dc3545':
            return 'error'
        if color_hex == '#e0a800':
            return 'warning'
        return 'idle'

    def _compute_status(self):
        """Berechnet Ampel-Farbe und Titel (1:1 aus app übernommen)."""
        now = datetime.datetime.now()
        profile = self.app.config.get_active_profile()
        status = self.app.config.get_status(profile.name) if profile else None

        color = '#6e6e6e'
        title = 'Noch kein Backup ausgeführt'

        if getattr(self.app, 'backup_running', False):
            color = '#2d7ff9'
            title = 'Backup läuft gerade'
        elif status and status.last_exit_code == 0:
            last_success = self._parse_iso(status.last_success_at)
            if last_success:
                age_hours = (now - last_success).total_seconds() / 3600.0
                if age_hours <= 24:
                    color = '#28a745'
                    title = 'Backup aktuell und erfolgreich'
                elif age_hours <= 48:
                    color = '#e0a800'
                    title = 'Backup erfolgreich, aber nicht mehr tagesaktuell'
                else:
                    color = '#dc3545'
                    title = 'Letztes erfolgreiches Backup ist zu alt'
        elif status and status.last_exit_code == 1:
            last_success = self._parse_iso(status.last_success_at)
            if last_success:
                age_hours = (now - last_success).total_seconds() / 3600.0
                if age_hours <= 48:
                    color = '#e0a800'
                    title = 'Backup mit Warnungen abgeschlossen'
                else:
                    color = '#dc3545'
                    title = 'Letztes Backup hatte Warnungen und ist zu alt'
        elif status and status.last_exit_code is not None:
            color = '#dc3545'
            title = 'Letztes Backup fehlgeschlagen'

        return color, title

    def _parse_iso(self, value):
        if not value:
            return None
        try:
            return datetime.datetime.fromisoformat(value)
        except ValueError:
            return None

    def _format_time(self, value):
        dt = self._parse_iso(value)
        if not dt:
            return '-'
        return dt.strftime('%d.%m.%Y %H:%M:%S')

    def _format_runtime(self, seconds):
        if seconds is None:
            return 'noch keine Daten'
        total = max(0, int(seconds))
        h, remainder = divmod(total, 3600)
        m, s = divmod(remainder, 60)
        return f'{h:02d}:{m:02d}:{s:02d}'

    def _build_icon_image(self, status_color, status_kind='idle', animation_phase=0):
        if not (Image and ImageDraw):
            return None

        size = 64
        image = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)

        bg = (35, 35, 35, 245)
        draw.rounded_rectangle((4, 4, 60, 60), radius=14, fill=bg)

        if status_kind == 'running':
            draw.rounded_rectangle((6, 6, 58, 58), radius=13, outline=status_color, width=4)

        style = self.config.global_settings.get('tray_icon_style', 'disk')
        fg = (245, 245, 245, 255)
        muted = (180, 180, 180, 255)
        sc = tuple(int(c) for c in status_color[:3]) if isinstance(status_color, tuple) else (0, 0, 0)

        if style == 'shield':
            draw.polygon([(32, 12), (48, 18), (45, 38), (32, 51), (19, 38), (16, 18)], fill=fg)
            draw.polygon([(32, 18), (42, 22), (40, 35), (32, 43), (24, 35), (22, 22)], fill=bg)
        elif style == 'palm':
            draw.rectangle((30, 26, 34, 50), fill=muted)
            draw.polygon([(32, 26), (14, 22), (24, 16)], fill=fg)
            draw.polygon([(32, 26), (50, 22), (40, 16)], fill=fg)
            draw.polygon([(32, 24), (18, 10), (30, 14)], fill=fg)
            draw.polygon([(32, 24), (46, 10), (34, 14)], fill=fg)
            draw.polygon([(32, 22), (24, 8), (40, 8)], fill=fg)
        else:
            draw.rounded_rectangle((14, 19, 50, 43), radius=5, fill=fg)
            draw.rectangle((19, 25, 45, 33), fill=bg)
            draw.rectangle((19, 36, 45, 39), fill=muted)

        draw.rounded_rectangle((10, 52, 54, 58), radius=3, fill=sc if isinstance(status_color, tuple) else (0, 0, 0))
        r = 9
        sx, sy = 49, 15
        draw.ellipse((sx - r - 2, sy - r - 2, sx + r + 2, sy + r + 2), fill=(17, 24, 39, 255))
        draw.ellipse((sx - r, sy - r, sx + r, sy + r), fill=sc if isinstance(status_color, tuple) else (0, 0, 0))

        if status_kind == 'running':
            if animation_phase % 2 == 0:
                draw.polygon([(28, 12), (28, 26), (40, 19)], fill=(255, 255, 255, 235))
            else:
                draw.rounded_rectangle((26, 12, 31, 26), radius=2, fill=(255, 255, 255, 235))
                draw.rounded_rectangle((34, 12, 39, 26), radius=2, fill=(255, 255, 255, 235))

        return image

    def _update_icon(self, color_hex, animation_phase=None):
        status_kind = self._status_kind(color_hex)
        if animation_phase is None:
            animation_phase = self.tray_running_animation_phase if status_kind == 'running' else 0

        icon_sig = (bool(self.native_indicator), color_hex, status_kind, animation_phase,
                     self.config.global_settings.get('tray_icon_style', 'disk'))
        if icon_sig == self.tray_icon_signature:
            return

        def _rgb(hex_color):
            return tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))

        if self.native_indicator is not None:
            try:
                rgb = _rgb(color_hex)
                img = self._build_icon_image((*rgb, 255), status_kind, animation_phase)
                self._set_native_icon_from_image(img)
            except Exception:
                pass
            self.tray_icon_signature = icon_sig
            return

        if self.tray_icon is not None and self.tray_supported:
            try:
                rgb = _rgb(color_hex)
                self.tray_icon.icon = self._build_icon_image((*rgb, 255), status_kind, animation_phase)
                self.tray_icon.title = 'Borg Backup GUI'
            except Exception:
                pass
            self.tray_icon_signature = icon_sig

    def _set_native_icon_from_image(self, img):
        if self.native_indicator is None or GLib is None:
            return
        try:
            icon_path = self._tray_icon_path
            img.save(icon_path, 'PNG')
            indicator = self.native_indicator

            def _do():
                try:
                    indicator.set_icon(icon_path)
                except Exception:
                    pass
                return False

            GLib.idle_add(_do)
        except Exception:
            pass

    @property
    def _tray_icon_path(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        return str(CONFIG_DIR / 'tray-icon.png')

    # ---- Live-Refresh ----

    def _schedule_live_refresh(self, delay_ms=1000):
        self._cancel_live_refresh()
        delay_ms = max(250, int(delay_ms))

        if self.native_indicator is not None and GLib is not None:
            try:
                self.tray_live_refresh_glib_id = GLib.timeout_add(delay_ms, self._periodic_live_refresh)
                return
            except Exception:
                pass

        self.tray_live_refresh_id = self.master.after(delay_ms, self._periodic_live_refresh)

    def _cancel_live_refresh(self):
        if self.tray_live_refresh_id is not None:
            try:
                self.master.after_cancel(self.tray_live_refresh_id)
            except Exception:
                pass
            self.tray_live_refresh_id = None
        if self.tray_live_refresh_glib_id is not None and GLib is not None:
            try:
                GLib.source_remove(self.tray_live_refresh_glib_id)
            except Exception:
                pass
            self.tray_live_refresh_glib_id = None

    def _periodic_live_refresh(self):
        self.tray_live_refresh_id = None
        self.tray_live_refresh_glib_id = None

        if not self.tray_runtime_available or not self.config.global_settings.get('tray_enabled', True):
            return False

        self._update_menu_labels()

        delay = 1000 if getattr(self.app, 'backup_running', False) else 15000
        self._schedule_live_refresh(delay)
        return False

    # ---- Health-Check ----

    def schedule_health_check(self):
        self.master.after(30000, self._periodic_health_check)

    def _periodic_health_check(self):
        self.master.after(30000, self._periodic_health_check)

        if not self.tray_runtime_available:
            return

        tray_enabled = bool(self.config.global_settings.get('tray_enabled', True))
        if not tray_enabled:
            self.tray_ready = False
            return

        # pystray prüfen
        if self.tray_icon is not None:
            try:
                if hasattr(self.tray_icon, 'update_menu'):
                    self.tray_icon.update_menu()
                else:
                    _ = self.tray_icon.icon
            except Exception:
                try:
                    self.tray_icon.stop()
                except Exception:
                    pass
                self.tray_icon = None
                self.tray_ready = False
                self._start()
                self._restore_window()
            return

        # Native Indicator prüfen
        if self.native_indicator is not None:
            try:
                if Gtk is not None and Gtk.main_level() == 0:
                    raise RuntimeError('Gtk main loop not running')
            except Exception:
                self.native_indicator = None
                self.native_indicator_failed = True
                self.tray_ready = False
                self._start()
                self._restore_window()
            return

        # Kein Icon, aber sollte aktiv sein → starten
        self.tray_ready = False
        self._start()
        self._restore_window()

    def _restore_window(self):
        try:
            if str(self.master.state()) == 'withdrawn':
                self.master.deiconify()
                self.master.lift()
        except Exception:
            pass
