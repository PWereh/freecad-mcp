"""v0.2.1 - the dispatch QUEUE made observable, separately from the heartbeat.

Measured by ec25-cadman, 2026-09-30: gui_ping could not START a no-op within 120 s while
get_rpc_status reported state "healthy", stuck_since null, and a GUI heartbeat 0.032 s old.

The mechanism, read from process_gui_tasks: the heartbeat ticks FIRST, and the drain is then
DEFERRED while a mouse button is held, a popup is open, or a modal is open. Only the modal was ever
surfaced. So the event loop turned, the heartbeat stayed fresh, and the queue starved - and the
model had no word for "enqueued and not starting".

These tests pin the four things v0.2.1 adds:
  1. failure_mode on a queue timeout, with a queue_cause that separates WAIT from CLEAR from HUMAN
  2. the drain age, published beside the heartbeat age, so the two can disagree
  3. queue_state (idle / draining / stalled), additive to DispatchHealth.state
  4. the defer REASON, recorded instead of returned silently
"""
import time
import types

import pytest

from test_gui_dispatch import load_gui_dispatch


def _app(popup=None, modal=None, buttons=0):
    """A QApplication stand-in whose popup / modal / mouse state is chosen per test."""
    return types.SimpleNamespace(
        mouseButtons=staticmethod(lambda: buttons),
        activePopupWidget=staticmethod(lambda: popup),
        activeModalWidget=staticmethod(lambda: modal),
        instance=staticmethod(lambda: types.SimpleNamespace(
            setOverrideCursor=lambda _c: None, restoreOverrideCursor=lambda: None)),
    )


def _defer_with(gd, os_down=None, **state):
    """Queue a dummy so the drain does not short-circuit on an empty queue, then run one tick
    under the given GUI state. Returns nothing; the tick records the defer reason.

    os_down pins the OS mouse answer (v0.2.3). The default None is "OS cannot be asked", which keeps
    Qt's state authoritative; without pinning, a test on Windows would read the real mouse."""
    gd._os_buttons_down = lambda: os_down
    gd.QtWidgets.QApplication = _app(**state)
    gd._rpc_request_queue.put(lambda: None)
    gd.process_gui_tasks(reschedule=False)


# ---- 1. failure_mode / queue_cause ---------------------------------------------------------------

def test_queue_timeout_with_nothing_running_and_nothing_deferring_is_drain_not_servicing():
    """The case that needs a HUMAN: no task running, no deferral recorded, and still no start."""
    with load_gui_dispatch() as gd:
        r = gd.dispatch_to_gui(lambda: None, timeout=0.01, operation_name="noop")
        assert r["success"] is False
        assert r["failure_mode"] == "queue_never_started"
        assert r["queue_cause"] == "drain_not_servicing"
        # nothing was dispatched, so the outcome is NOT indeterminate
        assert "never started" in r["mutation_outcome"]


@pytest.mark.parametrize("state,reason", [
    ({"popup": object()}, "popup_open"),
    ({"modal": object()}, "modal_open"),
    ({"buttons": 1}, "mouse_button_held"),
])
def test_queue_timeout_under_a_deferral_names_the_deferral(state, reason):
    """The case a human can CLEAR at the GUI: the event loop turns but the drain is held back.
    Before v0.2.1 all three of these were silent returns, indistinguishable from each other and
    from drain_not_servicing."""
    with load_gui_dispatch() as gd:
        _defer_with(gd, **state)
        r = gd.dispatch_to_gui(lambda: None, timeout=0.01, operation_name="noop")
        assert r["failure_mode"] == "queue_never_started"
        assert r["queue_cause"] == "drain_deferred:%s" % reason
        assert reason in r["error"]


def test_queue_timeout_message_is_unchanged_for_existing_string_matchers():
    """failure_mode is ADDITIVE. A client that still matches the prose must keep working."""
    with load_gui_dispatch() as gd:
        r = gd.dispatch_to_gui(lambda: None, timeout=0.01, operation_name="stale_call")
        assert "waiting for 'stale_call' to start" in r["error"]


# ---- 2. heartbeat and drain, measured separately ------------------------------------------------

def test_heartbeat_stays_fresh_while_the_drain_goes_stale():
    """THE FINDING. Under a deferral every tick advances the heartbeat and none advances the
    drain. A single 'GUI is alive' number cannot show this; two numbers that can disagree do."""
    with load_gui_dispatch() as gd:
        _defer_with(gd, popup=object())
        time.sleep(0.25)
        gd.process_gui_tasks(reschedule=False)          # another deferred tick
        q = gd.get_queue_status()
        assert q["heartbeat_age_s"] < 0.1               # the event loop is turning
        assert q["drain_age_s"] >= 0.2                  # the queue is not being serviced
        assert q["drain_age_s"] > q["heartbeat_age_s"]
        assert q["drain_defer_reason"] == "popup_open"
        assert q["drain_deferred_for_s"] is not None and q["drain_deferred_for_s"] >= 0.2


