"""OPEN-3 option (c): hello(pid) registration measures LIVE BRIDGES, NOT PROCESSES.

The constraint the user set when resolving OPEN-3 on 2026-09-17 is the thing
these tests exist to hold. Most of them would also pass against a naive
implementation that kept the pid and answered "live" with ``os.kill(pid, 0)``.
Two do not, and they are the point of the file:

  ``test_a_live_pid_with_an_expired_lease_is_not_a_bridge`` registers THIS TEST
  PROCESS'S OWN PID - alive beyond argument, since it is running the assertion -
  lets the lease expire, and requires the count to be 0. A pid-based
  implementation returns 1 and fails.

  ``test_the_module_never_asks_the_os_whether_a_pid_is_alive`` reads the shipped
  source and fails on ``os.kill``/``pid_exists``/``psutil``, so the property
  cannot be quietly removed later by someone who finds the lease inconvenient.

Each of those two carries its own ABLATION, because a control that has never
fired is indistinguishable from one that cannot. The ablations plant the exact
forbidden implementation and require the check to catch it, and they run in the
suite permanently rather than once by hand.
"""

from __future__ import annotations

import importlib.util
import os
import re
import threading
import types
import xmlrpc.client
from pathlib import Path
from xmlrpc.server import SimpleXMLRPCServer

import pytest

from freecad_mcp.freecad_client import BRIDGE_HEADER_DEFAULT, _TimeoutTransport

from test_rpc_handlers import rpc_module  # noqa: F401  (fixture)

BRIDGES_PATH = (
    Path(__file__).resolve().parents[1]
    / "addon" / "FreeCADMCP" / "rpc_server" / "bridges.py"
)


