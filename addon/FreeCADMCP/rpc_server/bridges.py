"""OPEN-3 option (c): bridge presence measured by ``hello(pid)`` registration.

RESOLVED 2026-09-17 by the user: option (c), with the binding constraint that it
must MEASURE LIVE BRIDGES, NOT PROCESSES.

WHY NOT PROCESSES
-----------------
The obvious implementation of ``hello(pid)`` is to keep the pid and answer
"live" with ``os.kill(pid, 0)`` or ``psutil.pid_exists(pid)``. THIS MODULE NEVER
DOES THAT, and the constraint is the reason:

  * A process can be alive while its bridge is dead. A freecad-mcp.exe whose
    stdio read loop is wedged, whose XML-RPC proxy has been closed by
    ``close_bridge``, or which is sitting in an un-reaped shutdown, is a live
    PROCESS and no bridge at all. OPEN-13 measured exactly this - THREE ORPHANED
    freecad-mcp.exe, each still holding or half-closing a socket on 9875. A pid
    check counts all three; they are zero bridges.
  * Windows recycles pids. A pid that answers "alive" may be an unrelated
    process wearing a dead bridge's number.
  * A pid check measures the machine. The question G2 asks - "is there exactly
    one live bridge talking to THIS FreeCAD" - is about the conversation, and
    only the conversation can answer it.

So liveness here is LEASE FRESHNESS: evidence the bridge itself spoke recently.
The pid is identity and diagnostics, never the liveness test. The test that
proves the difference is ``test_live_pid_expired_lease_is_not_a_bridge``, which
registers the RUNNING TEST PROCESS'S OWN PID - alive beyond argument - lets the
lease expire, and requires the count to be 0.

WHAT KEEPS A LEASE FRESH
------------------------
Three things, and the third is not optional:

  1. ``hello`` mints a lease.
  2. Every RPC call the bridge makes renews it. The token rides in an HTTP
     header (``BRIDGE_HEADER``) that the client's transport adds to every
     request, so no method signature changes and no call can forget to renew.
  3. IN-FLIGHT IMMUNITY. A bridge with a call in flight is live BY DEFINITION,
     whatever its lease says. This is load-bearing rather than tidy: the budget
     table gives fem R=600 Q=600, so one legitimate call can run ~20 minutes.
     Without immunity the TTL would have to exceed the longest call - over
     1230 s - and a TTL that long makes the G2 halt useless, because a
     hard-killed bridge would keep counting for twenty minutes and halt the next
     honest one. With immunity the TTL can be short AND a long FEM run never
     reads as dead.

  An idle bridge that makes no calls is kept fresh by the client's heartbeat
  thread (``freecad_mcp.server._start_heartbeat``). Without it an idle-but-live
  bridge would expire and read 0 - and UNDER-counting is the dangerous direction
  for G2, because it fails to halt when a second bridge really is there.

IDENTITY IS THE BRIDGE, NOT THE PID
-----------------------------------
A second ``hello`` from the same (pid, instance) SUPERSEDES the first rather
than adding to the count. This matters in this codebase: ``close_bridge`` clears
``state.freecad_connection`` and the next tool call reconnects, so one process
legitimately says hello more than once. Counting those as two would halt G2 on a
single healthy bridge. ``instance`` is a per-process token minted once by the
client, so identity survives reconnects within a process and still distinguishes
two bridges that somehow share one. Superseding uses the pid as an IDENTITY key;
it is still the lease, never the pid's existence, that decides live.

Stdlib only, and deliberately no ``import FreeCAD`` - same reason as
``connections.py``: the preserved ``tests/test_rpc_concurrency.py`` withdraws its
FreeCAD stub, so an import-time dependency here would be a latent failure.
"""

import os
import threading
import time
import uuid
from typing import Any, Callable

# The header the client's transport stamps on every XML-RPC request. Renewal
# rides on it so that no individual call has to remember to renew.
BRIDGE_HEADER = "X-FreeCAD-MCP-Bridge"

