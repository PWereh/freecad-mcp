"""BC-02, client half: ONE derivation from the table the addon owns.

The addon owns the budgets and reports them through ``get_rpc_status()``. This
module is PURE - no state, no I/O - and turns that table into the per-call
socket timeout S:

    S = Q + R + M,  M = 30   (RPC_TIMEOUT_MARGIN, preserved)

At the shipped table that is 210 for execute_code, 150 for every other GUI tool
and 1230 for FEM: the 5dbfe2c behaviour exactly, now with one reason instead of
three separate ``2 * X + margin`` expressions.

OPEN-2 (build blueprint s13, UNRESOLVED as a document conflict, RESOLVED in
code as the blueprint directs - DO NOT "FIX" IT BACK):
    The authoritative wireframe s4 writes ``socket_for`` as a bare
    ``return b["Q"] + R + M``. THE BASELINE APPLIES AN ENVELOPE AT BOTH CALL
    SITES - ``max(self._timeout, 2 * EXECUTE_CODE_TIMEOUT + RPC_TIMEOUT_MARGIN)``
    at freecad_client.py:78 and the same shape for FEM at :117 - and
    ``tests/test_client_timeouts.py::test_long_call_socket_budgets_and_cleanup``
    is parameterised on ("method", "args", "minimum", "configured") with rows
    whose CONFIGURED timeout exceeds the derived value (500 and 2000). A bare
    Q + R + M fails two rows of a PRESERVED module.
    Therefore: ``socket_for`` stays pure here, so
    ``test_socket_for_reproduces_5dbfe2c`` sees exactly 210/150/1230, and
    ``max(configured, socket_for(...))`` is applied AT THE CALL SITE in
    freecad_client.py.

THE INVARIANT, asserted in this module's TESTS and not at runtime:

    S >= Q + R + M, with M >= 30

Today's execute_code nesting is 90 + 90 + 30 = 210 = S - AN EQUALITY, and it
MUST PASS. A strict ``<`` would refuse to boot the working baseline. There is no
"C" (client tool wait): the client blocks on the socket and nothing else.
``Q <= R`` is NOT required either, because a cold-start Q may legitimately
exceed R.
"""

from typing import Any


M = 30.0  # RPC_TIMEOUT_MARGIN, preserved from freecad_client.py:29

# The table the addon ships. Used ONLY until a real one has been read from
# get_rpc_status(); it is a fallback, not a second copy to be reconciled - a
# connection that has not completed the connect sequence still has to pick some
# socket timeout, and the baseline's numbers are the right guess.
BASELINE_TABLE: dict[str, dict[str, float]] = {
    "execute_code": {"R": 90, "Q": 90},
    "gui_default": {"R": 60, "Q": 60},
    "fem": {"R": 600, "Q": 600},
    "commit": {"R": 120, "Q": 120},
    "probe": {"R": 5, "Q": 5},
}


def assert_table(table: Any) -> None:
    """Raise ValueError unless *table* is a well-formed budget table.

    Well-formed means: a non-empty mapping of tool -> {"R": ..., "Q": ...} with
    every R and Q at least 1 second. This is the connect-time check that refuses
    a v0.1.23 addon, which reports no table at all.
    """
    if not isinstance(table, dict) or not table:
        raise ValueError("budget table is missing or empty")
    for tool, entry in table.items():
        if not isinstance(entry, dict) or "R" not in entry or "Q" not in entry:
            raise ValueError(f"budget entry for {tool!r} is malformed: {entry!r}")
        for key in ("R", "Q"):
            value = entry[key]
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 1:
                raise ValueError(f"budget {tool}.{key} must be >= 1 second, got {value!r}")


def socket_for(table: dict, tool: str, R_override: float | None = None) -> float:
    """S = Q + R + M for *tool*. PURE - the caller applies the envelope.

    ``R_override`` is the per-call run budget when a tool carries one
    (``run_fem_analysis(timeout=...)``, ``execute_code(timeout=...)``); the queue
    budget always comes from the table.
    """
    entry = table.get(tool) or table.get("gui_default") or BASELINE_TABLE["gui_default"]
    R = entry["R"] if R_override is None else float(R_override)
    return float(entry["Q"]) + float(R) + M


def satisfies_invariant(
    S: float, table: dict, tool: str, R_override: float | None = None, margin: float = M
) -> bool:
    """The reference check: S >= Q + R + M with M >= 30. Equality passes."""
    if margin < 30:
        return False
    return S >= socket_for(table, tool, R_override)
