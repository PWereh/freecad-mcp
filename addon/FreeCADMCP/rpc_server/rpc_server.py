import FreeCAD
import FreeCADGui

import ast
import contextlib
import base64
import io
import os
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any
from xmlrpc.client import Fault

from PySide import QtCore

from rpc_server import async_jobs, bridges, budgets, connections, health_probe
from rpc_server.commands import register_commands, schedule_toggle_sync
from rpc_server.fem_executor import run_fem_analysis as _run_fem_analysis
from rpc_server.gui_dispatch import (
    cleanup_waker,
    dispatch_to_gui,
    get_dispatch_status,
    init_waker,
    process_gui_tasks,
    request_shutdown,
)
from rpc_server.ip_filter import FilteredXMLRPCServer, validate_allowed_ips
from rpc_server.object_factory import create_object_gui, edit_object_gui
from rpc_server.parts_library import get_parts_list, insert_part_from_library
from rpc_server.property_mapper import Object
from rpc_server.serialize import serialize_object
from rpc_server.settings import load_settings, save_settings
from rpc_server.view_manager import save_active_screenshot

rpc_server_thread = None
rpc_server_instance = None
_stop_thread = None  # drains shutdown off the GUI thread; see stop_rpc_server

# BC-08's canary. Reported by get_rpc_status so a client can halt against a
# v0.1.23 addon WITH THE VERSION NAMED instead of failing opaquely later.
BRIDGE_CONTRACT = "v0.2.0"

# Persistent namespace for execute_code / execute_code_async. A dedicated dict
# (instead of this module's globals()) keeps user code from shadowing server
# internals like dispatch_to_gui while preserving the documented pattern of
# sharing module-level variables between successive calls.
_EXEC_NAMESPACE: dict[str, Any] = {
    "FreeCAD": FreeCAD,
    "App": FreeCAD,
    "FreeCADGui": FreeCADGui,
    "Gui": FreeCADGui,
}
_async_execution = threading.local()

# The job store, its lock and the keep-20 history policy moved to
# rpc_server/async_jobs.py, which also owns the BC-01 schema and its state
# machine. The WORKER LAUNCH, the time.time() stamps and the dispatch_to_gui
# calls stay here on purpose: tests/test_rpc_handlers.py monkeypatches
# rpc_module.threading, rpc_module.time and rpc_module.dispatch_to_gui, so those
# names must keep resolving in THIS module's globals at call time.
#
# The store is process-global, while this module is re-executed per test by the
# preserved rpc_module fixture; clearing at import keeps a fresh store per load
# and is a no-op in the addon, which imports this module exactly once.
async_jobs.reset()

# Length of the code excerpt kept on a job record.
#
# OPEN-6 (build blueprint s13, UNRESOLVED - a human must confirm):
#     Two disagreements at once. (a) The baseline writes the field as ``code``
#     (rpc_server.py:324); the BC-01 schema names it ``code_preview``. That is a
#     RENAME, and any consumer reading job["code"] would break silently at the
#     swap, so v0.2.x writes BOTH - code_preview canonical, code retained as an
#     alias - and the blueprint's recommendation is to drop ``code`` in v0.3.0.
#     (b) The truncation length disagrees: the baseline previews 200 chars
#     (rpc_server.py:283), the schema says 120. The PRESERVED 200 is kept here
#     and the schema comment is the thing that should be corrected.
_CODE_PREVIEW_CHARS = 200


def _record_job(job_id: str, **fields: Any) -> None:
    async_jobs.record_job(job_id, **fields)


def _gui_default() -> dict[str, float]:
    """``timeout``/``queue_timeout`` kwargs for a standard GUI tool.

    Resolved from the ADDON's budget table on every call. At the shipped table
    this is 60/60 - identical to ``dispatch_to_gui``'s preserved signature
    default, which is why gui_dispatch.py never needs to import the table.
    """
    entry = budgets.budget_for("gui_default")
    return {"timeout": entry["R"], "queue_timeout": entry["Q"]}


def _ok(res) -> bool:
    """True when a GUI-thread handler returned success."""
    return res is True


def _err(res) -> dict:
    """Convert any non-True result (error string or timeout dict) to a failure dict."""
    if isinstance(res, dict):
        return res
    return {"success": False, "error": str(res)}


def _commit_async(fn: Callable[[], Any], timeout: float | None = None) -> Any:
    """Run an async script's document/view writes on the GUI thread.

    ``timeout=None`` means the budget table's ``commit.R``, which ships at 120 -
    exactly the literal written into execute_code_async's preserved public
    docstring (``commit(fn, timeout=120)``). commit therefore trips ITS OWN
    budget, not gui_default's, and an operator who raises it with
    set_gui_budget("commit", ...) moves both together.
    """
    if not getattr(_async_execution, "active", False):
        raise RuntimeError("commit() is only available inside execute_code_async workers")
    entry = budgets.budget_for("commit")
    run_budget = entry["R"] if timeout is None else float(timeout)
    queue_budget = entry["Q"] if timeout is None else float(timeout)
    res = dispatch_to_gui(
        lambda: (fn(),), timeout=run_budget, queue_timeout=queue_budget,
        operation_name="async_commit",
    )
    if isinstance(res, tuple):
        return res[0]
    error = _err(res)
    raise RuntimeError(f"commit() failed: {error['error']}")


