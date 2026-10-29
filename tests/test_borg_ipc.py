from __future__ import annotations

import os
import stat
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import borg_ipc
from borg_ipc import (
    ACTION_CONFIGURE,
    ACTION_RESTORE,
    ACTION_STATUS,
    METHOD_ACTIONS,
    IpcClient,
    IpcError,
    IpcServer,
    Peer,
    PolkitAuthorizer,
)


class FakeAuthorizer:
    def __init__(self, denied: set[str] | None = None) -> None:
        self.denied = denied or set()
        self.checks: list[tuple[Peer, str]] = []

    def check(self, peer: Peer, action: str) -> tuple[bool, str]:
        self.checks.append((peer, action))
        if action in self.denied:
            return False, "abgelehnt"
        return True, ""


@pytest.fixture
def server(tmp_path: Path) -> Iterator[tuple[IpcServer, FakeAuthorizer, list[tuple[str, dict[str, Any], Peer]]]]:
    calls: list[tuple[str, dict[str, Any], Peer]] = []

    def handler(method: str, params: dict[str, Any], peer: Peer) -> Any:
        calls.append((method, params, peer))
        if method == "get_status":
            return {"running": False}
        if method == "run_job":
            raise IpcError("busy", "läuft schon")
        if method == "cancel":
            raise RuntimeError("kaputt")
        return {"echo": params}

    authorizer = FakeAuthorizer(denied={ACTION_RESTORE})
    srv = IpcServer(tmp_path / "run" / "control.sock", handler, authorizer)
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    yield srv, authorizer, calls
    srv.shutdown()
    srv.server_close()


def test_every_method_maps_to_known_action() -> None:
    actions = {borg_ipc.ACTION_STATUS, borg_ipc.ACTION_RUN, ACTION_CONFIGURE, ACTION_RESTORE}
    assert set(METHOD_ACTIONS.values()) <= actions
    assert METHOD_ACTIONS["save_config"] == ACTION_CONFIGURE
    assert METHOD_ACTIONS["extract"] == ACTION_RESTORE
    assert METHOD_ACTIONS["mount"] == ACTION_RESTORE
    assert METHOD_ACTIONS["get_status"] == ACTION_STATUS


def test_policy_file_declares_all_actions() -> None:
    policy = (Path(__file__).resolve().parent.parent / "packaging" / "org.borgbackupgui.policy").read_text()
    for action in set(METHOD_ACTIONS.values()):
        assert f'id="{action}"' in policy


def test_socket_mode_and_peer_credentials(server: Any) -> None:
    srv, _auth, calls = server
    assert stat.S_IMODE(os.stat(srv.socket_path).st_mode) == 0o666
    client = IpcClient(srv.socket_path, timeout=5)
    assert client.call("get_status") == {"running": False}
    peer = calls[0][2]
    assert peer.uid == os.getuid() and peer.pid == os.getpid()
    client.close()


def test_authorization_cached_per_connection(server: Any) -> None:
    srv, auth, _calls = server
    client = IpcClient(srv.socket_path, timeout=5)
    client.call("get_status")
    client.call("get_config")
    assert [a for _p, a in auth.checks] == [ACTION_STATUS]
    client.close()
    other = IpcClient(srv.socket_path, timeout=5)
    other.call("get_status")
    assert [a for _p, a in auth.checks] == [ACTION_STATUS, ACTION_STATUS]
    other.close()


def test_denied_action_is_unauthorized_and_handler_not_called(server: Any) -> None:
    srv, _auth, calls = server
    client = IpcClient(srv.socket_path, timeout=5)
    with pytest.raises(IpcError) as exc:
        client.call("extract", {"archive": "x"})
    assert exc.value.code == "unauthorized"
    assert not any(method == "extract" for method, _p, _peer in calls)
    assert client.call("get_status") == {"running": False}
    client.close()


def test_errors_are_reported(server: Any) -> None:
    srv, _auth, _calls = server
    client = IpcClient(srv.socket_path, timeout=5)
    with pytest.raises(IpcError) as busy:
        client.call("run_job", {"job": "x"})
    assert busy.value.code == "busy"
    with pytest.raises(IpcError) as internal:
        client.call("cancel")
    assert internal.value.code == "internal"
    with pytest.raises(IpcError) as unknown:
        client.call("rm_rf")
    assert unknown.value.code == "unknown_method"
    client.close()


def test_client_reconnects_after_server_drop(server: Any) -> None:
    srv, _auth, _calls = server
    client = IpcClient(srv.socket_path, timeout=5)
    client.call("get_status")
    assert client._sock is not None
    client._sock.close()
    client._sock = None
    assert client.call("get_status") == {"running": False}
    client.close()


def test_unavailable_when_no_socket(tmp_path: Path) -> None:
    with pytest.raises(IpcError) as exc:
        IpcClient(tmp_path / "missing.sock", timeout=1).call("ping")
    assert exc.value.code == "unavailable"


def test_subscription_receives_broadcasts(server: Any) -> None:
    srv, _auth, _calls = server
    received: list[tuple[str, Any]] = []
    states: list[bool] = []
    stop = threading.Event()
    thread = threading.Thread(
        target=borg_ipc.subscribe_forever,
        args=(lambda e, d: received.append((e, d)), stop),
        kwargs={"path": srv.socket_path, "on_state": lambda ok, _m: states.append(ok)},
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 5
    while srv.subscriber_count() == 0 and time.monotonic() < deadline:
        time.sleep(0.02)
    srv.broadcast("log", {"text": "hallo"})
    while not received and time.monotonic() < deadline:
        time.sleep(0.02)
    stop.set()
    thread.join(timeout=5)
    assert states and states[0] is True
    assert received == [("log", {"text": "hallo"})]


def test_polkit_authorizer_allows_root_without_pkcheck() -> None:
    auth = PolkitAuthorizer(pkcheck="/nonexistent/pkcheck")
    assert auth.check(Peer(pid=1, uid=0, gid=0), ACTION_CONFIGURE) == (True, "")
    allowed, reason = auth.check(Peer(pid=os.getpid(), uid=1000, gid=1000), ACTION_CONFIGURE)
    assert not allowed and reason


def test_polkit_authorizer_passes_process_identity(tmp_path: Path) -> None:
    fake = tmp_path / "pkcheck"
    log = tmp_path / "args"
    fake.write_text(f'#!/bin/sh\necho "$@" > {log}\nexit 0\n', encoding="utf-8")
    fake.chmod(0o755)
    peer = Peer(pid=os.getpid(), uid=os.getuid(), gid=os.getgid())
    assert PolkitAuthorizer(pkcheck=str(fake)).check(peer, ACTION_RESTORE)[0]
    args = log.read_text(encoding="utf-8").split()
    assert args[:2] == ["--action-id", ACTION_RESTORE]
    pid, start, uid = args[3].split(",")
    assert int(pid) == os.getpid() and int(uid) == os.getuid()
    assert int(start) == borg_ipc.process_start_time(os.getpid())
    assert "--allow-user-interaction" in args
