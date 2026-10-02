"""GUI-thread task dispatch for the RPC server.

The XML-RPC server runs in its own thread. FreeCAD APIs that touch the GUI
or the document tree must run in the main GUI thread. This module owns the
queue that ferries wrapped callables onto the GUI thread and the helper that
RPC handlers use to invoke them.

Robustness and performance guarantees:

1. Per-call response queues: each ``dispatch_to_gui`` call owns its own
   ``queue.Queue``. A timeout in one call can never corrupt the response for
   a subsequent call.
2. Immediate wake via Qt signal: ``dispatch_to_gui`` emits a signal from the
   RPC thread; the GUI thread processes the task immediately rather than
   waiting for the next 500 ms heartbeat tick. The 500 ms heartbeat is kept
   only as a fallback.
3. Mouse-button guard: ``process_gui_tasks`` skips the current tick while
   mouse buttons are held so MCP tasks cannot interrupt 3D navigation drags.
4. Clean shutdown: the ``_SHUTDOWN`` sentinel sets a flag that suppresses the
   ``finally`` reschedule, so ``stop_rpc_server`` actually stops the loop.
5. Exception isolation: exceptions inside a task are caught, logged, and
   returned as error strings; they never kill the dispatch loop.
6. Stuck-task fail-fast: once a task that already started times out, later GUI
   calls fail immediately until that task returns. Status remains available
   through a GUI-independent RPC method.
7. Timeout counts from task start: the GUI thread runs queued tasks one at a
   time, so a call that arrives while another task is running waits in the
   FIFO first. That wait is budgeted separately (``queue_timeout``) and does
   not consume the task's own ``timeout``. Two concurrent ``execute_code``
   calls therefore each get their full run budget instead of the second one
   being reported stuck because the first was slow.
"""

import itertools
import queue
import sys
import threading
import time
import traceback
from typing import Any, Callable

import FreeCAD
import FreeCADGui
from PySide import QtCore, QtWidgets

from rpc_server.dispatch_health import DispatchHealth, stuck_failure


_rpc_request_queue: "queue.Queue[Any]" = queue.Queue()
_SHUTDOWN = object()
_processing = False  # re-entrancy guard: True while process_gui_tasks is draining
_processing_since: float = 0.0  # wall-clock time when _processing became True
_task_ids = itertools.count(1)

# Heartbeat: the monotonic time of the GUI thread's last dispatch tick. The
# 500 ms chain reschedules on every path, so an IDLE GUI keeps ticking; the
# value stops advancing only when the GUI thread is genuinely not returning,
# which is precisely the signal that separates a wedged thread from a stale
# stuck flag. Recorded in process_gui_tasks - see the comment there for why the
# placement is the whole mechanism.
_last_gui_tick: float = time.monotonic()

# ---- v0.2.1: THE DRAIN, MEASURED SEPARATELY FROM THE HEARTBEAT ------------------------------
# The heartbeat proves the Qt event loop is turning. It does NOT prove the queue is draining:
# process_gui_tasks ticks the heartbeat FIRST and then DEFERS the drain when a mouse button is
# held, a popup is open, or a modal is open. Only the modal was ever surfaced. So a context menu
# left open, or a stuck mouse-button state, produced exactly what ec25-cadman measured on
# 2026-09-30 - heartbeat 0.032 s old, nothing running, stuck_since null, and gui_ping unable to
# START within 120 s. Two numbers that can disagree are far more diagnostic than one that cannot.
_last_drain_tick: float = time.monotonic()   # last time the queue was actually serviced
_drain_defer_reason: str | None = None        # why the most recent tick did not drain
_drain_defer_since: float | None = None       # when the current run of deferrals began
# Unstarted tasks, task_id -> (enqueued_at, operation). Lets a client see "queued and not
# draining" from get_rpc_status, which is answered on the RPC thread and therefore stays
# observable while the GUI queue is the thing that has stalled.
_unstarted: dict[int, tuple[float, str]] = {}
_unstarted_lock = threading.Lock()
# How long the oldest unstarted task may wait before the queue is reported STALLED. Separate from
# any per-call queue budget: this is a status signal, not a timeout.
QUEUE_STALL_AFTER_S = 10.0