# Keep one live namespace, including the helper: saved functions retain this
# dictionary as their globals. The thread-local guard prevents a GUI callback
# or synchronous script from waiting on its own GUI thread through commit().
_EXEC_NAMESPACE["commit"] = _commit_async

# BC-01/BC-07 helpers, injected beside commit into the SAME live namespace:
# emit / result / plan / chunk / commit_many. They resolve the running job from
# a per-thread binding, so saved functions keep working across async calls and a
# payload cannot address another job's record. execute_code_async's MCP
# signature is unchanged - these live in the payload's namespace, not in the
# tool's input schema.
_EXEC_NAMESPACE.update(async_jobs.namespace(_commit_async))


def _exec_async_payload(code: str, namespace: dict[str, Any]) -> Any:
    """exec the payload and return the value of a trailing expression, if any.

    ``exec`` has no return value, so "the payload's own return value is stored
    when result() was not called" (BC-01) is implemented by evaluating a final
    expression statement separately. Semantics are unchanged - that expression
    was already evaluated by exec - and any failure to split falls back to the
    baseline's plain ``exec(code, namespace)``.
    """
    head_code = tail_code = None
    try:
        body = ast.parse(code).body
        if body and isinstance(body[-1], ast.Expr):
            head_code = compile(
                ast.Module(body=body[:-1], type_ignores=[]), "<string>", "exec"
            )
            tail_code = compile(ast.Expression(body[-1].value), "<string>", "eval")
    except SyntaxError:
        raise
    except Exception:
        head_code = tail_code = None

    # ONLY the splitting is guarded above. Running the payload is deliberately
    # OUTSIDE that try: an exception raised BY THE PAYLOAD must propagate to the
    # worker, never be caught here and retried through the fallback - which
    # would execute the whole payload a second time, with every side effect it
    # already had.
    if tail_code is not None:
        exec(head_code, namespace)
        return eval(tail_code, namespace)
    exec(code, namespace)
    return None


def _query_on_gui(task: Callable[[], Any], operation: str) -> Any:
    """Preserve query results while reporting dispatch failures as RPC faults."""
    # A tuple distinguishes valid results (including None and empty lists)
    # from the dispatcher's error strings and failure dictionaries.
    res = dispatch_to_gui(lambda: (task(),), operation_name=operation, **_gui_default())
    if isinstance(res, tuple):
        return res[0]
    error = _err(res)
    code = error.get("code", "GUI_DISPATCH_FAILED")
    raise Fault(1, f"{code}: {error['error']}")