# Short, because in-flight immunity covers the long calls. See the module note.
DEFAULT_LEASE_S = 120.0
MIN_LEASE_S = 5.0
MAX_LEASE_S = 3600.0

# A LEASE CAN OUTLIVE ITS PROCESS, AND THAT IS A PHANTOM.
#
# The module note above argues a short TTL keeps the G2 halt useful because "a hard-killed bridge
# would keep counting for twenty minutes". That reasoning was right and STOPPED ONE STEP SHORT:
# 120 s is bounded, not zero. A client killed with TerminateProcess sends no goodbye, so its
# registration sits there until the lease expires, and for up to two minutes live_bridges reports
# a bridge whose process is already gone.
#
# MEASURED 2026-09-17 by edc25-cadman, across a real run:
#     t=20s  live_bridges=2  bridges=[6644, 11928]   <- 11928 was ALREADY hard-killed
#     t=60s  live_bridges=1  bridges=[6644]          <- its lease expired mid-run
#
# So G2's "exactly one live bridge" can halt on a PHANTOM for up to the lease, blaming an orphan
# that no longer exists. That is the ORIGINAL AMBIGUITY WITH THE SIGN REVERSED: G2 exists because
# a process count cannot tell a live bridge from a dead one; the lease fixes that direction and
# opens a bounded window where it cannot tell a dead bridge from a live one either.
#
# The distinguishing signal is already collected. A registered client heartbeats every lease/3, so
# a record whose last_seen is older than one interval plus slack is EXPIRING, not competing. An
# in-flight call is fresh by definition, for the same reason it is live by definition.
HEARTBEAT_DIVISOR = 3.0        # freecad_mcp.server._start_heartbeat uses lease/3
STALE_SLACK = 1.5              # tolerate one missed beat plus jitter before calling it stale

_lock = threading.Lock()
_bridges: dict = {}      # token -> record
_identity: dict = {}     # identity key -> token
_logger = None
_clock: Callable[[], float] = time.monotonic


def set_logger(logger: Callable[[str], None] | None) -> None:
    global _logger
    _logger = logger


def set_clock(clock: Callable[[], float] | None) -> None:
    """Test seam. A lease test that really sleeps 120 s is a test nobody runs."""
    global _clock
    _clock = clock or time.monotonic


def _log(message: str) -> None:
    if _logger is not None:
        try:
            _logger(message)
        except Exception:
            pass


def _identity_key(pid: Any, instance: Any) -> str:
    return "%s:%s" % (pid, instance) if instance else "pid:%s" % (pid,)


def _is_fresh(rec: dict, now: float) -> bool:
    """Is this bridge still TALKING, as opposed to merely inside its lease?

    Live answers "may I still count this registration". Fresh answers "is the process behind it
    demonstrably still there". They differ for exactly the lease window after a hard kill, which
    is the window in which a single-bridge halt would otherwise fire on a phantom.
    """
    if rec["in_flight"] > 0:
        return True
    return (now - rec["last_seen"]) <= (rec["lease_s"] / HEARTBEAT_DIVISOR) * STALE_SLACK


def _is_live(rec: dict, now: float) -> bool:
    """Live iff a call is in flight, or the lease has not expired.

    THE PID IS NOT CONSULTED. Read the module docstring before changing that.
    """
    if rec["in_flight"] > 0:
        return True
    return (now - rec["last_seen"]) < rec["lease_s"]


