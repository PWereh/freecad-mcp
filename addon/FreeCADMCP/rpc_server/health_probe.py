"""Liveness probe (BC-04) and bounded stuck-flag reset (BC-05).

THE LOAD-BEARING LINE OF THIS MODULE: ``probe_gui`` does NOT go through
``dispatch_to_gui``. The first two statements of the public wrapper are

    rejection = _dispatch_health.rejection()
    if rejection is not None: return rejection

so a probe routed through it is rejected before it ever reaches the GUI thread
and can never return ``alive: true`` in the stuck state - the ONLY state BC-05
needs it for. ``probe_gui`` calls the inner ``_enqueue_and_wait`` DIRECTLY:
same ``_rpc_request_queue``, same per-call response queue, same ``_waker``
wake, but

  * NO rejection check - the probe is the one caller allowed past the flag;
  * NO ``DispatchHealth.start()`` / ``finish()`` / ``mark_timed_out()`` - a
    probe must not perturb the state it measures. ``start()`` overwrites
    ``_active_task_id`` and would CORRUPT THE STUCK TASK'S IDENTITY. The probe
    stays out of DispatchHealth entirely and reads the snapshot only to report.

``alive`` means exactly "a no-op completed on the GUI thread within cap". A
legitimately busy GUI therefore returns ``alive: false`` with
``health.state == "busy"``, WHICH IS NOT THE SAME AS DEAD; dead is
``last_gui_heartbeat_age_s`` growing without bound.

gui_dispatch is resolved lazily through sys.modules rather than imported at
module scope: the preserved test modules load gui_dispatch under stubs and swap
``sys.modules["rpc_server.gui_dispatch"]`` per test, and a module-scope import
here would pin the first one loaded for the rest of the session.
"""

import importlib
import sys
import time
from typing import Any

from rpc_server import budgets


def _gui():
    """The live rpc_server.gui_dispatch module. See the module docstring."""
    module = sys.modules.get("rpc_server.gui_dispatch")
    if module is None:
        module = importlib.import_module("rpc_server.gui_dispatch")
    return module


def _noop() -> bool:
    """The cheapest possible GUI-thread task: proof the thread ran something."""
    return True


def probe_gui(cap: float = 5.0) -> dict[str, Any]:
    """Run a no-op on the GUI thread under its own short budget. BYPASS path.

    Returns ``{alive, latency_s, health, last_gui_heartbeat_age_s, detail}``.
    Never mutates DispatchHealth.
    """
    gui = _gui()
    try:
        cap_s = float(cap)
    except (TypeError, ValueError):
        cap_s = float(budgets.budget_for("probe")["R"])
    if cap_s <= 0:
        cap_s = float(budgets.budget_for("probe")["R"])

    started = time.monotonic()
    # cap is passed as BOTH run and queue budget, so the probe's total wall cost
    # is bounded by the structural worst case cap + cap and in practice is
    # cap + epsilon: a no-op cannot outlive its own start.
    outcome = gui._enqueue_and_wait(
        _noop, cap_s, cap_s, track_health=False, operation_name="gui_ping"
    )
    latency = time.monotonic() - started

    health = gui.get_dispatch_status()
    return {
        "success": True,
        "alive": outcome is True,
        "latency_s": round(latency, 3),
        "cap_s": cap_s,
        "health": health,
        "last_gui_heartbeat_age_s": health.get("last_gui_heartbeat_age_s"),
        "detail": None if outcome is True else outcome,
    }