class FreeCADRPC:
    """RPC server for FreeCAD"""

    # ``TIMEOUT = 60`` was declared here at 5dbfe2c and NEVER REFERENCED. It is
    # deleted rather than carried, so a later reader cannot mistake it for the
    # source of a run budget; the real per-tool budgets live in
    # rpc_server/budgets.py.

    # The preserved literal, and the value the budget table ships for
    # execute_code. An INSTANCE attribute set on an RPC object overrides the
    # table for that object - see _execute_code_budgets.
    EXECUTE_CODE_TIMEOUT = 90  # GUI-thread execution; use execute_code_async for heavy OCCT ops

    def ping(self):
        return True

    # --- OPEN-3 option (c): bridge presence -------------------------------
    # RESOLVED 2026-09-17: measure LIVE BRIDGES, NOT PROCESSES. These three
    # methods are the only additions to the RPC surface in v0.2.1, and they are
    # additive: a client that never calls hello() behaves exactly as before and
    # simply does not appear in the count. See rpc_server/bridges.py for why a
    # pid is never asked whether it is alive.

    def hello(self, pid, instance=None, lease_s=None, contract=None):
        """Register this bridge. Returns a token to stamp on later requests."""
        return bridges.hello(pid, instance=instance, lease_s=lease_s,
                             contract=contract)

    def heartbeat(self, token):
        """Renew an idle bridge's lease. Busy bridges renew via the header."""
        return {"success": True, "renewed": bridges.renew(token),
                "live_bridges": bridges.count()}

    def goodbye(self, token):
        """Deregister on clean shutdown. A hard kill expires instead."""
        return bridges.goodbye(token)

    def live_bridges(self):
        """The list behind the count, with ages, so a human can judge it."""
        return {"success": True, "count": bridges.count(),
                "bridges": bridges.live()}

    def _execute_code_budgets(self) -> tuple[float, float]:
        """Resolve execute_code's (run, queue) budget.

        The table is the source of truth, EXCEPT when an instance attribute
        named EXECUTE_CODE_TIMEOUT has been set on this object, which then wins
        for both budgets exactly as the baseline's single ``timeout=`` argument
        did.

        OPEN-8 (build blueprint s13, UNRESOLVED - a human must confirm):
            The blueprint expected this hook to become a NO-OP once execute_code
            read R from the table, which would silently weaken two preserved
            modules: tests/test_client_timeouts.py:19 (``rpc.EXECUTE_CODE_TIMEOUT
            = 1``, whose docstring claims "with shortened budgets") and
            tests/test_rpc_handlers.py:315 (``rpc.EXECUTE_CODE_TIMEOUT = 0.5``,
            which needs a stuck dispatch inside 2 s and would otherwise wait for
            the table's 90). The hook is KEPT LIVE here so neither test is
            weakened and neither is edited. The open question the blueprint
            raises stands: whether a per-instance override should exist at all,
            or whether those two lines should instead monkeypatch the budget
            table. A human decides; nothing here depends on the answer.
        """
        entry = budgets.budget_for("execute_code")
        if "EXECUTE_CODE_TIMEOUT" in vars(self):
            override = float(self.EXECUTE_CODE_TIMEOUT)
            return override, override
        return float(entry["R"]), float(entry["Q"])

    def get_rpc_status(self) -> dict[str, Any]:
        """Report server and GUI-dispatch health without using the GUI thread.

        Remains answered OFF the GUI thread - that is a feature, and ``gui_ping``
        exists so nobody misreads it. Every v0.2.0 field below is ADDITIVE, so a
        v0.1.23 client keeps working against this addon.
        """
        return {
            "success": True,
            "rpc_server": "running",
            "gui_dispatch": get_dispatch_status(),
            "async_jobs_running": async_jobs.running_ids(),
            # --- v0.2.0, additive -------------------------------------------
            "bridge_contract": BRIDGE_CONTRACT,
            # The one copy of the budget table. The client DERIVES its socket
            # timeout S = Q + R + M from this echo; nothing is pushed and
            # nothing is compared across the boundary.
            "budgets": budgets.get_budgets(),
            # OPEN-3 (build blueprint s13, UNRESOLVED - a human must pick):
            #     This is a count of connections OPEN RIGHT NOW, taken over the
            #     handler's setup()/finish() lifetime. Under
            #     SimpleXMLRPCRequestHandler's HTTP/1.0 the server closes after
            #     every response, so it reads ~0 at idle EVEN WITH A LIVE
            #     BRIDGE. It is a concurrency gauge, NOT a bridge-presence
            #     gauge: BC-06's "connected_clients == 0 after the parent dies"
            #     passes vacuously against it, and rxCAD's G2 halt (count > 1)
            #     is not measurable from it. See rpc_server/connections.py for
            #     the three options and why option (a) is the floor built here.
            "connected_clients": connections.count(),
            # OPEN-3 RESOLVED 2026-09-17 - option (c), the number that answers
            # the question. A bridge is live iff it has a call in flight or its
            # hello() lease is unexpired. NOT whether its pid exists: the three
            # orphaned freecad-mcp.exe OPEN-13 measured were live processes and
            # zero bridges. BC-06 ("0 after the parent dies") is now
            # non-vacuous, because a live bridge reads 1. rxCAD's G2 halt reads
            # live_bridges > 1.
            "live_bridges": bridges.count(),
            # PREFER THIS FOR A SINGLE-BRIDGE HALT. live_bridges counts registrations inside
            # their lease, which after a hard kill includes a PHANTOM for up to the lease: the
            # client is gone, sent no goodbye, and its registration sits there. Measured in the
            # wild on 2026-09-17 - a halt would have blamed an orphan that no longer existed.
            # live_bridges_fresh counts only those still heartbeating, so it drops within one
            # beat interval instead of one lease. See rpc_server/bridges.py.
            "live_bridges_fresh": bridges.count_fresh(),
            # The header name the client stamps tokens into, REPORTED rather
            # than duplicated as a literal on both sides of the wire. One copy.
            "bridge_header": bridges.BRIDGE_HEADER,
            # v0.2.1 - TWO DIFFERENT PROCESSES, NAMED AS SUCH. bridges[].pid is the pid each
            # CLIENT passes to hello(pid): the freecad-mcp shim, never FreeCAD. Measured
            # 2026-09-30 - bridges[0].pid resolved to a `python` process while the GUI was a
            # different pid, so a reader asking "is FreeCAD up?" got a true answer to another
            # question. This payload invited that confusion, so it now states both.
            # freecad_pid is THIS process - the addon runs inside FreeCAD.
            "freecad_pid": os.getpid(),
            "bridges_pid_is": "the CLIENT shim's pid from hello(pid), never FreeCAD's - "
                              "see freecad_pid for the FreeCAD process",
            "bridges": bridges.live(),
            # The EFFECTIVE receipt directory, so the client reads job receipts
            # from a reported path instead of guessing one. Resolved in THIS
            # process: an env var set for freecad-mcp.exe never reaches here.
            "jobs_dir": async_jobs.jobs_dir(),
        }

    def gui_ping(self, cap: float = 5.0) -> dict[str, Any]:
        """BC-04. Dispatch a no-op THROUGH the GUI thread under its own budget.

        The question get_rpc_status structurally cannot answer. ``alive`` means
        exactly "a no-op completed on the GUI thread within cap": a legitimately
        busy GUI returns alive=false with health.state "busy", which is NOT the
        same as dead. Dead is last_gui_heartbeat_age_s growing without bound.

        Runs on the BYPASS path (rpc_server/health_probe.py): no rejection
        check, no DispatchHealth mutation. It is the only caller allowed past a
        stuck flag.
        """
        return health_probe.gui_ping(cap)

    def reset_dispatch_health(self, force: bool = False) -> dict[str, Any]:
        """BC-05. Clear a STALE stuck flag; report a REAL wedge unchanged."""
        return health_probe.reset_dispatch_health(force)

    def set_gui_budget(self, tool: str, R: float, Q: float | None = None) -> dict[str, Any]:
        """BC-02. Configuration is an RPC, not a server flag.

        Bounds [5, 3600] seconds, applied to the table, persisted, and the NEW
        TABLE ECHOED so the client re-derives its socket timeouts from one copy.
        RESTRICTED in the harness tiering: an operator's decision, never a
        payload's.
        """
        return budgets.set_gui_budget(tool, R, Q)

    def get_async_status(self, job_id: str = "") -> dict[str, Any]:
        """Report background jobs without using the GUI thread.

        With ``job_id`` returns that job as the BC-01 record: state
        (``running``/``done``/``partial``/``failed``), result, output,
        output_truncated, plan_chunks, chunks, error and traceback. Without it
        returns all running jobs and up to 20 recently finished jobs. History
        resets when FreeCAD exits; the same record is also on disk under
        ``jobs_dir`` (reported by get_rpc_status).

        A CONSUMER VALIDATES COMPLETENESS BY THE CHUNK COUNT, NOT THE STATE
        WORD: ``state == "done" and (plan_chunks is None or len(chunks) ==
        plan_chunks)``. rpc_server/async_jobs.py ships that rule as
        ``valid_receipt()`` beside the schema.
        """
        if job_id:
            job = async_jobs.get_job(job_id)
            if job is None:
                return {"success": False, "error": f"unknown async job: {job_id}"}
            return {"success": True, "job": job}
        return {"success": True, "jobs": async_jobs.all_jobs()}

    def create_document(self, name="New_Document"):
        # The GUI handler reports the document's ACTUAL name — FreeCAD
        # sanitises requested names ("My Doc" -> "My_Doc") and de-duplicates
        # ("Doc" -> "Doc001"); reporting the requested name breaks every
        # follow-up call that uses it.
        res = dispatch_to_gui(
            lambda: self._create_document_gui(name),
            operation_name="create_document",
            **_gui_default(),
        )
        if isinstance(res, dict) and res.get("success"):
            return res
        return _err(res)

    def create_object(self, doc_name, obj_data: dict[str, Any]):
        obj = Object(
            name=obj_data.get("Name", "New_Object"),
            type=obj_data["Type"],
            analysis=obj_data.get("Analysis", None),
            properties=obj_data.get("Properties", {}),
        )
        # create_object_gui reports the created object's actual Name (see
        # its docstring) — same sanitise/de-duplicate concern as documents.
        res = dispatch_to_gui(
            lambda: self._create_object_gui(doc_name, obj),
            operation_name="create_object",
            **_gui_default(),
        )
        if isinstance(res, dict) and res.get("success"):
            return res
        return _err(res)

    def edit_object(self, doc_name: str, obj_name: str, properties: dict[str, Any]) -> dict[str, Any]:
        obj = Object(
            name=obj_name,
            properties=properties.get("Properties", {}),
        )
        res = dispatch_to_gui(
            lambda: self._edit_object_gui(doc_name, obj),
            operation_name="edit_object",
            **_gui_default(),
        )
        if _ok(res):
            return {"success": True, "object_name": obj.name}
        return _err(res)

    def delete_object(self, doc_name: str, obj_name: str):
        res = dispatch_to_gui(
            lambda: self._delete_object_gui(doc_name, obj_name),
            operation_name="delete_object",
            **_gui_default(),
        )
        if _ok(res):
            return {"success": True, "object_name": obj_name}
        return _err(res)


    def reload_document(self, doc_name: str) -> dict[str, Any]:
        """Close and re-open a document by name to pick up external file
        changes (e.g. edits made by another process such as `freecadcmd`
        running headlessly). Returns success once the new document is
        loaded from disk.
        """
        res = dispatch_to_gui(
            lambda: self._reload_document_gui(doc_name),
            operation_name="reload_document",
            **_gui_default(),
        )
        if _ok(res):
            return {"success": True, "document_name": doc_name}
        return _err(res)

    def run_fem_analysis(self, doc_name: str, analysis_name: str, timeout: int = 600) -> dict[str, Any]:
        """Run the CalculiX solver on an existing Fem::FemAnalysis and return summary results."""
        try:
            timeout_s = int(timeout)
        except (TypeError, ValueError):
            return {"success": False, "error": f"invalid timeout: {timeout!r}"}
        # The call parameter overrides R, as it does at 5dbfe2c, and the queue
        # budget keeps defaulting to it. The table's "fem" entry (600/600)
        # documents that default and is what the client derives its 1230 s
        # socket from; FEM is otherwise out of this contract's scope.
        res = dispatch_to_gui(
            lambda: self._run_fem_analysis_gui(doc_name, analysis_name),
            timeout=timeout_s,
            operation_name="run_fem_analysis",
        )
        if isinstance(res, dict):
            return res
        return {"success": False, "error": str(res)}

    def execute_code_async(self, code: str) -> dict[str, Any]:
        """Start code execution in a background thread and return immediately.

        Use for long-running OCCT *geometry* work (fuse/cut/loft on shapes) that
        would otherwise exceed the MCP timeout. The caller should poll a document
        object for completion status (e.g. check SessionState.Label via get_object).

        Thread-safety contract — read before using this method:

        FreeCAD documents and the Coin3D scenegraph are NOT thread-safe. Code run
        here executes off the GUI thread, so it must not touch them directly.
        Assigning ``obj.Shape``, calling ``doc.recompute()``, ``doc.addObject()``,
        ``doc.save()`` or any ``ViewObject`` from this thread races the GUI thread
        and can wedge FreeCAD's event loop, after which the RPC server stops
        answering entirely.

        Safe pattern: build shapes in the background, then hand the document write
        to the GUI thread via the injected ``commit`` helper::

            box = Part.makeBox(10, 10, 10)          # background: fine
            fused = base.fuse(box).removeSplitter()  # background: fine, this is the slow part

            def apply():                             # runs on the GUI thread
                obj.Shape = fused
                doc.recompute()

            commit(apply)                            # blocks until the GUI thread ran it

        ``commit(fn, timeout=...)`` returns ``fn``'s value, or raises RuntimeError
        if the GUI dispatch failed or timed out. The helper persists so saved
        functions can reuse it in later async calls. It may only be called from
        an async worker, not from a synchronous script or GUI callback.
        """
        def _set_status(msg):
            # OPEN-4 (build blueprint s13, UNRESOLVED - a human must sign off):
            #     THIS IS A SYNCHRONOUS, HEALTH-TRACKED DISPATCH AND IT RUNS
            #     BEFORE THE WORKER STARTS (see the call at the end of this
            #     method). Against a BUSY - not stuck - GUI it can block for the
            #     full queue budget (60 s at the shipped table) before the job
            #     even begins, which contradicts "returns a job_id immediately"
            #     and degrades the BC-01/BC-07 entry point this release is built
            #     around. Against a STUCK GUI it returns instantly via the
            #     rejection, which is why the defect is invisible in the stuck
            #     case. No design document mentions it. The blueprint recommends
            #     moving this inside the worker, or dispatching it with the
            #     probe-sized budget and ignoring the result; BOTH CHANGE
            #     MEASURED BEHAVIOUR, so the baseline is preserved here and the
            #     defect is reported instead.
            dispatch_to_gui(
                lambda: FreeCADGui.getMainWindow().statusBar().showMessage(msg),
                operation_name="show_async_status",
                **_gui_default(),
            )

        def _clear_status():
            # Short timeout: this runs in the worker's finally block, so a wedged
            # or busy GUI thread must not keep the worker alive for the full
            # default dispatch timeout. Losing a status-bar reset is harmless.
            #
            # OPEN-10 (build blueprint s13, UNRESOLVED - a human must confirm):
            #     This literal 5 is NOT the table's "probe" entry, even though
            #     both are 5 s. "probe" is documented as health-UNTRACKED and
            #     belongs to probe_gui alone; this dispatch IS health-tracked, so
            #     mapping both to one key would conflate a tracked call with an
            #     untracked one. Adding a sixth key ("status_clear") would change
            #     the table shape that test_default_table_is_the_baseline
            #     asserts. The literal therefore stays, DELIBERATELY OUTSIDE THE
            #     TABLE: the table's client-facing purpose is socket derivation
            #     and this call has no socket.
            dispatch_to_gui(
                lambda: FreeCADGui.getMainWindow().statusBar().clearMessage(),
                timeout=5,
                operation_name="clear_async_status",
            )

        job_id = f"job-{uuid.uuid4().hex}"
        code_preview = (
            code if len(code) <= _CODE_PREVIEW_CHARS
            else code[:_CODE_PREVIEW_CHARS] + "…"
        )

        def worker() -> None:
            # NOTE: we do NOT redirect sys.stdout here. contextlib.redirect_stdout
            # swaps stdout process-wide, not per-thread, so it would race with the
            # GUI thread and other concurrent work. Background code should report
            # via emit() - which IS per-job and thread-local - or FreeCAD.Console
            # (thread-safe) instead.
            # Execute against the live dictionary. Merging a snapshot on exit
            # would restore stale values and lose deletions/concurrent writes.
            _async_execution.active = True
            context = async_jobs.begin_job(job_id)
            raised: BaseException | None = None
            return_value: Any = None
            try:
                return_value = _exec_async_payload(code, _EXEC_NAMESPACE)
            except BaseException as e:
                # SystemExit/KeyboardInterrupt raised by a worker script must
                # also finish its job record rather than leave it running.
                raised = e
            finally:
                del _async_execution.active
                # THE STORE decides the terminal state: a worker that reported
                # fewer chunks than it planned is "partial", never "done".
                outcome = async_jobs.finalize(context, raised, return_value)
                async_jobs.end_job()
                # Publish the result before best-effort GUI/log cleanup. A busy
                # GUI must not prevent a client from observing script failure.
                _record_job(job_id, finished=time.time(), **outcome)
                try:
                    if outcome["state"] == "done":
                        FreeCAD.Console.PrintMessage("Async code execution completed.\n")
                    elif outcome["state"] == "failed":
                        FreeCAD.Console.PrintError(
                            f"Async code error ({job_id}): {outcome['error']}\n{outcome['traceback']}\n"
                        )
                    else:
                        FreeCAD.Console.PrintWarning(
                            f"Async job {job_id} finished {outcome['state']}: "
                            f"{len(outcome.get('chunks') or [])} of "
                            f"{outcome.get('plan_chunks')} planned chunks.\n"
                        )
                except Exception:
                    pass
                try:
                    _clear_status()
                except Exception:
                    pass  # never let status cleanup mask or outlive the real work

        # code_preview is canonical; code is the v0.2.x alias - see OPEN-6.
        _record_job(
            job_id, state="running", started=time.time(),
            code_preview=code_preview, code=code_preview,
        )
        try:
            _set_status("MCP: running background task…")
            threading.Thread(target=worker, daemon=True).start()
        except Exception as e:
            import traceback as _tb
            error = f"{type(e).__name__}: {e}"
            _record_job(
                job_id, state="failed", finished=time.time(), error=error,
                traceback=_tb.format_exc().rstrip(),
            )
            return {"success": False, "job_id": job_id, "error": error}
        return {
            "success": True,
            "job_id": job_id,
            "message": (
                "Code execution started in background. Document writes "
                "(obj.Shape = ..., recompute, addObject, save, ViewObject) must "
                "go through commit(fn) — direct writes from this thread can wedge "
                "FreeCAD."
            ),
        }

    def execute_code(self, code: str, timeout: float | None = None) -> dict[str, Any]:
        """Execute Python code on the GUI thread and wait for the result.

        Runs on the GUI thread so that FreeCAD document operations
        (addObject, recompute, save) are safe and correctly ordered.
        Use execute_code_async for heavy OCCT boolean ops (fuse/cut)
        that would block the GUI thread too long.

        ``timeout`` (v0.2.0, optional) may request LESS than this server's run
        budget R for execute_code. Requesting MORE is a structured error naming
        the cap - never a silent clamp - so a payload cannot vote itself
        unlimited GUI time. Omitting it keeps the baseline behaviour exactly,
        which is why a v0.1.23 client calling execute_code(code) still works.
        """
        run_budget, queue_budget = self._execute_code_budgets()
        if timeout is not None:
            try:
                requested = float(timeout)
            except (TypeError, ValueError):
                return {"success": False, "error": f"invalid timeout: {timeout!r}"}
            if requested <= 0:
                return {
                    "success": False,
                    "error": f"invalid timeout: {requested:g}s must be positive",
                }
            if requested > run_budget:
                return {
                    "success": False,
                    "code": "TIMEOUT_ABOVE_CAP",
                    "cap_seconds": run_budget,
                    "error": (
                        f"requested timeout {requested:g}s exceeds this server's "
                        f"execute_code run budget of {run_budget:g}s. Lower the "
                        f"request, or raise the cap with "
                        f"set_gui_budget('execute_code', R) - an operator action."
                    ),
                }
            run_budget = requested

        output_buffer = io.StringIO()

        def task():
            with contextlib.redirect_stdout(output_buffer):
                exec(code, _EXEC_NAMESPACE)
            return True

        res = dispatch_to_gui(
            task,
            timeout=run_budget,
            queue_timeout=queue_budget,
            operation_name="execute_code",
        )
        if _ok(res):
            FreeCAD.Console.PrintMessage("Python code executed successfully.\n")
            return {
                "success": True,
                "message": "Python code executed successfully.\nOutput: " + output_buffer.getvalue(),
            }
        # Log the offending code (truncated) to make errors traceable
        code_preview = code if len(code) <= 800 else code[:800] + "\n...(truncated)"
        FreeCAD.Console.PrintError(
            f"Error executing Python code: {res}\n"
            f"--- code ---\n{code_preview}\n--- end ---\n"
        )
        return _err(res)

    def get_objects(self, doc_name: str) -> list[dict[str, Any]]:
        return _query_on_gui(lambda: self._get_objects_gui(doc_name), "get_objects")

    def _get_objects_gui(self, doc_name: str) -> list[dict[str, Any]]:
        # FreeCAD.getDocument raises (not returns None) for an unknown name.
        try:
            doc = FreeCAD.getDocument(doc_name)
        except Exception:
            return []
        return [serialize_object(obj) for obj in doc.Objects]

    def get_object(self, doc_name: str, obj_name: str) -> dict[str, Any] | None:
        return _query_on_gui(
            lambda: self._get_object_gui(doc_name, obj_name), "get_object"
        )

    def _get_object_gui(self, doc_name: str, obj_name: str) -> dict[str, Any] | None:
        # FreeCAD.getDocument raises (not returns None) for an unknown name.
        try:
            doc = FreeCAD.getDocument(doc_name)
        except Exception:
            return None
        obj = doc.getObject(obj_name)
        if obj:
            return serialize_object(obj)
        return None

    def insert_part_from_library(self, relative_path):
        res = dispatch_to_gui(
            lambda: self._insert_part_from_library(relative_path),
            operation_name="insert_part_from_library",
            **_gui_default(),
        )
        if _ok(res):
            return {"success": True, "message": "Part inserted from library."}
        return _err(res)

    def list_documents(self) -> list[str]:
        return _query_on_gui(
            lambda: list(FreeCAD.listDocuments().keys()), "list_documents"
        )

    def get_parts_list(self):
        return get_parts_list()

    def get_active_screenshot(
        self,
        view_name: str = "Isometric",
        width: int | None = None,
        height: int | None = None,
        focus_object: str | None = None,
    ) -> str:
        """Get a screenshot of the active view as a base64-encoded PNG string.

        Returns None if the active view does not support screenshots
        (e.g., TechDraw or Spreadsheet workbench).
        """
        fd, tmp_path = tempfile.mkstemp(suffix=".png")
        os.close(fd)

        def task():
            try:
                active_view = FreeCADGui.ActiveDocument.ActiveView
            except Exception:
                return False
            if active_view is None or not hasattr(active_view, "saveImage"):
                view_type = type(active_view).__name__ if active_view is not None else "None"
                FreeCAD.Console.PrintWarning(
                    f"MCP RPC: view type '{view_type}' does not support screenshots\n"
                )
                return False
            return save_active_screenshot(tmp_path, view_name, width, height, focus_object)

        try:
            res = dispatch_to_gui(
                task, operation_name="get_active_screenshot", **_gui_default()
            )
            if _ok(res):
                with open(tmp_path, "rb") as f:
                    return base64.b64encode(f.read()).decode("utf-8")
            if res is False:
                return None
            FreeCAD.Console.PrintWarning(f"MCP RPC: screenshot failed: {res}\n")
            return None
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def _create_document_gui(self, name):
        doc = FreeCAD.newDocument(name)
        doc.recompute()
        FreeCAD.Console.PrintMessage(f"Document '{doc.Name}' created via RPC.\n")
        return {"success": True, "document_name": doc.Name}

    def _create_object_gui(self, doc_name, obj: Object):
        return create_object_gui(doc_name, obj)

    def _edit_object_gui(self, doc_name: str, obj: Object):
        return edit_object_gui(doc_name, obj)

    def _run_fem_analysis_gui(self, doc_name: str, analysis_name: str):
        return _run_fem_analysis(doc_name, analysis_name)

    def _delete_object_gui(self, doc_name: str, obj_name: str):
        try:
            doc = FreeCAD.getDocument(doc_name)
        except Exception:
            FreeCAD.Console.PrintError(f"Document '{doc_name}' not found.\n")
            return f"Document '{doc_name}' not found.\n"

        try:
            doc.removeObject(obj_name)
            doc.recompute()
            FreeCAD.Console.PrintMessage(f"Object '{obj_name}' deleted via RPC.\n")
            return True
        except Exception as e:
            return str(e)


    def _reload_document_gui(self, doc_name: str):
        if doc_name not in FreeCAD.listDocuments():
            return f"Document '{doc_name}' is not loaded."
        doc = FreeCAD.getDocument(doc_name)
        file_path = doc.FileName
        if not file_path:
            return (
                f"Document '{doc_name}' has no file on disk "
                "(unsaved scratch document); nothing to reload from."
            )
        if not os.path.exists(file_path):
            return f"File for '{doc_name}' not found at {file_path!r}."
        # Close, then reopen from the same file. Reopen preserves the
        # original document name when the file was previously saved
        # under that name.
        FreeCAD.closeDocument(doc_name)
        FreeCAD.openDocument(file_path)
        FreeCAD.Console.PrintMessage(
            f"Document '{doc_name}' reloaded from '{file_path}' via RPC.\n"
        )
        return True

    def _insert_part_from_library(self, relative_path):
        try:
            insert_part_from_library(relative_path)
            return True
        except Exception as e:
            return str(e)

    def _save_active_screenshot(
        self,
        save_path: str,
        view_name: str = "Isometric",
        width: int | None = None,
        height: int | None = None,
        focus_object: str | None = None,
    ):
        return save_active_screenshot(save_path, view_name, width, height, focus_object)