# ---- v0.2.3: QT'S MOUSE STATE IS A CACHE, AND IT CAN GO STALE ----------------------------------
# QApplication.mouseButtons() is the state Qt assembled from the press/release events it RECEIVED.
# A release that never reaches this application - measured 2026-10-02, plausibly delivered to the
# Document Recovery dialog as it closed - leaves it reporting a held button indefinitely, and the
# drain then defers forever with nobody touching the mouse: Win32 GetAsyncKeyState read L/R/M all
# UP while get_rpc_status reported mouse_button_held for 200 s and counting. So on Windows the OS
# is asked before deferring. A deferral protects a REAL drag; when the OS says no button is down
# there is no drag to protect, and the queue drains. Off Windows, or if the OS read fails, the
# answer is None and Qt's state is trusted exactly as before.
_VK_MOUSE_BUTTONS = (0x01, 0x02, 0x04, 0x05, 0x06)   # left, right, middle, X1, X2
_mouse_stale_overrides = 0                            # times a stale Qt "held" was overruled


def _os_buttons_down() -> bool | None:
    """True/False from the OS on Windows; None when the OS cannot be asked."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        get = ctypes.windll.user32.GetAsyncKeyState
        return any(get(vk) & 0x8000 for vk in _VK_MOUSE_BUTTONS)
    except Exception:
        return None


def _set_defer(reason: str | None) -> None:
    """Record why a tick deferred the drain, keeping the start of a contiguous run."""
    global _drain_defer_reason, _drain_defer_since
    if reason is None:
        _drain_defer_reason, _drain_defer_since = None, None
    elif reason != _drain_defer_reason:
        _drain_defer_reason, _drain_defer_since = reason, time.monotonic()


def gui_heartbeat_tick() -> float:
    """Monotonic timestamp of the GUI thread's last dispatch tick."""
    return _last_gui_tick


def gui_heartbeat_age() -> float:
    """Seconds since the GUI thread last ticked."""
    return max(0.0, time.monotonic() - _last_gui_tick)


_dispatch_health = DispatchHealth(heartbeat=gui_heartbeat_tick)


class _WakeSignal(QtCore.QObject):
    """Qt signal bridge for cross-thread GUI-task wakeup.

    Must be created on the GUI thread (``init_waker``). Emitting from the
    RPC thread is safe: Qt delivers the connection with ``QueuedConnection``,
    so the slot always fires in the GUI thread's event loop.
    """
    _sig = QtCore.Signal()

    def __init__(self):
        super().__init__()
        self._sig.connect(self._on_wake, QtCore.Qt.QueuedConnection)

    def wake(self) -> None:
        self._sig.emit()

    def _on_wake(self) -> None:
        process_gui_tasks(reschedule=False)


_waker: "_WakeSignal | None" = None


def init_waker() -> None:
    """Create the wake-signal bridge. Call once from the GUI thread."""
    global _waker
    _waker = _WakeSignal()


def cleanup_waker() -> None:
    """Release the wake-signal bridge on server stop."""
    global _waker
    _waker = None


def _flush_gui_events(delay_ms: int = 20) -> None:
    FreeCADGui.updateGui()
    app = QtWidgets.QApplication.instance()
    if app is None:
        return

    # ExcludeUserInputEvents: skip mouse/keyboard events to avoid re-entrancy
    # with ongoing navigation. ExcludeSocketNotifiers keeps network I/O out.
    flags = (
        QtCore.QEventLoop.ExcludeUserInputEvents
        | QtCore.QEventLoop.ExcludeSocketNotifiers
    )
    app.processEvents(flags, delay_ms)
    if delay_ms > 0:
        QtCore.QThread.msleep(delay_ms)
        app.processEvents(flags, delay_ms)


