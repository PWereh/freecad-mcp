"""Per-tool GUI dispatch budgets. The ADDON owns this table; the client derives.

Every default below is the measured behaviour of the baseline at 5dbfe2c:

    execute_code  R=90   rpc_server.py:120, passed explicitly at :364
    gui_default   R=60   dispatch_to_gui signature default, gui_dispatch.py:183
    fem           R=600  run_fem_analysis timeout parameter default
    commit        R=120  _commit_async default, rpc_server.py:86
    probe         R=5    gui_ping cap; health-UNTRACKED (see OPEN-10)

``Q`` (queue budget, the wait to *start*) defaults to ``R`` for every call,
exactly as ``gui_dispatch.py:212`` does today. The client fetches this table
through ``get_rpc_status()`` and derives its socket timeout S = Q + R + M with
M = 30, which reproduces the baseline's 210 / 150 / 1230 exactly.

OPEN-1 (build blueprint s13, UNRESOLVED - a human must confirm):
    The authoritative architecture s2.3 and wireframe s1 put this table in
    ``rpc_server/settings.py``. It is here instead because
    ``tests/test_rpc_handlers.py:56`` stubs that module as
    ``SimpleNamespace(load_settings=..., save_settings=...)``; a
    ``from rpc_server.settings import get_budgets`` raises ImportError under
    that stub and kills a whole PRESERVED test module. This module persists
    THROUGH ``settings.load_settings`` / ``settings.save_settings``, which the
    stub does provide, so the stub stays sufficient and no preserved test is
    edited. The disagreement is about FILE LOCATION only; "the addon owns one
    table, persisted" holds either way.

OPEN-7 (build blueprint s13, UNRESOLVED - a human must confirm):
    The architecture says budgets are "persisted through FreeCAD's parameter
    store" and the wireframe says "via ParamGet". The baseline's settings.py
    persists to ``freecad_mcp_settings.json`` in ``FreeCAD.getUserAppDataDir()``
    and that file is live and load-bearing today (it carries auto_start_rpc).
    Introducing ParamGet would create a SECOND configuration store for one
    subsystem. This module therefore writes a "budgets" key into the existing
    JSON settings - one store, one file. If ParamGet is preferred, the change
    is confined to ``get_budgets`` and ``set_gui_budget`` below.
"""

import threading
from typing import Any

from rpc_server.settings import load_settings, save_settings


# Seconds. Every value is the measured 5dbfe2c behaviour - see the module docstring.
DEFAULT_BUDGETS: dict[str, dict[str, float]] = {
    "execute_code": {"R": 90, "Q": 90},
    "gui_default": {"R": 60, "Q": 60},
    "fem": {"R": 600, "Q": 600},
    "commit": {"R": 120, "Q": 120},
    "probe": {"R": 5, "Q": 5},
}

# set_gui_budget bounds, architecture s2.3.
MIN_BUDGET = 5.0
MAX_BUDGET = 3600.0

_SETTINGS_KEY = "budgets"
_lock = threading.Lock()
_cache: dict[str, dict[str, float]] | None = None


def _merge(overrides: Any) -> dict[str, dict[str, float]]:
    """Default table overlaid with persisted per-tool overrides."""
    table = {tool: dict(entry) for tool, entry in DEFAULT_BUDGETS.items()}
    if not isinstance(overrides, dict):
        return table
    for tool, entry in overrides.items():
        if not isinstance(entry, dict):
            continue
        merged = dict(table.get(tool, {"R": DEFAULT_BUDGETS["gui_default"]["R"],
                                       "Q": DEFAULT_BUDGETS["gui_default"]["Q"]}))
        for key in ("R", "Q"):
            value = entry.get(key)
            if isinstance(value, (int, float)) and value >= 1:
                merged[key] = float(value)
        table[tool] = merged
    return table


def get_budgets() -> dict[str, dict[str, float]]:
    """Return the effective per-tool budget table (defaults + persisted overrides)."""
    global _cache
    with _lock:
        if _cache is None:
            try:
                settings = load_settings() or {}
            except Exception:
                settings = {}
            _cache = _merge(settings.get(_SETTINGS_KEY))
        return {tool: dict(entry) for tool, entry in _cache.items()}


def budget_for(tool: str) -> dict[str, float]:
    """Return ``{"R": ..., "Q": ...}`` for *tool*, falling back to gui_default."""
    table = get_budgets()
    return table.get(tool, table["gui_default"])


def set_gui_budget(tool: str, R: float, Q: float | None = None) -> dict[str, Any]:
    """Apply, persist and echo a per-tool budget. Bounds [5, 3600] seconds.

    Returns ``{"success": True, "budgets": <new table>}`` so the client can
    re-derive its socket timeouts from the echo instead of keeping a second copy.
    """
    global _cache
    try:
        run = float(R)
        queue = run if Q is None else float(Q)
    except (TypeError, ValueError):
        return {"success": False, "error": f"invalid budget: R={R!r} Q={Q!r}"}

    if not isinstance(tool, str) or not tool:
        return {"success": False, "error": f"invalid tool name: {tool!r}"}

    for name, value in (("R", run), ("Q", queue)):
        if not (MIN_BUDGET <= value <= MAX_BUDGET):
            return {
                "success": False,
                "code": "BUDGET_OUT_OF_BOUNDS",
                "error": (
                    f"{name}={value:g}s is outside the permitted range "
                    f"[{MIN_BUDGET:g}, {MAX_BUDGET:g}] seconds"
                ),
            }

    with _lock:
        table = _merge((load_settings() or {}).get(_SETTINGS_KEY)) if _cache is None else dict(_cache)
        table = {name: dict(entry) for name, entry in table.items()}
        table[tool] = {"R": run, "Q": queue}
        _cache = table
        try:
            settings = load_settings() or {}
            settings[_SETTINGS_KEY] = {
                name: {"R": entry["R"], "Q": entry["Q"]} for name, entry in table.items()
            }
            save_settings(settings)
        except Exception as exc:  # persistence is best effort; the live table still changed
            return {
                "success": True,
                "persisted": False,
                "warning": f"budget applied but not persisted: {type(exc).__name__}: {exc}",
                "budgets": {name: dict(entry) for name, entry in table.items()},
            }
        return {
            "success": True,
            "persisted": True,
            "budgets": {name: dict(entry) for name, entry in table.items()},
        }


def assert_table(table: dict[str, dict[str, float]]) -> None:
    """Addon-start assertion: every entry has R >= 1 and Q >= 1. Raises ValueError."""
    if not isinstance(table, dict) or not table:
        raise ValueError("budget table is empty or not a mapping")
    for tool, entry in table.items():
        if not isinstance(entry, dict) or "R" not in entry or "Q" not in entry:
            raise ValueError(f"budget entry for {tool!r} is malformed: {entry!r}")
        for key in ("R", "Q"):
            value = entry[key]
            if not isinstance(value, (int, float)) or value < 1:
                raise ValueError(f"budget {tool}.{key} must be >= 1 second, got {value!r}")


def _reset_cache_for_tests() -> None:
    """Drop the memoised table. Test helper; not part of the RPC surface."""
    global _cache
    with _lock:
        _cache = None