def start_rpc_server(port=9875):
    global rpc_server_thread, rpc_server_instance

    if rpc_server_instance:
        return "RPC Server already running."

    # A previous stop may still be draining an in-flight request off-thread;
    # binding before its server_close() would hit the old socket.
    if _stop_thread is not None and _stop_thread.is_alive():
        _stop_thread.join(timeout=5.0)
        if _stop_thread.is_alive():
            return ("RPC Server is still stopping (a request is draining); "
                    "try again in a few seconds.")

    settings = load_settings()
    remote_enabled = settings.get("remote_enabled", False)
    allowed_ips = settings.get("allowed_ips", "127.0.0.1")

    if remote_enabled:
        host = "0.0.0.0"
    else:
        host = "127.0.0.1"

    try:
        table = budgets.get_budgets()
        budgets.assert_table(table)
        FreeCAD.Console.PrintMessage(f"MCP RPC: GUI dispatch budgets {table}\n")
    except ValueError as exc:
        FreeCAD.Console.PrintError(f"MCP RPC: budget table is invalid: {exc}\n")
        return f"RPC Server not started: budget table is invalid: {exc}"

    # Connection lifetime tracking (BC-06). ip_filter.py is unchanged; the
    # counting handler is supplied here, which is the only place that builds the
    # server. See OPEN-3 in connections.py before reading meaning into the count.
    connections.set_logger(FreeCAD.Console.PrintMessage)
    bridges.set_logger(FreeCAD.Console.PrintMessage)
    try:
        rpc_server_instance = FilteredXMLRPCServer(
            (host, port), allowed_ips_str=allowed_ips, allow_none=True, logRequests=False,
            requestHandler=BridgeAwareRequestHandler,
        )
    except OSError as exc:
        # The port is exclusive (ip_filter.FilteredXMLRPCServer). Failing here is the point: a
        # second FreeCAD must not serve the bridge alongside the first.
        rpc_server_instance = None
        msg = (f"RPC Server not started: {host}:{port} is already in use ({exc}). Another "
               f"FreeCAD is probably serving the MCP bridge; close it, or stop its RPC server, "
               f"before starting one here.")
        FreeCAD.Console.PrintError(msg + "\n")
        return msg
    rpc_server_instance.register_instance(FreeCADRPC())

    def server_loop():
        FreeCAD.Console.PrintMessage(f"RPC Server started at {host}:{port}\n")
        if remote_enabled:
            FreeCAD.Console.PrintMessage(f"Remote connections enabled. Allowed IPs: {allowed_ips}\n")
        rpc_server_instance.serve_forever()

    rpc_server_thread = threading.Thread(target=server_loop, daemon=True)
    rpc_server_thread.start()

    init_waker()
    QtCore.QTimer.singleShot(500, process_gui_tasks)

    msg = f"RPC Server started at {host}:{port}."
    if remote_enabled:
        msg += f" Allowed IPs: {allowed_ips}"
    return msg