def process_gui_tasks(reschedule: bool = True) -> None:
    """Drain queued GUI-thread callables and optionally reschedule.

    Skips the current tick when any mouse button is held (e.g., 3D navigation
    drag) or when already executing a task (re-entrancy guard). The guard
    prevents ``doc.recompute()`` or ``processEvents()`` inside a task from
    triggering a nested ``process_gui_tasks`` call that corrupts FreeCAD state.

    ``reschedule=False`` is used by the immediate-wake path so it does not
    start a second heartbeat chain alongside the existing 500 ms one.
    """
    global _processing, _processing_since, _last_gui_tick, _last_drain_tick, _mouse_stale_overrides
    if _processing:
        return  # re-entrant call from processEvents inside a task; skip

    # Heartbeat tick. The placement is the mechanism, not a detail:
    #   * AFTER the re-entrancy guard's early return, so a long task calling
    #     updateGui()/processEvents() - which re-enters here and returns at the
    #     line above - CANNOT FORGE A TICK and cannot make a wedged thread look
    #     alive;
    #   * BEFORE the queue-empty check, so an idle GUI still ticks every 500 ms
    #     and an empty queue is never mistaken for a stalled thread.
    _last_gui_tick = time.monotonic()

    shutdown = False
    try:
        if _rpc_request_queue.empty():
            _set_defer(None)
            _last_drain_tick = time.monotonic()   # an empty queue is fully serviced
            return  # nothing queued; skip cursor/status-bar churn on idle heartbeat ticks
        # Each deferral is now RECORDED with its reason. Before v0.2.1 these three returns were
        # silent, which is how a tick could keep the heartbeat fresh while the queue starved.
        if QtWidgets.QApplication.mouseButtons() != QtCore.Qt.NoButton:
            if _os_buttons_down() is False:
                # Qt's cached state is stale: the OS says nothing is held, so nobody is dragging.
                _mouse_stale_overrides += 1
            else:
                _set_defer("mouse_button_held")
                return  # user is dragging; defer to next tick
        if QtWidgets.QApplication.activePopupWidget() is not None:
            _set_defer("popup_open")
            return  # context menu or popup open; defer to next tick
        if QtWidgets.QApplication.activeModalWidget() is not None:
            _set_defer("modal_open")
            return  # modal dialog open; defer to next tick

        _set_defer(None)
        _last_drain_tick = time.monotonic()
        _processing = True
        _processing_since = time.monotonic()
        app = QtWidgets.QApplication.instance()
        try:
            status_bar = FreeCADGui.getMainWindow().statusBar()
        except Exception:
            status_bar = None

        if app is not None:
            app.setOverrideCursor(QtCore.Qt.WaitCursor)
        if status_bar is not None:
            status_bar.showMessage("MCP: processing…")
        try:
            while not _rpc_request_queue.empty():
                task = _rpc_request_queue.get()
                if task is _SHUTDOWN:
                    shutdown = True
                    return
                try:
                    task()
                except Exception as e:
                    FreeCAD.Console.PrintError(
                        f"MCP RPC: unhandled exception in GUI task: {type(e).__name__}: {e}\n"
                        f"{traceback.format_exc()}"
                    )
        finally:
            if app is not None:
                app.restoreOverrideCursor()
            if status_bar is not None:
                status_bar.clearMessage()
    finally:
        _processing = False
        if not shutdown and reschedule:
            QtCore.QTimer.singleShot(500, process_gui_tasks)


def request_shutdown() -> None:
    """Post the sentinel so the next dispatch tick exits without rescheduling."""
    _rpc_request_queue.put(_SHUTDOWN)


def get_queue_status() -> dict[str, Any]:
    """Dispatch-queue observability, answered on the RPC thread (never touches the GUI thread).

    This is the instrument that was missing on 2026-09-30: when the GUI queue stalls, the stalling
    call cannot report on itself, and the async jobs directory never records GUI dispatches. This
    is read here, off the GUI thread, so it stays observable precisely when the queue does not.

    queue_state is ADDITIVE and does not replace DispatchHealth.state. That state keeps its
    healthy/busy/stuck meaning, which describes the RUNNING task; queue_state describes the
    WAITING ones, which is the condition the old model had no word for:
        idle      nothing waiting
        draining  work waiting, and either running or younger than QUEUE_STALL_AFTER_S
        stalled   the oldest waiting task has exceeded QUEUE_STALL_AFTER_S without starting
    """
    now = time.monotonic()
    with _unstarted_lock:
        waiting = list(_unstarted.values())
    oldest_age = max((now - t for t, _ in waiting), default=None)
    oldest_op = (max(waiting, key=lambda w: now - w[0])[1] if waiting else None)
    if not waiting:
        qstate = "idle"
    elif oldest_age is not None and oldest_age > QUEUE_STALL_AFTER_S and not _processing:
        qstate = "stalled"
    else:
        qstate = "draining"
    return {
        "queue_state": qstate,
        "queue_depth": len(waiting),
        "oldest_unstarted_age_s": None if oldest_age is None else round(oldest_age, 3),
        "oldest_unstarted_operation": oldest_op,
        # TWO AGES THAT CAN DISAGREE. A fresh heartbeat with a stale drain is the signature of a
        # deferred drain: the event loop turns, the queue is not serviced.
        "heartbeat_age_s": round(max(0.0, now - _last_gui_tick), 3),
        "drain_age_s": round(max(0.0, now - _last_drain_tick), 3),
        "drain_defer_reason": _drain_defer_reason,
        "drain_deferred_for_s": (None if _drain_defer_since is None
                                 else round(now - _drain_defer_since, 3)),
        "gui_processing": bool(_processing),
        "queue_stall_after_s": QUEUE_STALL_AFTER_S,
        # v0.2.3: drains that went ahead because Qt said "held" and the OS said every button is up
        "mouse_stale_overrides": _mouse_stale_overrides,
    }