def test_clearing_the_deferral_drains_and_resets_both_ages():
    with load_gui_dispatch() as gd:
        _defer_with(gd, popup=object())
        time.sleep(0.15)
        gd.QtWidgets.QApplication = _app()              # popup closed
        gd.process_gui_tasks(reschedule=False)
        q = gd.get_queue_status()
        assert q["drain_defer_reason"] is None
        assert q["drain_deferred_for_s"] is None
        assert q["drain_age_s"] < 0.1


# ---- 3. queue_state, additive to DispatchHealth.state -------------------------------------------

def test_queue_state_idle_when_nothing_waits():
    with load_gui_dispatch() as gd:
        q = gd.get_queue_status()
        assert q["queue_state"] == "idle"
        assert q["queue_depth"] == 0
        assert q["oldest_unstarted_age_s"] is None


def test_queue_state_stalled_when_the_oldest_waiting_task_exceeds_the_threshold():
    """The state the old model had no word for. DispatchHealth.state stays 'healthy' here -
    nothing is RUNNING - which is exactly why a separate queue_state is needed."""
    with load_gui_dispatch() as gd:
        gd.QUEUE_STALL_AFTER_S = 0.05
        with gd._unstarted_lock:
            gd._unstarted[999] = (time.monotonic() - 1.0, "gui_ping")
        q = gd.get_queue_status()
        assert q["queue_state"] == "stalled"
        assert q["queue_depth"] == 1
        assert q["oldest_unstarted_operation"] == "gui_ping"
        assert q["oldest_unstarted_age_s"] >= 1.0
        # the running-task model is untouched and still reads healthy
        assert gd.get_dispatch_status()["state"] == "healthy"
        assert gd.get_dispatch_status()["queue_state"] == "stalled"


def test_queue_state_draining_for_a_young_waiting_task():
    with load_gui_dispatch() as gd:
        gd.QUEUE_STALL_AFTER_S = 60.0
        with gd._unstarted_lock:
            gd._unstarted[7] = (time.monotonic(), "edit_object")
        assert gd.get_queue_status()["queue_state"] == "draining"


def test_a_cancelled_task_is_removed_from_the_waiting_set():
    """A task that times out in the queue must not linger and inflate queue_depth forever."""
    with load_gui_dispatch() as gd:
        gd.dispatch_to_gui(lambda: None, timeout=0.01, operation_name="noop")
        assert gd.get_queue_status()["queue_depth"] == 0


def test_a_started_task_is_removed_from_the_waiting_set():
    with load_gui_dispatch() as gd:
        gd._rpc_request_queue.put(lambda: None)
        with gd._unstarted_lock:
            gd._unstarted.clear()
        gd.process_gui_tasks(reschedule=False)
        assert gd.get_queue_status()["queue_depth"] == 0


# ---- v0.2.3: a STALE Qt "button held" must not starve the queue ---------------------------------

def _drained(gd):
    ran = []
    gd._rpc_request_queue.put(lambda: ran.append(1))
    gd.process_gui_tasks(reschedule=False)
    return bool(ran)


def test_stale_qt_mouse_state_is_overruled_when_the_os_says_no_button_is_down():
    """Measured 2026-10-02: Qt reported mouse_button_held for 200 s while Win32 said L/R/M all UP,
    and the drain deferred forever. The OS answer wins; the drain runs and the override is counted."""
    with load_gui_dispatch() as gd:
        gd.QtWidgets.QApplication = _app(buttons=1)
        gd._os_buttons_down = lambda: False
        assert _drained(gd) is True
        assert gd._mouse_stale_overrides == 1
        assert gd._drain_defer_reason is None


def test_a_real_held_button_still_defers():
    with load_gui_dispatch() as gd:
        gd.QtWidgets.QApplication = _app(buttons=1)
        gd._os_buttons_down = lambda: True
        assert _drained(gd) is False
        assert gd._drain_defer_reason == "mouse_button_held"
        assert gd._mouse_stale_overrides == 0


def test_when_the_os_cannot_be_asked_qt_is_trusted_as_before():
    with load_gui_dispatch() as gd:
        gd.QtWidgets.QApplication = _app(buttons=1)
        gd._os_buttons_down = lambda: None
        assert _drained(gd) is False
        assert gd._drain_defer_reason == "mouse_button_held"


def test_the_override_count_is_reported_in_status():
    with load_gui_dispatch() as gd:
        gd.QtWidgets.QApplication = _app(buttons=1)
        gd._os_buttons_down = lambda: False
        _drained(gd)
        assert gd.get_dispatch_status()["mouse_stale_overrides"] == 1