def active_modal() -> dict[str, Any]:
    """Is a MODAL DIALOG holding the GUI? Answered, not inferred.

    THE THIRD STATE. The module note above documents two: alive=false with
    health.state "busy" (legitimately busy) and last_gui_heartbeat_age_s growing
    without bound (dead). A modal is NEITHER, and it was measured on this machine
    on 2026-09-17 by edc25-cadman before it had a name:

        get_rpc_status   healthy, last_gui_heartbeat_age_s = 0.031   <- FRESH
        gui_ping         alive=false, latency 5.016 against a 5.0 cap
        list_documents   TimeoutError
        system.listMethods  Fault - so the transport is fine, the METHOD hangs

    Qt runs a NESTED EVENT LOOP for a modal dialog. Timers keep firing, so the
    heartbeat stays fresh and the tracker sees a live GUI, while queued callables
    are never serviced. Confirmed from outside the process: the dialog was
    "VTK Python module conflict" and FreeCAD's main window reported
    IsWindowEnabled == false, which is what a modal does to its owner.

    THE REMEDY DIFFERS FROM BOTH OTHERS, which is why the state needs a name:
        busy   -> wait
        dead   -> restart
        modal  -> A HUMAN GOES AND CLICKS SOMETHING

    QApplication.activeModalWidget() answers it definitively and costs nothing.
    It is read WITHOUT dispatching to the GUI thread, deliberately: a probe for
    "the GUI thread is blocked" that needs the GUI thread would be the same
    mistake as a gate that cannot fire.
    """
    try:
        from PySide import QtWidgets
        app = QtWidgets.QApplication.instance()
        if app is None:
            return {"modal": None, "known": False, "why": "no QApplication"}
        widget = app.activeModalWidget()
        if widget is None:
            return {"modal": None, "known": True}
        return {"modal": True, "known": True,
                "title": widget.windowTitle(),
                "type": type(widget).__name__}
    except Exception as exc:
        # Never raise from a health probe - an unknown answer is reported AS
        # unknown, never as "no modal", which would be a false all-clear.
        return {"modal": None, "known": False, "why": type(exc).__name__}


def gui_ping(cap: float = 5.0) -> dict[str, Any]:
    """MCP tool 18. ``alive`` == "a no-op completed on the GUI thread within cap"."""
    result = probe_gui(cap)
    modal = active_modal()
    result["modal"] = modal
    if not result.get("alive") and modal.get("modal"):
        # The one combination the two documented states do not cover.
        result["blocked_by"] = "modal"
        result["remedy"] = ("a human must dismiss the dialog %r - this is neither busy "
                            "nor dead, and a restart is the wrong remedy"
                            % (modal.get("title") or "(untitled)",))
    return result


def reset_dispatch_health(force: bool = False) -> dict[str, Any]:
    """MCP tool 19. Clear a STALE stuck flag; report a REAL wedge unchanged.

    Calls ``probe_gui`` - never the ``gui_ping`` tool, and never
    ``dispatch_to_gui``, either of which would be rejected by the very flag this
    is trying to clear. Clearing goes through ``DispatchHealth.reset_stale()``,
    which clears ONLY when ``_timed_out`` is set.

    OPEN-9 (build blueprint s13, carried from manifest CONFLICT-06, UNRESOLVED):
        ``force`` STILL HAS NO SPECIFIED OBSERVABLE EFFECT DISTINCT FROM
        ``force=False``. Both branches require a successful ping; the
        architecture describes force only as clearing "a flag whose owning task
        id is gone", which no document defines in testable terms. Nothing is
        invented here: force is accepted, echoed, and changes nothing. The
        parameter is on a NEW tool, so there is no compatibility risk either
        way - it needs a definition or removal, and a human decides which.
    """
    gui = _gui()
    cap = float(budgets.budget_for("probe")["R"])
    probe = probe_gui(cap)
    health = probe["health"]

    if not probe["alive"]:
        return {
            "success": True,
            "reset": False,
            "reason": "GUI thread not answering",
            "stuck_since": health.get("stuck_since"),
            "heartbeat_age_s": health.get("last_gui_heartbeat_age_s"),
            "force": bool(force),
            "health": health,
            "probe": {"alive": False, "latency_s": probe["latency_s"], "cap_s": cap},
        }

    cleared = gui._dispatch_health.reset_stale()
    if cleared is None:
        return {
            "success": True,
            "reset": False,
            "reason": "dispatch health is not stuck; nothing to clear",
            "force": bool(force),
            "health": gui.get_dispatch_status(),
        }
    return {
        "success": True,
        "reset": True,
        "cleared_task_id": cleared.get("task_id"),
        "cleared_operation": cleared.get("operation"),
        "was_stuck_for_s": cleared.get("was_stuck_for_s"),
        "force": bool(force),
        "health": gui.get_dispatch_status(),
    }
