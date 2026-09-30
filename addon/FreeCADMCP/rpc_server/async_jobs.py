"""Async job store, result channel, chunking state machine and file receipts.

This module owns the BC-01 job-result record. THE RECORD IS A SHARED INTERFACE,
not an implementation detail: a second harness (rxCAD pass 2, checks A1/A2)
consumes it as a receipt, so the state rules below are part of the interface.

================================ THE SCHEMA ================================

Returned by ``get_async_status(job_id)``; also written to
``<jobs_dir>/<job_id>.json`` on every state change by atomic rename, so the
verdict stays readable when the RPC thread is unreachable.

    {
      "id":               "job-<hex32>",                    str
      "state":            "running|done|partial|failed",    str, OPEN enum
      "started":          1789572000.12,                    number|null (epoch s)
      "finished":         1789572310.88,                    number|null
      "code_preview":     "first 200 chars",                str
      "code":             "<alias of code_preview>",        str  (see OPEN-6)
      "plan_chunks":      5,                                integer|null
      "chunks": [                                           array<object>
        {"name": "TransverseSection", "state": "ok|failed",
         "started": 0.0, "finished": 0.0, "result": {...}, "error": null}
      ],
      "result":           <json>|null,                      any
      "output":           "text from emit()",               str
      "output_truncated": false,                            bool
      "error":            null,                             str|null
      "traceback":        null                              str|null
    }

STATE RULES - the consumer contract, shipped here with the schema:

  1. ``done`` REQUIRES ``len(chunks) == plan_chunks`` AND every chunk ``ok``.
     THE STORE enforces this; a worker cannot mark itself done otherwise.
  2. A worker that returns before reporting ``plan_chunks`` chunks is
     ``partial``, not ``done``, even if it raised nothing.
  3. A worker that raises is ``failed``; chunks already recorded are KEPT.
  4. Single-shot jobs (``plan()`` never called) have ``plan_chunks: null`` and
     ``chunks: []``; completeness is ``state == "done"``.
  5. A CONSUMER VALIDATES COMPLETENESS BY THE COUNT, NOT THE WORD:

         valid_receipt := (state == "done")
                          and (plan_chunks is None or len(chunks) == plan_chunks)

     Checking the state word alone is the bug this design exists to remove; the
     count is the cross-check that stops a five-minute payload from presenting
     a partial verdict as a whole one. ``valid_receipt()`` below IS that rule -
     import it rather than re-implementing it.
  6. Fields are NEVER removed within v0.2.x; additions are allowed.
  7. ``state`` IS AN OPEN ENUM. Widening it is as breaking for a strict consumer
     as removing a field. The rule: ANY ``state`` OTHER THAN ``done`` IS
     NOT-DONE. A validator that enumerates running|done|failed and treats the
     rest as an error - or worse as success - is wrong by this rule, and one
     that RAISES on an unknown state instead of REJECTING it is equally wrong.
     ``valid_receipt()`` returns False for an unknown state and never raises.

============================================================================
"""

import json
import os
import tempfile
import threading
import time
import traceback as _tb
from typing import Any, Callable

from rpc_server.settings import load_settings


# Background jobs started by execute_code_async, newest last. Errors raised off
# the GUI thread used to reach only the Report View; the registry lets the
# client read them via get_async_status. Retain all running jobs and bound only
# completed history, in completion order. (Moved verbatim from rpc_server.py.)
_ASYNC_JOBS: dict[str, dict[str, Any]] = {}
_ASYNC_JOBS_LOCK = threading.Lock()
_ASYNC_JOBS_KEEP = 20

# Receipt write ordering. The store lock publishes a record to RPC readers; the
# file is written AFTERWARDS and deliberately OUTSIDE that lock, so
# get_async_status never blocks on disk I/O - it is the one call that must still
# answer while the GUI thread is wedged. Two threads therefore race to write the
# same job's receipt (the RPC thread records "running" while the worker, for a
# fast payload, is already recording "done"), and without ordering THE STALE
# WRITE CAN LAND LAST and leave a completed job looking like a running one on
# disk. Every record gets a revision under the store lock; a writer that finds a
# newer revision already on disk drops its own.
_receipt_lock = threading.Lock()
_receipt_revision = 0
_receipt_written: dict[str, int] = {}
_RECEIPT_WRITTEN_KEEP = 4096

# OPEN-5 (build blueprint s13, UNRESOLVED - a human must confirm):
#     The baseline evicts records whose state is in {"done", "failed"} and keeps
#     20 of them. v0.2.0 adds "partial", which is NOT in that set, so PARTIAL
#     JOBS ACCUMULATE WITHOUT BOUND for the life of the FreeCAD process. The
#     blueprint recommends inverting the predicate to ``state != "running"`` so
#     any future state is evicted correctly - the same open-enum discipline that
#     schema rule 7 imposes on consumers. That is a behaviour change to a
#     preserved code path, so THE BASELINE PREDICATE IS KEPT HERE UNCHANGED and
#     the leak is reported rather than silently fixed.
_FINISHED_STATES = {"done", "failed"}

