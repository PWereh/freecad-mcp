"""v0.2.4 - a bridge re-registers when the addon it registered with is gone.

Measured 2026-10-02: after a FreeCAD restart, a running freecad-mcp shim kept serving every call
while live_bridges read 0 for it. hello() ran only on first connect, the heartbeat thread had
returned on its first failure, and the restarted addon answered every renewal False. rxCAD's G2
under-counted - the dangerous direction. These tests fail against v0.2.3 (ablated).
"""
import threading

import pytest

from freecad_mcp import server


class FakeConnection:
    """Records the connect sequence; hands out a fresh token per hello()."""

    made: list["FakeConnection"] = []

    def __init__(self, host, port):
        self.bridge_token = None
        self.bridge_lease_s = 120.0
        self.budgets, self.bridge_contract, self.jobs_dir = {}, "v0.2.0", None
        self.calls: list[str] = []
        FakeConnection.made.append(self)

    def ping(self):
        self.calls.append("ping")
        return True

    def get_rpc_status(self):
        self.calls.append("status")
        return {}

    def apply_status(self, status):
        self.calls.append("apply_status")  # the contract assertion lives here
        return status

    def hello(self):
        self.calls.append("hello")
        self.bridge_token = "tok-%d" % len(FakeConnection.made)
        return {"live_bridges": 1}

    def heartbeat(self):
        return True

    def disconnect(self):
        self.calls.append("disconnect")


@pytest.fixture
def fresh(monkeypatch):
    FakeConnection.made = []
    monkeypatch.setattr(server, "FreeCADConnection", FakeConnection)
    monkeypatch.setattr(server.state, "freecad_connection", None)
    # never start a real heartbeat thread; each test sets the thread state it needs
    monkeypatch.setattr(server, "_start_heartbeat", lambda conn: None)
    monkeypatch.setattr(server, "_heartbeat_thread", None)
    yield
    server.state.freecad_connection = None


def _alive_thread():
    t = threading.Thread(target=threading.Event().wait, args=(5,), daemon=True)
    t.start()
    return t


def test_a_live_registration_is_reused(fresh, monkeypatch):
    first = server.get_freecad_connection()
    monkeypatch.setattr(server, "_heartbeat_thread", _alive_thread())
    assert server.get_freecad_connection() is first
    assert len(FakeConnection.made) == 1


def test_a_dead_heartbeat_thread_reconnects_and_re_registers(fresh):
    """FreeCAD shut down: the heartbeat thread returned on its first failure."""
    first = server.get_freecad_connection()
    assert first.bridge_token
    # _heartbeat_thread is None/dead -> registration lost
    second = server.get_freecad_connection()
    assert second is not first
    assert "disconnect" in first.calls
    # the WHOLE sequence re-ran, contract assertion included, then a new hello
    assert second.calls == ["ping", "status", "apply_status", "hello"]
    assert second.bridge_token != first.bridge_token


def test_an_unrenewed_heartbeat_flags_and_reconnects(fresh, monkeypatch):
    """FreeCAD restarted between beats: the addon answered, but no longer knows the token."""
    first = server.get_freecad_connection()
    monkeypatch.setattr(server, "_heartbeat_thread", _alive_thread())
    first.registration_lost = True
    second = server.get_freecad_connection()
    assert second is not first and "hello" in second.calls


def test_an_unregistered_bridge_is_never_sent_round_a_reconnect_loop(fresh):
    """An addon without hello() leaves bridge_token None; that is not 'lost'."""
    first = server.get_freecad_connection()
    first.bridge_token = None
    assert server.get_freecad_connection() is first


def test_the_heartbeat_flags_an_unknown_token(monkeypatch):
    """The loop itself: renewed False sets registration_lost and stops the thread."""
    conn = FakeConnection("h", 1)
    conn.bridge_token = "tok"
    conn.heartbeat = lambda: False
    monkeypatch.setattr(server.state, "freecad_connection", conn)
    monkeypatch.setattr(server, "_heartbeat_thread", None)
    conn.bridge_lease_s = 0.0          # interval floors at 5 s; shorten the wait below
    real_wait = server._heartbeat_stop.wait
    monkeypatch.setattr(server._heartbeat_stop, "wait", lambda _t: False)
    try:
        server._start_heartbeat(conn)
        server._heartbeat_thread.join(timeout=5)
        assert not server._heartbeat_thread.is_alive()
        assert getattr(conn, "registration_lost", False) is True
    finally:
        monkeypatch.setattr(server._heartbeat_stop, "wait", real_wait)
        server.state.freecad_connection = None