def get_dispatch_status() -> dict[str, Any]:
    """Return GUI dispatch health without touching FreeCAD's GUI thread.

    v0.2.1 merges the queue view in, so a single status call answers both "is the running task
    stuck" (state) and "is anything waiting that cannot start" (queue_state).
    """
    snap = _dispatch_health.snapshot()
    snap.update(get_queue_status())
    return snap


def dispatch_to_gui(
    task: Callable[[], Any],
    timeout: float = 60,
    operation_name: str | None = None,
    queue_timeout: float | None = None,
) -> Any:
    """Run ``task`` on the GUI thread and return its result.

    PUBLIC WRAPPER - signature unchanged from 5dbfe2c, including the literal
    ``timeout: float = 60``, which is simultaneously the preserved signature
    default and the budget table's ``gui_default.R``. This module deliberately
    does NOT import the budget table: every call site in rpc_server.py passes an
    explicit ``timeout=R, queue_timeout=Q`` resolved from it, which keeps the
    budget import out of the hot path and keeps tests/test_gui_dispatch.py
    untouched.

    Behaviour for all 17 existing tools is unchanged: the rejection check runs
    first, and the inner call is health-TRACKED.

    Uses a per-call response queue so a timeout in one call never corrupts
    the response for a subsequent call. Wakes the GUI thread immediately via
    a Qt signal instead of waiting for the next 500 ms heartbeat.

    ``timeout`` is the run budget and starts counting when the task actually
    begins on the GUI thread. Time spent queued behind earlier tasks is
    budgeted separately by ``queue_timeout`` (defaults to ``timeout``); if the
    task has not started by then it is cancelled without marking dispatch
    stuck. A call that is already queued when an earlier task becomes stuck
    keeps waiting for its turn; only calls arriving after that point are
    rejected immediately.

    A task already running on the GUI thread cannot be interrupted; if it
    exceeds ``timeout`` it is marked as stuck and subsequent GUI calls fail
    immediately until it returns.

    Returns the task's return value on success, an error string if the task
    raises, or ``{"success": False, "error": ...}`` on timeout.
    """
    rejection = _dispatch_health.rejection()
    if rejection is not None:
        return rejection

    if queue_timeout is None:
        queue_timeout = timeout

    return _enqueue_and_wait(
        task, timeout, queue_timeout, track_health=True, operation_name=operation_name
    )