# Per-job, thread-local binding for emit/result/plan/chunk. No process-wide
# stdout redirect: contextlib.redirect_stdout swaps the stream process-wide and
# would race the GUI thread. The baseline's reason for not capturing stdout
# (rpc_server.py:286) is correct and stands.
_current = threading.local()

_DEFAULT_OUTPUT_CAP = 1048576  # 1 MiB; FREECAD_MCP_OUTPUT_CAP overrides

_commit: Callable[..., Any] | None = None


def set_commit(commit: Callable[..., Any] | None) -> None:
    """Inject the GUI commit callable.

    rpc_server.py owns the dispatch call (a preserved test monkeypatches
    ``rpc_module.dispatch_to_gui``), so the callable is injected here rather
    than imported - build blueprint s8.2.
    """
    global _commit
    _commit = commit


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def _setting(key: str) -> Any:
    try:
        return (load_settings() or {}).get(key)
    except Exception:
        return None


def jobs_dir() -> str:
    """Resolve the receipt directory: env -> settings -> platform default.

    Read in the FreeCAD process, NOT the client's: an env var set for
    freecad-mcp.exe does not reach the addon. get_rpc_status echoes the
    effective value so the client reads receipts from a reported path and never
    guesses one.
    """
    env = os.environ.get("FREECAD_MCP_JOBS_DIR")
    if env:
        return env
    configured = _setting("jobs_dir")
    if isinstance(configured, str) and configured:
        return configured
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "freecad-mcp", "jobs")


def output_cap() -> int:
    """emit() buffer cap in bytes: env -> settings -> 1 MiB."""
    for candidate in (os.environ.get("FREECAD_MCP_OUTPUT_CAP"), _setting("output_cap")):
        try:
            if candidate is not None:
                value = int(candidate)
                if value > 0:
                    return value
        except (TypeError, ValueError):
            continue
    return _DEFAULT_OUTPUT_CAP


# --------------------------------------------------------------------------
# the store
# --------------------------------------------------------------------------

def record_job(job_id: str, **fields: Any) -> dict[str, Any]:
    """Create or update a job record, evict finished history, write the receipt."""
    global _receipt_revision
    with _ASYNC_JOBS_LOCK:
        job = _ASYNC_JOBS.pop(job_id, {"id": job_id})
        job.update(fields)
        _ASYNC_JOBS[job_id] = job
        finished = [
            key for key, value in _ASYNC_JOBS.items()
            if value.get("state") in _FINISHED_STATES
        ]
        for key in finished[:max(0, len(finished) - _ASYNC_JOBS_KEEP)]:
            del _ASYNC_JOBS[key]
        snapshot = dict(job)
        _receipt_revision += 1
        revision = _receipt_revision
    _write_receipt(snapshot, revision)
    return snapshot


def get_job(job_id: str) -> dict[str, Any] | None:
    with _ASYNC_JOBS_LOCK:
        job = _ASYNC_JOBS.get(job_id)
        return dict(job) if job is not None else None


def all_jobs() -> list[dict[str, Any]]:
    with _ASYNC_JOBS_LOCK:
        return [dict(job) for job in _ASYNC_JOBS.values()]


def running_ids() -> list[str]:
    with _ASYNC_JOBS_LOCK:
        return [job["id"] for job in _ASYNC_JOBS.values() if job.get("state") == "running"]


def reset() -> None:
    """Test helper; not part of the RPC surface."""
    with _ASYNC_JOBS_LOCK:
        _ASYNC_JOBS.clear()
    with _receipt_lock:
        _receipt_written.clear()


# --------------------------------------------------------------------------
# file receipts (BC-01): two independent routes to one record
# --------------------------------------------------------------------------

def _replace_with_retry(source: str, destination: str, attempts: int = 6) -> None:
    """os.replace with a bounded retry on a transient Windows lock.

    MEASURED, not defensive: on Windows the destination of an atomic rename is
    intermittently held for a few milliseconds by another process - the
    real-time virus scanner or the search indexer inspecting the file the
    previous write just created - and MoveFileEx then returns ERROR_ACCESS_DENIED
    (WinError 5). Observed here as a job's TERMINAL receipt silently failing to
    replace its own "running" receipt, which is precisely the failure the file
    channel exists to prevent: a finished job reading as still running on disk.
    Retries are bounded (about 0.3 s total) and the last failure propagates to
    the caller, which treats a lost receipt as non-fatal.
    """
    for attempt in range(attempts):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.02 * (attempt + 1))


