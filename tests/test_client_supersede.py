"""v0.3.1 - a reconnect in one MCP client never counts as two bridges.

Measured 2026-10-02: /mcp reconnect left bridge 3780 running under claude.exe 2500 beside the new
bridge 19900 under the same claude.exe; both stayed registered and rxCAD's G2 read 2 and halted.
"""
import xmlrpc.client

import pytest

from freecad_mcp import client_identity, freecad_client

from test_bridge_presence import bridges  # noqa: F401  (fixture: the shipped bridges.py + FakeClock)


# ---- addon: supersede by client -----------------------------------------------------------------

def test_a_new_bridge_of_the_same_client_displaces_the_old(bridges) -> None:  # noqa: F811
    old = bridges.hello(3780, instance="a", client_pid=2500)
    new = bridges.hello(19900, instance="b", client_pid=2500)
    assert bridges.count() == 1
    assert new["displaced_same_client"] == [3780]
    assert [b["pid"] for b in bridges.live()] == [19900]
    # the displaced bridge's next renewal is refused, so under v0.2.4 its heartbeat stops
    assert bridges.renew(old["token"]) is False


def test_two_different_clients_still_count_as_two(bridges) -> None:  # noqa: F811
    """The case G2 exists to catch must survive the change."""
    bridges.hello(3780, instance="a", client_pid=2500)
    bridges.hello(19900, instance="b", client_pid=9999)
    assert bridges.count() == 2


def test_without_a_client_pid_nothing_is_displaced(bridges) -> None:  # noqa: F811
    """An older client sends no client_pid; behaviour is exactly as before."""
    bridges.hello(3780, instance="a")
    r = bridges.hello(19900, instance="b")
    assert bridges.count() == 2 and r["displaced_same_client"] == []


def test_the_client_pid_is_reported_for_humans(bridges) -> None:  # noqa: F811
    bridges.hello(19900, instance="b", client_pid=2500)
    assert bridges.live()[0]["client_pid"] == 2500


# ---- client: which process is the client --------------------------------------------------------

SCRIPTS = r"c:\proj\.venv\scripts"
PATHS = {
    19900: r"C:\uv\python\cpython-3.12.7\python.exe",          # this interpreter
    17028: r"C:\proj\.venv\Scripts\python.exe",               # venv redirector  -> skip
    13716: r"C:\proj\.venv\Scripts\freecad-mcp.exe",          # launcher         -> skip
    2500: r"C:\Users\x\.local\bin\claude.exe",                # the client
}
PARENTS = {19900: 17028, 17028: 13716, 13716: 2500, 2500: 16248}


def _walk(start, paths=PATHS, parents=PARENTS):
    return client_identity._first_non_venv_ancestor(
        start, parents, lambda pid: paths.get(pid), SCRIPTS)


def test_the_walk_skips_the_venv_redirector_and_the_launcher(monkeypatch) -> None:
    monkeypatch.setattr(client_identity.os.path, "realpath", lambda p: p)
    assert _walk(PARENTS[19900]) == 2500


def test_a_client_named_python_outside_the_venv_is_not_skipped(monkeypatch) -> None:
    """A name rule would skip it; the path rule must not."""
    monkeypatch.setattr(client_identity.os.path, "realpath", lambda p: p)
    paths = {**PATHS, 2500: r"C:\tools\python.exe"}
    assert _walk(PARENTS[19900], paths=paths) == 2500


def test_an_unreadable_ancestor_is_taken_as_the_client(monkeypatch) -> None:
    monkeypatch.setattr(client_identity.os.path, "realpath", lambda p: p)
    paths = {k: v for k, v in PATHS.items() if k != 13716}
    assert _walk(PARENTS[19900], paths=paths) == 13716


def test_client_pid_never_raises(monkeypatch) -> None:
    monkeypatch.setattr(client_identity, "_windows_parent_table", lambda: 1 / 0)
    monkeypatch.setattr(client_identity.sys, "platform", "win32")
    assert client_identity.client_pid() is None


# ---- client: older addons -----------------------------------------------------------------------

class _Proxy:
    def __init__(self, old_addon: bool):
        self.old_addon, self.calls = old_addon, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def hello(self, *args):
        self.calls.append(args)
        if self.old_addon and len(args) > 4:
            raise xmlrpc.client.Fault(1, "TypeError: hello() takes from 2 to 5 positional arguments")
        return {"token": "t", "lease_s": 120.0}


@pytest.mark.parametrize("old_addon", [False, True])
def test_hello_sends_client_pid_and_falls_back_for_an_older_addon(monkeypatch, old_addon) -> None:
    proxy = _Proxy(old_addon)
    conn = freecad_client.FreeCADConnection.__new__(freecad_client.FreeCADConnection)
    conn._timeout = 5.0
    conn._make_proxy = lambda timeout: proxy
    monkeypatch.setattr(client_identity, "client_pid", lambda: 2500)
    reply = conn.hello()
    assert reply["token"] == "t" and conn.bridge_token == "t"
    assert proxy.calls[0][-1] == 2500                       # client_pid sent first
    assert len(proxy.calls) == (2 if old_addon else 1)      # one retry, four args, for old addons
    if old_addon:
        assert len(proxy.calls[1]) == 4