def _enqueue_and_wait(
    task: Callable[[], Any],
    run_budget: float,
    queue_budget: float,
    *,
    track_health: bool,
    operation_name: str | None = None,
) -> Any:
    """The dispatch mechanism itself: FIFO, per-call response queue, Qt wake.

    Identical for every caller except in ONE respect, ``track_health``:

      * ``True``  - the 17 existing tools, reached through the public wrapper
        above. DispatchHealth.start()/finish()/mark_timed_out() are called, so a
        task that overruns its run budget marks dispatch stuck.
      * ``False`` - health_probe.probe_gui ONLY. No DispatchHealth mutation at
        all, because ``start()`` overwrites ``_active_task_id`` and a
        health-tracked probe would CORRUPT THE STUCK TASK'S IDENTITY - the very
        identity BC-05's reset has to act on. That caller also never reaches the
        rejection check above, because the probe is the one caller allowed to
        look past the flag.

    The two-phase queue/run budgeting, the mouse/popup/modal guards and the
    re-entrancy guard are unchanged for both.
    """
    task_id = next(_task_ids)
    operation = operation_name or getattr(task, "__name__", "GUI operation")
    if operation == "<lambda>":
        operation = "GUI operation"

    response_queue: "queue.Queue[Any]" = queue.Queue(maxsize=1)
    state_lock = threading.Lock()
    started_event = threading.Event()
    started_at: float | None = None
    cancelled = False

    def _wrapped() -> None:
        nonlocal started_at
        with state_lock:
            if cancelled:
                return  # caller timed out and went away; don't run a stale task
            started_at = time.monotonic()
            with _unstarted_lock:
                _unstarted.pop(task_id, None)   # it has started; it is no longer waiting
            if track_health:
                _dispatch_health.start(task_id, operation)
            started_event.set()
        missing = object()
        res = missing
        try:
            try:
                res = task()
            except Exception as e:
                FreeCAD.Console.PrintError(
                    f"MCP RPC: GUI task raised {type(e).__name__}: {e}\n"
                    f"{traceback.format_exc()}"
                )
                res = f"{type(e).__name__}: {e}"
        finally:
            # Publish completion atomically with clearing health, so a deadline
            # racing with completion cannot report a missing successful result.
            with state_lock:
                if track_health:
                    _dispatch_health.finish(task_id)
                if res is not missing:
                    response_queue.put_nowait(res)

    queued_at = time.monotonic()
    with _unstarted_lock:
        _unstarted[task_id] = (queued_at, operation)
    _rpc_request_queue.put(_wrapped)
    if _waker is not None:
        _waker.wake()  # immediate wake via Qt signal (thread-safe)

    # Phase 1: wait for the task to start. Earlier queued tasks run first on
    # the GUI thread; that wait must not eat into this task's run budget.
    queue_deadline = queued_at + queue_budget
    if not started_event.wait(max(0, queue_deadline - time.monotonic())):
        with state_lock:
            cancelled = started_at is None
    if cancelled:
        with _unstarted_lock:
            _unstarted.pop(task_id, None)       # cancelled; no longer waiting
        queued_for = time.monotonic() - queued_at
        # v0.2.1: WHY IT NEVER STARTED, as a structured field rather than prose to string-match.
        # Before this, a harness had to parse detail.error to branch, and alive:false alone could
        # not separate three conditions that demand three different actions:
        #   gui_busy             the GUI thread is running something else      -> WAIT
        #   drain_deferred:<r>   the event loop turns but the drain is held     -> CLEAR <r> (a popup,
        #                        back by a popup, modal or held mouse button       modal or mouse state)
        #   drain_not_servicing  nothing running, nothing deferring, and still  -> HUMAN AT THE GUI
        #                        the queue did not drain
        if _processing:
            queue_cause = "gui_busy"
            busy_for = time.monotonic() - _processing_since
            hint = (
                f" (GUI thread has been busy for {busy_for:.1f}s - for heavy OCCT"
                " geometry consider execute_code_async, which must apply document"
                " writes through its commit() helper)"
            )
        elif _drain_defer_reason is not None:
            queue_cause = "drain_deferred:%s" % _drain_defer_reason
            hint = (" (the GUI event loop is turning but the drain has been deferred by %s for "
                    "%.1fs - clear it at the GUI and the queue will drain)"
                    % (_drain_defer_reason,
                       time.monotonic() - (_drain_defer_since or time.monotonic())))
        else:
            queue_cause = "drain_not_servicing"
            hint = (" (nothing is running and nothing is deferring the drain, yet the queue did "
                    "not drain - this needs a human at the FreeCAD GUI)")
        return {
            "success": False,
            "failure_mode": "queue_never_started",
            "queue_cause": queue_cause,
            "queued_for_s": round(queued_for, 3),
            "queue_budget_s": queue_budget,
            # Nothing was dispatched, so nothing is half-done: the outcome is NOT indeterminate.
            "mutation_outcome": "none - the task never started",
            "error": (
                f"GUI dispatch gave up after {queued_for:.1f}s waiting for "
                f"'{operation}' to start (queue_timeout={queue_budget:g}s){hint}"
            ),
        }

    # Phase 2: count from actual GUI start, even if this RPC thread woke late.
    assert started_at is not None
    run_remaining = max(0, started_at + run_budget - time.monotonic())
    try:
        return response_queue.get(timeout=run_remaining)
    except queue.Empty:
        with state_lock:
            # Completion may have won the race while we acquired the lock.
            try:
                return response_queue.get_nowait()
            except queue.Empty:
                if track_health:
                    stuck = _dispatch_health.mark_timed_out(task_id, run_budget)
                    if stuck is not None:
                        out = stuck_failure(stuck, just_timed_out=True)
                        if isinstance(out, dict):
                            # The task STARTED and has not returned: the thread is wedged in it.
                            # Unlike queue_never_started, the outcome here IS indeterminate - the
                            # task may have mutated the document before stalling.
                            out.setdefault("failure_mode", "thread_wedged")
                            out.setdefault("mutation_outcome",
                                           "INDETERMINATE - the task started and did not return")
                        return out
                return {"success": False, "failure_mode": "run_exceeded",
                        "mutation_outcome": "INDETERMINATE - the task started and overran",
                        "error": f"GUI dispatch timed out after {run_budget}s"}