def _write_receipt(job: dict[str, Any], revision: int) -> None:
    """Write <jobs_dir>/<job_id>.json by atomic rename. Best effort, never raises.

    ``revision`` orders writers for one job: an older snapshot never overwrites a
    newer one. See the note beside _receipt_revision for the race this closes.
    """
    job_id = job.get("id")
    if not job_id:
        return
    with _receipt_lock:
        if _receipt_written.get(job_id, 0) > revision:
            return  # a newer snapshot of this job is already on disk
        try:
            directory = jobs_dir()
            os.makedirs(directory, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                suffix=".tmp", prefix=str(job_id) + ".", dir=directory
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(job, handle, ensure_ascii=False, indent=2, default=str)
                _replace_with_retry(tmp_path, os.path.join(directory, str(job_id) + ".json"))
            except Exception:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                raise
            _receipt_written[job_id] = revision
            if len(_receipt_written) > _RECEIPT_WRITTEN_KEEP:
                for key in sorted(_receipt_written, key=_receipt_written.get)[
                    : len(_receipt_written) - _RECEIPT_WRITTEN_KEEP
                ]:
                    del _receipt_written[key]
        except Exception:
            # A receipt is a second channel to the same record; losing it must
            # never fail the job whose verdict it carries.
            pass


# --------------------------------------------------------------------------
# the injected async namespace (BC-01 / BC-07)
# --------------------------------------------------------------------------

class _JobContext:
    """Live, per-worker accumulator for one job's output, result and chunks."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.lock = threading.Lock()
        self.output: list[str] = []
        self.output_len = 0
        self.output_truncated = False
        self.result: Any = None
        self.result_set = False
        self.plan_chunks: int | None = None
        self.chunks: list[dict[str, Any]] = []


def begin_job(job_id: str) -> "_JobContext":
    """Bind a fresh context to THIS thread. Called by the worker, not by payloads."""
    context = _JobContext(job_id)
    _current.context = context
    return context


def end_job() -> None:
    if hasattr(_current, "context"):
        del _current.context


def current_context() -> "_JobContext | None":
    return getattr(_current, "context", None)


def _require_context(helper: str) -> "_JobContext":
    context = current_context()
    if context is None:
        raise RuntimeError(helper + "() is only available inside execute_code_async workers")
    return context


def emit(text: Any) -> None:
    """Append to this job's per-job, thread-local output buffer (capped)."""
    context = _require_context("emit")
    piece = text if isinstance(text, str) else str(text)
    cap = output_cap()
    with context.lock:
        if context.output_len >= cap:
            context.output_truncated = True
            return
        room = cap - context.output_len
        if len(piece) > room:
            piece = piece[:room]
            context.output_truncated = True
        context.output.append(piece)
        context.output_len += len(piece)


def result(obj: Any) -> None:
    """Store a JSON-serialisable value as this job's result."""
    context = _require_context("result")
    with context.lock:
        context.result = obj
        context.result_set = True


def plan(n_chunks: int) -> None:
    """Declare how many chunks this job will report; recorded as plan_chunks."""
    context = _require_context("plan")
    count = int(n_chunks)
    if count < 0:
        raise ValueError("plan() requires a non-negative chunk count, got " + repr(n_chunks))
    with context.lock:
        context.plan_chunks = count
        chunks = list(context.chunks)
    record_job(context.job_id, plan_chunks=count, chunks=chunks)


def chunk(name: str, obj: Any = None) -> None:
    """Record one completed chunk. A BaseException value records a FAILED chunk."""
    context = _require_context("chunk")
    now = time.time()
    with context.lock:
        started = context.chunks[-1]["finished"] if context.chunks else now
        if isinstance(obj, BaseException):
            entry = {
                "name": name, "state": "failed", "started": started, "finished": now,
                "result": None, "error": type(obj).__name__ + ": " + str(obj),
            }
        else:
            entry = {
                "name": name, "state": "ok", "started": started, "finished": now,
                "result": obj, "error": None,
            }
        context.chunks.append(entry)
        snapshot = list(context.chunks)
        planned = context.plan_chunks
    record_job(context.job_id, chunks=snapshot, plan_chunks=planned)


def commit_many(fns: Any, timeout_each: float | None = None) -> list:
    """Run each callable in ITS OWN GUI budget; stop at the first failure.

    BC-07: a five-minute payload is a SEQUENCE of GUI steps, none of which may
    run for R inside one commit(). Health returns to healthy between calls.
    Stops at the first failure: the RuntimeError commit() raises propagates, and
    the chunks recorded before it are kept by the store (schema rule 3).

    OPEN-12 (build blueprint s13, UNRESOLVED): no document states
    ``timeout_each``'s default. ``None`` is taken here to mean "whatever
    ``commit`` itself defaults to", i.e. the budget table's ``commit.R`` (120),
    matching ``commit``'s documented default, so a default commit_many trips its
    own budget rather than gui_default's. Recorded so it is a decision rather
    than an accident.
    """
    if _commit is None:
        raise RuntimeError("commit_many() is only available inside execute_code_async workers")
    results: list[Any] = []
    for fn in fns:
        if timeout_each is None:
            results.append(_commit(fn))
        else:
            results.append(_commit(fn, timeout_each))
    return results


def namespace(commit: Callable[..., Any]) -> dict[str, Any]:
    """The helpers injected beside ``commit`` into the async payload namespace."""
    set_commit(commit)
    return {
        "emit": emit,
        "result": result,
        "plan": plan,
        "chunk": chunk,
        "commit_many": commit_many,
    }


# --------------------------------------------------------------------------
# the state machine (BC-01 rules 1-4)
# --------------------------------------------------------------------------

def finalize(context: "_JobContext | None", raised: BaseException | None,
             return_value: Any = None) -> dict[str, Any]:
    """Build the terminal field set for a worker that has just returned or raised.

    THE STORE enforces rule 1 here: a worker cannot mark itself ``done`` when it
    reported fewer chunks than it planned, or when any chunk failed.
    """
    if context is None:
        if raised is None:
            return {"state": "done"}
        return {
            "state": "failed",
            "error": type(raised).__name__ + ": " + str(raised),
            "traceback": "".join(_tb.format_exception(
                type(raised), raised, raised.__traceback__)).rstrip(),
        }

    with context.lock:
        chunks = list(context.chunks)
        planned = context.plan_chunks
        output = "".join(context.output)
        truncated = context.output_truncated
        value = context.result if context.result_set else return_value

    all_ok = all(entry.get("state") == "ok" for entry in chunks)
    if raised is not None:
        state = "failed"          # rule 3: chunks already recorded are kept
    elif planned is None:
        state = "done" if all_ok else "partial"   # rule 4
    elif len(chunks) == planned and all_ok:
        state = "done"            # rule 1
    else:
        state = "partial"         # rule 2

    fields: dict[str, Any] = {
        "state": state,
        "plan_chunks": planned,
        "chunks": chunks,
        "result": value,
        "output": output,
        "output_truncated": truncated,
    }
    if raised is not None:
        fields["error"] = type(raised).__name__ + ": " + str(raised)
        fields["traceback"] = "".join(_tb.format_exception(
            type(raised), raised, raised.__traceback__)).rstrip()
    return fields


# --------------------------------------------------------------------------
# the reference validator (schema rules 5 and 7)
# --------------------------------------------------------------------------

def valid_receipt(job: Any) -> bool:
    """True only for a COMPLETE job record. NEVER raises on an unknown state.

    This is the rule quoted in the schema documentation above, shipped as code
    so a consumer imports it instead of re-implementing it:

        valid_receipt := (state == "done")
                         and (plan_chunks is None or len(chunks) == plan_chunks)

    plus rule 1's "every chunk ok", which the store already enforces and this
    re-checks because a receipt may have been read from disk.
    """
    if not isinstance(job, dict):
        return False
    if job.get("state") != "done":
        return False  # rule 7: ANY state other than done is not-done
    planned = job.get("plan_chunks")
    chunks = job.get("chunks")
    if planned is None:
        if chunks is None:
            return True
        if not isinstance(chunks, list):
            return False
        return all(isinstance(e, dict) and e.get("state") == "ok" for e in chunks)
    if not isinstance(planned, int) or isinstance(planned, bool):
        return False
    if not isinstance(chunks, list) or len(chunks) != planned:
        return False  # rule 5: the COUNT, not the word
    return all(isinstance(e, dict) and e.get("state") == "ok" for e in chunks)


def receipt_rejection_reason(job: Any) -> str | None:
    """Why ``valid_receipt`` rejected a record, or None when it accepted it."""
    if not isinstance(job, dict):
        return "record is not a mapping"
    state = job.get("state")
    if state != "done":
        return (
            "state " + repr(state) + " is not 'done' (schema rule 7: any state "
            "other than done is not-done)"
        )
    planned = job.get("plan_chunks")
    chunks = job.get("chunks")
    if planned is not None:
        if not isinstance(chunks, list):
            return "plan_chunks declared but chunks is not a list"
        if len(chunks) != planned:
            return (
                "chunk count " + str(len(chunks)) + " != plan_chunks "
                + str(planned) + " (schema rule 5)"
            )
    if isinstance(chunks, list):
        failed = [e.get("name") for e in chunks if isinstance(e, dict) and e.get("state") != "ok"]
        if failed:
            return "chunks not ok: " + repr(failed)
    return None