def hello(pid: Any, instance: Any = None, lease_s: float | None = None,
          contract: str | None = None) -> dict:
    """Register one bridge. Returns its token, lease and the resulting count."""
    try:
        lease = DEFAULT_LEASE_S if lease_s is None else float(lease_s)
    except (TypeError, ValueError):
        lease = DEFAULT_LEASE_S
    lease = max(MIN_LEASE_S, min(MAX_LEASE_S, lease))
    key = _identity_key(pid, instance)
    token = uuid.uuid4().hex
    now = _clock()
    with _lock:
        previous = _identity.get(key)
        if previous is not None:
            # Same bridge saying hello again (a reconnect). Supersede, never add.
            _bridges.pop(previous, None)
        _identity[key] = token
        _bridges[token] = {
            "token": token, "pid": pid, "instance": instance,
            "identity": key, "contract": contract,
            "registered_at": now, "last_seen": now,
            "lease_s": lease, "in_flight": 0, "calls": 0,
        }
        live_now = sum(1 for r in _bridges.values() if _is_live(r, now))
        superseded = previous is not None
    _log(
        "MCP RPC: bridge hello pid=%s instance=%s %slease=%.0fs (live bridges: %d)\n"
        % (pid, instance,
           "(superseded its own earlier registration) " if superseded else "",
           lease, live_now)
    )
    return {"success": True, "token": token, "lease_s": lease,
            "live_bridges": live_now, "superseded": superseded,
            "addon_pid": os.getpid()}


def renew(token: str) -> bool:
    """Refresh a lease. Called for every RPC request that carries the header."""
    if not token:
        return False
    now = _clock()
    with _lock:
        rec = _bridges.get(token)
        if rec is None:
            return False
        rec["last_seen"] = now
        rec["calls"] += 1
        return True


def begin_call(token: str) -> bool:
    """Mark a call in flight. An in-flight bridge is live whatever its lease says."""
    if not token:
        return False
    now = _clock()
    with _lock:
        rec = _bridges.get(token)
        if rec is None:
            return False
        rec["in_flight"] += 1
        rec["last_seen"] = now
        return True


def end_call(token: str) -> bool:
    if not token:
        return False
    now = _clock()
    with _lock:
        rec = _bridges.get(token)
        if rec is None:
            return False
        rec["in_flight"] = max(0, rec["in_flight"] - 1)
        rec["last_seen"] = now
        return True


def goodbye(token: str) -> dict:
    """Clean shutdown. A hard kill never gets here; the lease covers that."""
    with _lock:
        rec = _bridges.pop(token, None)
        if rec is not None and _identity.get(rec["identity"]) == token:
            _identity.pop(rec["identity"], None)
        live_now = sum(1 for r in _bridges.values() if _is_live(r, _clock()))
    if rec is not None:
        _log("MCP RPC: bridge goodbye pid=%s (live bridges: %d)\n"
             % (rec["pid"], live_now))
    return {"success": True, "found": rec is not None, "live_bridges": live_now}


def _reap(now: float) -> None:
    """Drop expired records. Called under _lock."""
    for token in [t for t, r in _bridges.items() if not _is_live(r, now)]:
        rec = _bridges.pop(token, None)
        if rec is not None and _identity.get(rec["identity"]) == token:
            _identity.pop(rec["identity"], None)


def live() -> list:
    """Live bridges, freshest first, with the age a human needs to judge them."""
    now = _clock()
    with _lock:
        _reap(now)
        out = []
        for rec in _bridges.values():
            item = dict(rec)
            item["last_seen_age_s"] = round(now - rec["last_seen"], 3)
            # The field that lets a caller tell a competitor from a corpse.
            item["heartbeat_fresh"] = _is_fresh(rec, now)
            item["age_s"] = round(now - rec["registered_at"], 3)
            item.pop("last_seen", None)
            item.pop("registered_at", None)
            out.append(item)
    out.sort(key=lambda r: r["last_seen_age_s"])
    return out


def count() -> int:
    """Number of LIVE BRIDGES - registrations inside their lease.

    BC-06 asks for this one: "0 after the parent dies" is about the registration going away.
    G2 should prefer count_fresh(); see the phantom note at the top of this module.
    """
    now = _clock()
    with _lock:
        _reap(now)
        return len(_bridges)


def count_fresh() -> int:
    """Number of bridges still HEARTBEATING. The number a single-bridge halt should read.

    Equals count() in the steady state and is strictly lower during the window after a hard kill,
    which is precisely the window where count() reports a phantom.
    """
    now = _clock()
    with _lock:
        _reap(now)
        return sum(1 for r in _bridges.values() if _is_fresh(r, now))


def reset() -> None:
    """Test helper; not part of the RPC surface."""
    with _lock:
        _bridges.clear()
        _identity.clear()
