"""Control channel between the GUI (user) and the root service.

Newline-delimited JSON over a Unix stream socket. The server identifies the
caller via ``SO_PEERCRED`` and authorizes every method through polkit
(``pkcheck``) once per connection and action. Standard library only.
"""

from __future__ import annotations

import json
import os
import queue
import socket
import socketserver
import struct
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

DEFAULT_SOCKET = Path("/run/borg-backup-gui/control.sock")
MAX_MESSAGE_BYTES = 8 * 1024 * 1024
PKCHECK = "/usr/bin/pkcheck"
SUBSCRIBER_QUEUE_SIZE = 2000
HEARTBEAT_SEC = 15.0

ACTION_STATUS = "org.borgbackupgui.status"
ACTION_RUN = "org.borgbackupgui.run"
ACTION_CONFIGURE = "org.borgbackupgui.configure"
ACTION_RESTORE = "org.borgbackupgui.restore"

METHOD_ACTIONS: dict[str, str] = {
    "ping": ACTION_STATUS,
    "get_status": ACTION_STATUS,
    "get_config": ACTION_STATUS,
    "get_logs": ACTION_STATUS,
    "list_archives": ACTION_STATUS,
    "archive_info": ACTION_STATUS,
    "subscribe": ACTION_STATUS,
    "run_job": ACTION_RUN,
    "cancel": ACTION_RUN,
    "save_config": ACTION_CONFIGURE,
    "import_legacy": ACTION_CONFIGURE,
    "init_repo": ACTION_CONFIGURE,
    "extract": ACTION_RESTORE,
    "mount": ACTION_RESTORE,
    "umount": ACTION_RESTORE,
    "delete_archive": ACTION_RESTORE,
}