def _load_bridges() -> types.ModuleType:
    """Import the SHIPPED module by path - stdlib only, no FreeCAD needed."""
    spec = importlib.util.spec_from_file_location("_bridges_under_test", BRIDGES_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeClock:
    """A lease test that really sleeps 120 s is a test nobody runs."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def bridges():
    module = _load_bridges()
    clock = FakeClock()
    module.set_clock(clock)
    module.clock = clock
    yield module
    module.reset()


# ===========================================================================
# The count itself
# ===========================================================================

def test_hello_registers_one_live_bridge(bridges) -> None:
    reply = bridges.hello(4321, "inst-a")
    assert reply["success"] is True
    assert reply["token"]
    assert reply["live_bridges"] == 1
    assert bridges.count() == 1


def test_two_bridges_read_as_two_so_g2_is_measurable(bridges) -> None:
    """rxCAD G2 halts on 'more than one live bridge'. It must be able to see two."""
    bridges.hello(4321, "inst-a")
    second = bridges.hello(8765, "inst-b")
    assert second["live_bridges"] == 2
    assert bridges.count() == 2


def test_goodbye_drops_to_zero_non_vacuously(bridges) -> None:
    """BC-06: 'connected_clients == 0 after the parent dies'.

    Against the old concurrency gauge this passed VACUOUSLY - the number was ~0
    at idle even with a live bridge, so the assertion proved nothing. Here the
    zero means something precisely because the line above it reads one.
    """
    reply = bridges.hello(4321, "inst-a")
    assert bridges.count() == 1          # the line that makes the next one evidence
    bridges.goodbye(reply["token"])
    assert bridges.count() == 0


def test_an_expired_lease_without_goodbye_is_zero(bridges) -> None:
    """A hard kill never sends goodbye. The lease is the whole mechanism."""
    bridges.hello(4321, "inst-a", lease_s=60)
    assert bridges.count() == 1
    bridges.clock.advance(61)
    assert bridges.count() == 0


# ===========================================================================
# LIVE BRIDGES, NOT PROCESSES - the two discriminating checks, each ablated
# ===========================================================================

def test_a_live_pid_with_an_expired_lease_is_not_a_bridge(bridges) -> None:
    """THE DISCRIMINATING CONTROL for the constraint on OPEN-3.

    The registered pid is this very process, so it is alive beyond argument -
    it is executing the assertion. Its lease has expired, so it is not a bridge.
    An implementation that answered liveness with os.kill(pid, 0) or
    psutil.pid_exists returns 1 here and fails.

    This is the shape OPEN-13 actually measured: three orphaned freecad-mcp.exe,
    each a live process holding a socket on 9875, and zero live bridges.
    """
    my_own_pid = os.getpid()
    bridges.hello(my_own_pid, "inst-orphan", lease_s=30)
    assert bridges.count() == 1
    bridges.clock.advance(31)

    assert os.getpid() == my_own_pid          # the process is unambiguously alive
    assert bridges.count() == 0               # and it is not a live bridge
    assert bridges.live() == []


def test_the_discriminator_would_fail_a_pid_based_implementation(bridges) -> None:
    """ABLATION for the test above.

    Substitutes the implementation the constraint forbids - liveness by pid
    existence - and shows the same timeline then reads 1 instead of 0. Without
    this, a passing discriminator could just be an expired lease that happened
    to belong to a dead pid, and would prove nothing about which one was read.
    """
    def _pid_based_is_live(rec, _now):
        try:
            os.kill(rec["pid"], 0)            # the forbidden implementation
            return True
        except OSError:
            return False

    bridges.hello(os.getpid(), "inst-ablate", lease_s=30)
    bridges.clock.advance(31)
    assert bridges.count() == 0, "the shipped implementation must read 0 here"

    bridges.hello(os.getpid(), "inst-ablate", lease_s=30)
    bridges.clock.advance(31)
    bridges._is_live = _pid_based_is_live
    assert bridges.count() == 1, (
        "a pid-based implementation must read 1 on this timeline - if it does "
        "not, the discriminating test proves nothing"
    )


FORBIDDEN = (r"os\.kill", r"pid_exists", r"psutil", r"OpenProcess",
             r"Process\(", r"is_running")


def _forbidden_hits(source: str) -> list[str]:
    """Process-liveness calls in real code, ignoring prose and comments.

    Used by BOTH the guard below and its ablation, so the ablation exercises the
    checking code itself rather than a second copy of the idea - a check that
    only ever runs against a helper certifies the helper, not the guard.
    """
    code = chr(10).join(
        line for line in source.splitlines()
        if not line.lstrip().startswith(("#", chr(34), "*"))
    )
    return [pattern for pattern in FORBIDDEN if re.search(pattern, code)]


def test_the_module_never_asks_the_os_whether_a_pid_is_alive() -> None:
    """Guard the property at the source, so it cannot be quietly removed.

    Without this, a later edit could swap lease freshness for a pid check, every
    other test here would still pass, and the constraint would be gone with
    nothing firing.
    """
    hits = _forbidden_hits(BRIDGES_PATH.read_text(encoding="utf-8"))
    assert hits == [], (
        f"bridges.py references {hits}: liveness must be lease freshness, never "
        f"whether a pid exists. See the module docstring."
    )


def test_the_source_guard_fires_on_a_pid_based_implementation() -> None:
    """ABLATION for the guard above.

    Plants the exact edit the guard exists to stop and requires it to catch it.
    Without this, the guard passing tells us only that it ran.
    """
    source = BRIDGES_PATH.read_text(encoding="utf-8")
    needle = '    return (now - rec["last_seen"]) < rec["lease_s"]'
    assert needle in source, "the ablation's anchor has moved; re-point it"
    poisoned = source.replace(needle, '    os.kill(rec["pid"], 0)' + chr(10) + "    return True")
    assert _forbidden_hits(poisoned) == [r"os\.kill"]


# ===========================================================================
# In-flight immunity - why the TTL can be short
# ===========================================================================

def test_a_call_in_flight_survives_an_expired_lease(bridges) -> None:
    """A 20-minute FEM run must never read as a dead bridge.

    budgets gives fem R=600 Q=600, so one legitimate call outlives any TTL short
    enough to be useful for G2. Without this immunity the lease would have to
    exceed 1230 s, and a hard-killed bridge would then keep counting for twenty
    minutes and halt the next honest one.
    """
    reply = bridges.hello(4321, "inst-a", lease_s=60)
    bridges.begin_call(reply["token"])
    bridges.clock.advance(6000)               # far past the lease, mid-FEM
    assert bridges.count() == 1

    bridges.end_call(reply["token"])
    assert bridges.count() == 1               # end_call also renews
    bridges.clock.advance(61)
    assert bridges.count() == 0               # and then it expires normally


def test_in_flight_immunity_is_not_vacuous(bridges) -> None:
    """The same timeline WITHOUT begin_call must expire, or the test above proves nothing."""
    bridges.hello(4321, "inst-a", lease_s=60)
    bridges.clock.advance(6000)
    assert bridges.count() == 0


# ===========================================================================
# Identity is the bridge, not the pid
# ===========================================================================

def test_a_reconnect_supersedes_instead_of_double_counting(bridges) -> None:
    """close_bridge() then a tool call reconnects: one process, two hellos, ONE bridge.

    Counting those as two would halt G2 on a single healthy bridge.
    """
    first = bridges.hello(4321, "inst-a")
    second = bridges.hello(4321, "inst-a")
    assert second["superseded"] is True
    assert bridges.count() == 1
    assert bridges.renew(first["token"]) is False     # the old token is dead
    assert bridges.renew(second["token"]) is True


def test_two_instances_in_one_pid_are_two_bridges(bridges) -> None:
    """Identity is the bridge instance. The pid is diagnostics."""
    bridges.hello(4321, "inst-a")
    bridges.hello(4321, "inst-b")
    assert bridges.count() == 2


def test_live_reports_ages_so_a_human_can_judge_the_count(bridges) -> None:
    reply = bridges.hello(4321, "inst-a", lease_s=300)
    bridges.clock.advance(12)
    (record,) = bridges.live()
    assert record["pid"] == 4321
    assert record["token"] == reply["token"]
    assert record["last_seen_age_s"] == pytest.approx(12.0)
    assert record["lease_s"] == 300


# ===========================================================================
# The shipped RPC surface and the shipped handler
# ===========================================================================

def test_rpc_methods_are_the_shipped_ones(rpc_module) -> None:  # noqa: F811
    rpc_module.bridges.reset()
    rpc = rpc_module.FreeCADRPC()
    reply = rpc.hello(os.getpid(), "inst-rpc")
    assert reply["live_bridges"] == 1
    assert rpc.heartbeat(reply["token"])["renewed"] is True
    assert rpc.live_bridges()["count"] == 1
    assert rpc.goodbye(reply["token"])["found"] is True
    assert rpc.live_bridges()["count"] == 0


def test_status_reports_live_bridges_and_the_header_name(rpc_module) -> None:  # noqa: F811
    rpc_module.bridges.reset()
    rpc = rpc_module.FreeCADRPC()
    status = rpc.get_rpc_status()
    assert status["live_bridges"] == 0
    assert status["bridge_header"] == BRIDGE_HEADER_DEFAULT
    rpc.hello(os.getpid(), "inst-status")
    assert rpc.get_rpc_status()["live_bridges"] == 1
    rpc_module.bridges.reset()


def test_the_header_renews_through_the_shipped_handler(rpc_module) -> None:  # noqa: F811
    """End to end: the client's transport stamps, the addon's handler renews.

    Both halves are the shipped code - freecad_client._TimeoutTransport and
    rpc_server.BridgeAwareRequestHandler - so this does not certify a copy.
    """
    bridges = rpc_module.bridges
    bridges.reset()
    clock = FakeClock()
    bridges.set_clock(clock)
    try:
        server = SimpleXMLRPCServer(
            ("127.0.0.1", 0), allow_none=True, logRequests=False,
            requestHandler=rpc_module.BridgeAwareRequestHandler,
        )
        server.register_function(lambda: True, "ping")
        host, port = server.server_address
        loop = threading.Thread(target=server.serve_forever, daemon=True)
        loop.start()
        try:
            reply = bridges.hello(os.getpid(), "inst-wire", lease_s=60)
            token = reply["token"]

            clock.advance(50)                       # lease nearly out
            proxy = xmlrpc.client.ServerProxy(
                f"http://{host}:{port}", allow_none=True,
                transport=_TimeoutTransport(
                    timeout=10, bridge_header=BRIDGE_HEADER_DEFAULT,
                    bridge_token=token,
                ),
            )
            assert proxy.ping() is True             # a call that carries the header
            clock.advance(50)                       # past the ORIGINAL expiry
            assert bridges.count() == 1, "the header did not renew the lease"

            # And the negative: an unstamped call renews nothing.
            clock.advance(50)
            bare = xmlrpc.client.ServerProxy(
                f"http://{host}:{port}", allow_none=True,
                transport=_TimeoutTransport(timeout=10),
            )
            assert bare.ping() is True
            clock.advance(11)
            assert bridges.count() == 0, "an unstamped call must not renew"
        finally:
            server.shutdown()
            loop.join(timeout=5)
            server.server_close()
    finally:
        bridges.set_clock(None)
        bridges.reset()


def test_the_transport_stamps_only_when_it_has_a_token() -> None:
    with_token = _TimeoutTransport(timeout=5, bridge_header=BRIDGE_HEADER_DEFAULT,
                                   bridge_token="abc123")
    _host, headers, _x509 = with_token.get_host_info("localhost:9875")
    assert (BRIDGE_HEADER_DEFAULT, "abc123") in list(headers or [])

    without = _TimeoutTransport(timeout=5)
    _host, headers, _x509 = without.get_host_info("localhost:9875")
    assert not any(h[0] == BRIDGE_HEADER_DEFAULT for h in list(headers or []))


# ===========================================================================
# A LEASE CAN OUTLIVE ITS PROCESS - the phantom window
# ===========================================================================

def test_a_hard_killed_bridge_is_a_phantom_until_its_lease_expires(bridges) -> None:
    """The defect this distinction exists for, reproduced from a real measurement.

    edc25-cadman sampled a live run on 2026-09-17:
        t=20s  live_bridges=2  bridges=[6644, 11928]   <- 11928 was ALREADY hard-killed
        t=60s  live_bridges=1  bridges=[6644]          <- its lease expired mid-run

    A single-bridge halt reading live_bridges would have fired for that window, blaming an orphan
    that no longer existed. That is the ORIGINAL AMBIGUITY WITH THE SIGN REVERSED: the lease tells
    a live bridge from a dead process, and for one lease it cannot tell a dead bridge from a live
    one.

    THE TIMELINE IS THE TEST, so it uses real numbers. Lease 60 means a 20 s beat interval and a
    30 s staleness threshold (interval x STALE_SLACK). A first cut advanced only 25 s and failed,
    correctly: at 25 s the killed bridge is still INSIDE the freshness window and reads fresh
    because it has not yet missed a beat by enough. The implementation was right and the test was
    wrong - which is worth leaving in the record, because a control that fires on a timeline the
    mechanism does not actually produce proves nothing about the mechanism.
    """
    LEASE = 60.0                     # -> 20 s beat interval, 30 s staleness threshold
    bridges.hello(11928, "inst-killed", lease_s=LEASE)     # then hard-killed: no goodbye, ever
    running = bridges.hello(6644, "inst-running", lease_s=LEASE)

    bridges.clock.advance(35)        # past the 30 s threshold, still inside the 60 s lease
    bridges.renew(running["token"])  # the surviving client heartbeats; the dead one cannot

    assert bridges.count() == 2, "both registrations are still inside their leases - the phantom"
    assert bridges.count_fresh() == 1, "only one is still heartbeating"

    fresh = {b["pid"]: b["heartbeat_fresh"] for b in bridges.live()}
    assert fresh[6644] is True
    assert fresh[11928] is False, "a hard-killed client cannot heartbeat"

    bridges.clock.advance(26)        # 61 s since the killed one last spoke: lease expired
    bridges.renew(running["token"])
    assert bridges.count() == 1, "the phantom is gone once its lease runs out"
    assert bridges.count_fresh() == 1


def test_fresh_equals_live_in_the_steady_state(bridges) -> None:
    """Without this the distinction could be a permanent under-count rather than a refinement.

    A bridge that renews normally must read fresh, or live_bridges_fresh would be useless for the
    thing it was added for - every healthy second bridge would look like a phantom and the halt
    would stop firing when it should.
    """
    a = bridges.hello(1, "a", lease_s=60)
    b = bridges.hello(2, "b", lease_s=60)
    bridges.clock.advance(15)
    bridges.renew(a["token"])
    bridges.renew(b["token"])
    assert bridges.count() == 2
    assert bridges.count_fresh() == 2, "two healthy bridges must both read fresh"


def test_a_long_call_in_flight_is_fresh_not_a_phantom(bridges) -> None:
    """In-flight immunity must apply to freshness too, or a 20-minute FEM reads as a corpse."""
    reply = bridges.hello(1, "a", lease_s=60)
    bridges.begin_call(reply["token"])
    bridges.clock.advance(6000)
    assert bridges.count() == 1
    assert bridges.count_fresh() == 1, "a call in flight is fresh by definition"