class BridgeAwareRequestHandler(connections.CountingRequestHandler):
    """Renews the calling bridge's lease, and marks its call in flight.

    The token arrives in an HTTP header the client's transport stamps on every
    request, so renewal cannot be forgotten by an individual call and no method
    signature changes. in_flight is incremented for the whole dispatch: a bridge
    running a 20-minute FEM job is live by definition, which is what lets the
    lease TTL stay short enough for the G2 halt to be useful.
    """

    def do_POST(self):
        token = self.headers.get(bridges.BRIDGE_HEADER) if self.headers else None
        if token:
            bridges.begin_call(token)
        try:
            super().do_POST()
        finally:
            if token:
                bridges.end_call(token)


def stop_rpc_server():
    global rpc_server_instance, rpc_server_thread, _stop_thread

    if not rpc_server_instance:
        return "RPC Server was not running."

    server = rpc_server_instance
    thread = rpc_server_thread
    rpc_server_instance = None
    rpc_server_thread = None

    request_shutdown()
    cleanup_waker()

    def _shutdown_and_close():
        # shutdown() only stops the accept loop; in-flight requests run in
        # their own daemon threads and are not waited for. Kept off the GUI
        # thread so a menu command cannot block the UI. server_close() must
        # always follow, or the listening socket stays bound and Stop -> Start
        # fails with EADDRINUSE.
        try:
            server.shutdown()
            if thread is not None:
                thread.join(timeout=10.0)
                if thread.is_alive():
                    FreeCAD.Console.PrintWarning(
                        "MCP RPC: server thread still draining a request; "
                        "socket closes when it finishes.\n"
                    )
        finally:
            server.server_close()
        FreeCAD.Console.PrintMessage("RPC Server stopped.\n")

    _stop_thread = threading.Thread(target=_shutdown_and_close, daemon=True)
    _stop_thread.start()
    return "RPC Server stopping…"


register_commands()
schedule_toggle_sync()