class IpcError(Exception):
    """Error reported by the service (or a transport failure on the client)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Peer:
    pid: int
    uid: int
    gid: int


class Authorizer(Protocol):
    def check(self, peer: Peer, action: str) -> tuple[bool, str]: ...


def peer_credentials(sock: socket.socket) -> Peer:
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, gid = struct.unpack("3i", raw)
    return Peer(pid=pid, uid=uid, gid=gid)


def process_start_time(pid: int) -> int:
    """Kernel start time of ``pid`` in clock ticks (field 22 of ``/proc/<pid>/stat``)."""
    with open(f"/proc/{int(pid)}/stat", encoding="utf-8", errors="replace") as handle:
        data = handle.read()
    after_comm = data.rsplit(")", 1)[1].split()
    return int(after_comm[19])


class PolkitAuthorizer:
    """Authorize through ``pkcheck``; root callers are always allowed."""

    def __init__(self, pkcheck: str = PKCHECK, timeout: float = 600.0) -> None:
        self.pkcheck = pkcheck
        self.timeout = timeout

    def check(self, peer: Peer, action: str) -> tuple[bool, str]:
        if peer.uid == 0:
            return True, ""
        try:
            start = process_start_time(peer.pid)
        except (OSError, ValueError, IndexError):
            return False, "Aufrufender Prozess nicht ermittelbar"
        cmd = [
            self.pkcheck,
            "--action-id",
            action,
            "--process",
            f"{peer.pid},{start},{peer.uid}",
            "--allow-user-interaction",
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"polkit-Prüfung fehlgeschlagen: {exc}"
        if proc.returncode == 0:
            return True, ""
        if proc.returncode == 3:
            return False, "Authentifizierung abgebrochen oder abgelehnt"
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return False, detail[-1] if detail else f"nicht autorisiert ({action})"


def encode_message(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


class _LineSocket:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buffer = b""

    def read_message(self) -> dict[str, Any] | None:
        while b"\n" not in self.buffer:
            chunk = self.sock.recv(65536)
            if not chunk:
                return None
            self.buffer += chunk
            if len(self.buffer) > MAX_MESSAGE_BYTES:
                raise IpcError("invalid", "Nachricht zu groß")
        line, self.buffer = self.buffer.split(b"\n", 1)
        message = json.loads(line.decode("utf-8"))
        if not isinstance(message, dict):
            raise IpcError("invalid", "Nachricht ist kein Objekt")
        return message


Handler = Callable[[str, dict[str, Any], Peer], Any]


class _Connection(socketserver.BaseRequestHandler):
    server: IpcServer

    def handle(self) -> None:
        sock: socket.socket = self.request
        try:
            peer = peer_credentials(sock)
        except OSError:
            return
        reader = _LineSocket(sock)
        granted: set[str] = set()

        def send(payload: dict[str, Any]) -> bool:
            try:
                sock.sendall(encode_message(payload))
                return True
            except OSError:
                return False

        while True:
            try:
                message = reader.read_message()
            except (json.JSONDecodeError, UnicodeDecodeError):
                send({"id": None, "ok": False, "error": {"code": "invalid", "message": "Ungültiges JSON"}})
                return
            except IpcError as exc:
                send({"id": None, "ok": False, "error": {"code": exc.code, "message": exc.message}})
                return
            except OSError:
                return
            if message is None:
                return
            req_id = message.get("id")
            method = str(message.get("method") or "")
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            action = METHOD_ACTIONS.get(method)
            if action is None:
                send({"id": req_id, "ok": False, "error": {"code": "unknown_method", "message": method}})
                continue
            if action not in granted:
                allowed, reason = self.server.authorizer.check(peer, action)
                if not allowed:
                    send({"id": req_id, "ok": False, "error": {"code": "unauthorized", "message": reason}})
                    continue
                granted.add(action)
            if method == "subscribe":
                if send({"id": req_id, "ok": True, "result": {"subscribed": True}}):
                    self._stream_events(send)
                return
            try:
                result = self.server.handler(method, params or {}, peer)
                reply = {"id": req_id, "ok": True, "result": result}
            except IpcError as exc:
                reply = {"id": req_id, "ok": False, "error": {"code": exc.code, "message": exc.message}}
            except Exception as exc:  # noqa: BLE001 - report to client instead of killing the thread
                reply = {"id": req_id, "ok": False, "error": {"code": "internal", "message": str(exc)}}
            if not send(reply):
                return

    def _stream_events(self, send: Callable[[dict[str, Any]], bool]) -> None:
        """Dedicated writer for a subscriber; a slow client only ever blocks its own thread."""
        events: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        self.server.add_subscriber(events)
        try:
            while not self.server.closing.is_set():
                try:
                    payload = events.get(timeout=HEARTBEAT_SEC)
                except queue.Empty:
                    payload = {"event": "heartbeat", "data": None}
                if payload.get("event") == "_overflow" or not send(payload):
                    return
        finally:
            self.server.remove_subscriber(events)


class IpcServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, path: Path, handler: Handler, authorizer: Authorizer, socket_mode: int = 0o666) -> None:
        self.handler = handler
        self.authorizer = authorizer
        self._subscribers: list[queue.Queue[dict[str, Any]]] = []
        self._sub_lock = threading.Lock()
        self.closing = threading.Event()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        old_umask = os.umask(0o077)
        try:
            super().__init__(str(path), _Connection)
        finally:
            os.umask(old_umask)
        os.chmod(path, socket_mode)
        self.socket_path = path

    def add_subscriber(self, events: queue.Queue[dict[str, Any]]) -> None:
        with self._sub_lock:
            self._subscribers.append(events)

    def remove_subscriber(self, events: queue.Queue[dict[str, Any]]) -> None:
        with self._sub_lock:
            if events in self._subscribers:
                self._subscribers.remove(events)

    def subscriber_count(self) -> int:
        with self._sub_lock:
            return len(self._subscribers)

    def broadcast(self, event: str, data: Any) -> None:
        payload = {"event": event, "data": data}
        with self._sub_lock:
            subscribers = list(self._subscribers)
        for events in subscribers:
            try:
                events.put_nowait(payload)
            except queue.Full:
                self.remove_subscriber(events)
                try:
                    events.get_nowait()
                    events.put_nowait({"event": "_overflow", "data": None})
                except (queue.Empty, queue.Full):
                    pass

    def server_close(self) -> None:
        self.closing.set()
        super().server_close()
        try:
            self.socket_path.unlink()
        except OSError:
            pass


class IpcClient:
    """Blocking client with one persistent connection (thread-safe, reconnects once)."""

    def __init__(self, path: Path | str | None = None, timeout: float = 600.0) -> None:
        self.path = str(path or os.environ.get("BORG_BACKUP_GUI_SOCKET") or DEFAULT_SOCKET)
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._reader: _LineSocket | None = None
        self._lock = threading.Lock()
        self._next_id = 0

    def _connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        try:
            sock.connect(self.path)
        except OSError as exc:
            sock.close()
            raise IpcError("unavailable", f"Backup-Dienst nicht erreichbar ({exc.strerror or exc})") from exc
        self._sock = sock
        self._reader = _LineSocket(sock)

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._reader = None

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        with self._lock:
            for attempt in (1, 2):
                if self._sock is None:
                    self._connect()
                assert self._sock is not None and self._reader is not None
                self._next_id += 1
                req_id = self._next_id
                try:
                    self._sock.settimeout(timeout or self.timeout)
                    self._sock.sendall(encode_message({"id": req_id, "method": method, "params": params or {}}))
                    while True:
                        reply = self._reader.read_message()
                        if reply is None:
                            raise ConnectionResetError("Verbindung geschlossen")
                        if "event" in reply:
                            continue
                        if reply.get("id") == req_id:
                            break
                except (OSError, json.JSONDecodeError) as exc:
                    self._close_locked()
                    if attempt == 1 and not isinstance(exc, socket.timeout):
                        continue
                    raise IpcError("unavailable", f"Verbindung zum Backup-Dienst unterbrochen: {exc}") from exc
                if reply.get("ok"):
                    return reply.get("result")
                error = reply.get("error") or {}
                raise IpcError(str(error.get("code") or "error"), str(error.get("message") or "Unbekannter Fehler"))
        raise IpcError("unavailable", "Backup-Dienst nicht erreichbar")


def subscribe_forever(
    on_event: Callable[[str, Any], None],
    stop_event: threading.Event,
    *,
    path: Path | str | None = None,
    on_state: Callable[[bool, str], None] | None = None,
) -> None:
    """Blocking subscription loop with reconnect; run it in a daemon thread."""
    sock_path = str(path or os.environ.get("BORG_BACKUP_GUI_SOCKET") or DEFAULT_SOCKET)
    delay = 1.0
    while not stop_event.is_set():
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(10)
            sock.connect(sock_path)
            sock.sendall(encode_message({"id": 1, "method": "subscribe", "params": {}}))
            reader = _LineSocket(sock)
            sock.settimeout(600)
            first = reader.read_message()
            if not first or not first.get("ok"):
                reason = str(((first or {}).get("error") or {}).get("message") or "abgelehnt")
                if on_state:
                    on_state(False, reason)
                stop_event.wait(min(delay, 30))
                delay = min(delay * 2, 30)
                continue
            sock.settimeout(1.0)
            if on_state:
                on_state(True, "")
            delay = 1.0
            last_seen = time.monotonic()
            while not stop_event.is_set():
                try:
                    message = reader.read_message()
                except socket.timeout:
                    if time.monotonic() - last_seen > HEARTBEAT_SEC * 3:
                        break
                    continue
                if message is None:
                    break
                last_seen = time.monotonic()
                if "event" in message and message["event"] != "heartbeat":
                    on_event(str(message["event"]), message.get("data"))
        except (OSError, json.JSONDecodeError, IpcError) as exc:
            if on_state:
                on_state(False, str(exc))
        finally:
            try:
                sock.close()
            except OSError:
                pass
        stop_event.wait(min(delay, 30))
        delay = min(delay * 2, 30)


def wait_for_socket(path: Path | str, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(str(path)):
            return True
        time.sleep(0.1)
    return False
