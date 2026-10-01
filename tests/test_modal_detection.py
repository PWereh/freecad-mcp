"""The THIRD GUI state: blocked by a modal, which is neither busy nor dead.

Measured on 2026-09-17 by edc25-cadman against the live addon, before it had a name:

    get_rpc_status      healthy, last_gui_heartbeat_age_s = 0.031   <- FRESH
    gui_ping            alive=false, latency 5.016 against a 5.0 cap
    list_documents      TimeoutError
    system.listMethods  Fault - the transport is fine, the METHOD hangs

Qt runs a nested event loop for a modal, so timers keep firing and the heartbeat stays fresh
while queued callables are never serviced. Confirmed from outside the process: the dialog was
"VTK Python module conflict", and FreeCAD's main window reported IsWindowEnabled == false, which
is what a modal does to its owner.

The remedy is what makes the distinction worth code: busy means wait, dead means restart, MODAL
MEANS A HUMAN GOES AND CLICKS SOMETHING. Reporting a modal as "dead" sends someone to restart a
process that did not need restarting and loses whatever the dialog was asking about.

THE SHARPEST TEST HERE IS test_an_unknown_answer_is_not_a_clean_bill: when the probe cannot tell,
it must report known=False and NOT modal=False. A false all-clear on a health probe is the
false-presence direction - the response to it is to leave a real blockage undiagnosed.
"""

from __future__ import annotations

import sys
import types

import pytest


@pytest.fixture
def probe(monkeypatch):
    """The SHIPPED health_probe module, with PySide swapped for a stub."""
    sys.modules.pop("rpc_server.health_probe", None)
    import importlib
    from pathlib import Path
    addon = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP"
    if str(addon) not in sys.path:
        sys.path.insert(0, str(addon))
    monkeypatch.setitem(sys.modules, "rpc_server.budgets",
                        importlib.import_module("freecad_mcp.budgets"))
    module = importlib.import_module("rpc_server.health_probe")
    yield module
    sys.modules.pop("rpc_server.health_probe", None)


def _pyside(app):
    """A PySide stub whose QApplication.instance() returns `app`."""
    qtwidgets = types.SimpleNamespace(
        QApplication=types.SimpleNamespace(instance=lambda: app))
    return types.SimpleNamespace(QtWidgets=qtwidgets)


class _Widget:
    def __init__(self, title):
        self._title = title

    def windowTitle(self):
        return self._title


def test_no_modal_reports_none_and_known(probe, monkeypatch):
    app = types.SimpleNamespace(activeModalWidget=lambda: None)
    monkeypatch.setitem(sys.modules, "PySide", _pyside(app))
    result = probe.active_modal()
    assert result == {"modal": None, "known": True}


def test_a_modal_is_named(probe, monkeypatch):
    app = types.SimpleNamespace(
        activeModalWidget=lambda: _Widget("VTK Python module conflict"))
    monkeypatch.setitem(sys.modules, "PySide", _pyside(app))
    result = probe.active_modal()
    assert result["modal"] is True
    assert result["known"] is True
    assert result["title"] == "VTK Python module conflict"


def test_no_qapplication_is_unknown_not_clear(probe, monkeypatch):
    monkeypatch.setitem(sys.modules, "PySide", _pyside(None))
    result = probe.active_modal()
    assert result["known"] is False
    assert result["modal"] is None


def test_an_unknown_answer_is_not_a_clean_bill(probe, monkeypatch):
    """THE CONTROL THAT MATTERS. A probe that cannot tell must not say "no modal".

    A false all-clear here is worse than no probe: it asserts the GUI is unblocked on the
    strength of an exception, and the reader stops looking. known=False is the honest answer and
    is distinguishable from modal=False, which this asserts explicitly rather than by implication.
    """
    def _boom():
        raise RuntimeError("no GUI in this process")

    app = types.SimpleNamespace(activeModalWidget=_boom)
    monkeypatch.setitem(sys.modules, "PySide", _pyside(app))
    result = probe.active_modal()
    assert result["known"] is False, "an error must report UNKNOWN"
    assert result["modal"] is not False, "an error must NEVER report 'no modal'"
    assert result["why"] == "RuntimeError"


def test_gui_ping_names_the_modal_remedy_only_when_both_hold(probe, monkeypatch):
    """alive=false AND a modal present is the third state; either alone is not.

    Asserted in BOTH directions, because a check that fires on alive=false alone would relabel
    every legitimately busy GUI as blocked - turning "wait" into "go and click something", which
    is the opposite error and just as costly.
    """
    app = types.SimpleNamespace(
        activeModalWidget=lambda: _Widget("VTK Python module conflict"))
    monkeypatch.setitem(sys.modules, "PySide", _pyside(app))

    monkeypatch.setattr(probe, "probe_gui", lambda cap: {"alive": False, "health": {}})
    blocked = probe.gui_ping(5.0)
    assert blocked["blocked_by"] == "modal"
    assert "VTK Python module conflict" in blocked["remedy"]

    # alive, with the same modal present: NOT the third state.
    monkeypatch.setattr(probe, "probe_gui", lambda cap: {"alive": True, "health": {}})
    ok = probe.gui_ping(5.0)
    assert "blocked_by" not in ok

    # not alive, with NO modal: busy or dead, NOT the third state.
    app2 = types.SimpleNamespace(activeModalWidget=lambda: None)
    monkeypatch.setitem(sys.modules, "PySide", _pyside(app2))
    monkeypatch.setattr(probe, "probe_gui", lambda cap: {"alive": False, "health": {}})
    busy = probe.gui_ping(5.0)
    assert "blocked_by" not in busy


def test_the_modal_probe_never_dispatches_to_the_gui_thread(probe):
    """A probe for 'the GUI thread is blocked' must not need the GUI thread.

    Same shape as a gate that cannot fire: if active_modal() went through dispatch_to_gui it
    would hang in exactly the state it exists to detect, and the one answer it could never
    return is the one that matters.
    """
    import inspect
    src = inspect.getsource(probe.active_modal)
    for forbidden in ("dispatch_to_gui", "_enqueue_and_wait", "probe_gui"):
        assert forbidden not in src, (
            f"active_modal references {forbidden!r}: it would block in the state it detects"
        )
