# Build Blueprint — freecad-mcp v0.2.0

**Server slug** `freecad-mcp`
**Release** v0.2.0 — REBUILD of fork `PWereh/freecad-mcp` at `5dbfe2c` (v0.1.23)
**Stage** 2 of 4 — transport-architect. This blueprint decides; it builds nothing and dispatches
nothing to FreeCAD.
**Build root (this run)** `C:\Claude\freecad-mcp-v0.2.0\`

## Precedence, restated and obeyed

1. `260916-freecad-mcp-v0.2.0-architecture-rev0.md` — **authoritative**, amended since the manifest
2. `260916-freecad-mcp-v0.2.0-wireframe-rev0.md` — authoritative layout, subordinate to (1)
3. `260916-freecad-mcp-bridge-contract-rev0.md` — the requirements, BC-01..BC-08
4. `C:\Claude\freecad-mcp-v0.2.0\design\tool-manifest.md` — Stage 1; **its §9 predates the
   amendments** and is superseded where §11 below says so
5. the measured baseline at `5dbfe2c` — wins wherever all four are silent

This blueprint conforms to that design. It does not derive a competing one. Where it adds a
decision, the decision is a *build* decision the three documents left to this stage, and it is
labelled as such.

## The constraint that does not move

**Nothing in this pipeline writes to `C:\Claude\freecad-mcp`.** That is the installed, working
bridge and the only thing currently able to drive FreeCAD. The build target is
`C:\Claude\freecad-mcp-v0.2.0\`. **The swap to the installed location is a human decision after
BC-08 is green.** The addon half additionally cannot be installed by this pipeline at all — see §3.

---

## 1 · Transport, runtime and protocol era — decided, with reasons

### 1.1 Transport: `stdio`

> **Decision: `stdio`. One target: LOCAL. No HTTP entrypoint is built.**

The transport choice at revision `2026-07-28` is **two-way and only two-way**: `stdio` (LOCAL) or
**Streamable HTTP** (CONTAINERIZED). They are separate selections, never a hyphenated pair. The
older HTTP+SSE dual-endpoint design is **Deprecated** as of `2025-03-26`, is not a selectable
target of this harness, and **no tool in this blueprint is routed through it**.

**Reason stdio, stated so it is not re-litigated:**

- `freecad-mcp` is a **local XML-RPC client of a GUI process on `127.0.0.1:9875`**. The server
  process must run on the same machine as the FreeCAD GUI it drives. A network transport in front
  of a loopback-only backend buys nothing and adds an unauthenticated hop.
- BC-06 — *the client dies with its parent* — is defined in terms of **stdin EOF**. That signal
  exists only under stdio. Under HTTP there is no parent pipe, and the orphan class this release
  exists to kill (`freecad-mcp.exe` outliving its spawner, three measured on 16 Sep 2026) has no
  detector.
- `execute_code_headless` spawns `freecadcmd` **on the server machine**. Its semantics are already
  documented as local-only ("runs on the MCP server machine, independently of `--host`").
- The sole authentication in the whole stack is the addon's loopback host filter
  (`ip_filter.py`, default `127.0.0.1`). Exposing an HTTP port in front of an arbitrary-code
  execution tool (`execute_code`) with no other auth layer is not a thing this design does.

**Two links, one transport.** The MCP transport is *only* the first hop. The second is not a
transport decision of this stage and is preserved unchanged:

```
MCP client ──stdio (MCP, this decision)──▶ freecad-mcp.exe ──XML-RPC/loopback:9875──▶ FreeCAD addon
```

**Streamable HTTP is not selected, therefore its `2026-07-28` header-routing rules are out of
scope for this build and no code implements them.** Recorded once so Stage 3 does not invent a
gateway: had HTTP been selected, every POST would carry `MCP-Protocol-Version`, every request
`Mcp-Method`, and every `tools/call` / `resources/read` / `prompts/get` an `Mcp-Name`; any proxy
in front would have to forward all three unmodified, and a header disagreeing with the request
`_meta` value is a `400` with `-32020`. **None of that applies here.** There is no proxy, no
gateway, no port, and no `server_http.py`. Only `stdio` is built.

### 1.2 Runtime: Python, official MCP Python SDK, class `MCPServer`

> **Decision: Python. `mcp[cli]>=2.1.1,<3` (pinned `mcp==2.1.1` for build and test),
> server class `MCPServer`, entrypoint `mcp.run()` with no transport argument (stdio is the
> default and is what `run()` selects with no argument).**

- **Reason Python, not Node.** The addon half is Python running *inside FreeCAD's bundled
  CPython 3.11*, and the client half already exists in Python. A Node client would double the
  language surface across a two-process deliverable that shares one XML-RPC dialect, and would
  re-author 17 preserved tool signatures whose compatibility is the release's central constraint.
  No part of this is a decision the harness left open; it is recorded because the blueprint must
  state it rather than imply it.
- **Venv** Python 3.12 (`requires-python >=3.12` preserved). **Addon** runs under FreeCAD 1.1's
  bundled Python 3.11 and is *not* pip-installed.
- **Import shim is PRESERVED verbatim** from the baseline (`server.py:5-13`): try
  `mcp.server.fastmcp` (1.x) then fall back to `mcp.server.mcpserver.MCPServer as FastMCP` (2.x).
  In 2.x the canonical path is `from mcp.server import MCPServer`; the baseline's
  `mcp.server.mcpserver` path resolves to the same class. Deleting the shim is a needless
  divergence from a preserved file and is not done.
- The local alias `FastMCP` stays as the in-module name so the 17 `@mcp.tool(structured_output=False)`
  decorations and the `Context` injection are byte-for-byte the baseline's.
- Dependencies unchanged otherwise: `validators>=0.34.0` (used by `--host`), dev `pytest>=8,<10`.

### 1.3 Protocol era — the line the Forge copies

> **Target revision `2026-07-28`, dual-era: the server answers the modern `server/discover`
> method AND the legacy `initialize` handshake, so neither a modern nor a legacy client meets a
> compatibility cliff.**

**Client-side detection rule, as the transport reference carries it:** probe `server/discover`
first and fall back to the legacy `initialize` handshake on **any unrecognised error** — never on
one specific error code. A fallback keyed to a single code breaks the first time a server returns
a different one for the same "I do not know that method" condition.

Both eras are served by the SDK's stdio transport layer; **no handshake code is hand-written in
this tree**. Stage 3 writes tools, not handshakes.

*Harness consequence, reported in §11 (OPEN-11): rxCAD's project facts currently pin the installed
bridge at protocol `2025-11-25` with 17 tools. After the swap it is `2026-07-28` with 20.*

---

## 2 · Binding strategy

Three binding patterns exist in this server and **no fourth is introduced**:

| key | pattern | mechanism |
|---|---|---|
| **X** | XML-RPC, **off** the GUI thread | `xmlrpc.client.ServerProxy(...).method(args)` → `FreeCADRPC.<method>` answers directly. Survives a wedged GUI. |
| **G** | XML-RPC, **crossing the GUI-thread boundary** | as X, then `dispatch_to_gui(task, timeout=R, queue_timeout=Q, operation_name=...)` — public wrapper, **rejection check on**, `track_health=True` |
| **B** | XML-RPC, **crossing the boundary on the BYPASS path** | as X, then `health_probe.probe_gui(cap)` → `_enqueue_and_wait(task, R, Q, track_health=False)` — **no rejection check, no `DispatchHealth` mutation**. Exactly one tool uses it. |
| **W** | XML-RPC that **starts a worker thread**; the worker crosses the boundary only through `commit()`/`commit_many()` (which are **G** internally, budget key `commit`) |
| **P** | **pure client-side** — no XML-RPC at all. Subprocess/CLI wrap of `freecadcmd`. |

There is **no native FFI binding** in this server and no REST client. The XML-RPC hop is stdlib
(`xmlrpc.client`), the CLI hop is stdlib (`subprocess`), and both are preserved from the baseline.
Because the selected target is LOCAL/stdio, the CONTAINERIZED native-library provisioning question
(base image, FFI availability, licensing) **does not arise** — there is no image. Recorded
explicitly so it is not mistaken for an unanswered question: FreeCAD 1.1 is an installed desktop
GUI application at `C:\Program Files\FreeCAD 1.1`, driven in-process by an addon, and cannot be
containerised behind this bridge without also containerising the GUI it exists to drive.

**Transport surface preserved:** XML-RPC over loopback 9875, `allow_none=True`,
`_TimeoutTransport` with a per-call socket timeout, `logRequests=False`,
`FilteredXMLRPCServer(ThreadingMixIn, SimpleXMLRPCServer)` with `daemon_threads = True`, and
`register_instance(FreeCADRPC())` — which **auto-exposes every public method**, so the three new
RPC methods need no registration statement, only a public method on `FreeCADRPC`.

---

## 3 · The two-process split — which files land where

This is a **two-deliverable release**. The build tree contains both halves; the pipeline can
install only one of them.

### 3.1 CLIENT — `C:\Claude\freecad-mcp-v0.2.0\` (this pipeline writes it)

Installed later by a human as the MCP server executable (today
`C:\Claude\freecad-mcp\.venv\Scripts\freecad-mcp.exe`). `▸` new · `Δ` re-authored · unmarked =
copied byte-for-byte from `5dbfe2c`.

```
C:\Claude\freecad-mcp-v0.2.0\
├── pyproject.toml            Δ  version 0.2.0; mcp[cli]>=2.1.1,<3; validators; pytest
├── .env.example              ▸  §7 — no secret values, because there are none
├── README.md                 Δ  install + the human swap procedure (§9.3)
├── design/
│   ├── tool-manifest.md         (Stage 1)
│   └── build-blueprint.md       (this file)
├── src/freecad_mcp/
│   ├── __init__.py              unchanged
│   ├── server.py             Δ  20 MCP tools; payload screen at the top of every execute_code*;
│   │                            stdin-EOF / SIGTERM shutdown; env-var fallbacks behind the flags
│   ├── operations/
│   │   ├── __init__.py       Δ  re-exports 17 + 3 new *_operation functions
│   │   └── core.py           Δ  gains gui_ping_operation, reset_dispatch_health_operation,
│   │                            set_gui_budget_operation; get_async_status_operation renders the
│   │                            new schema fields CONDITIONALLY (§8.3)
│   ├── freecad_client.py     Δ  per-call socket from the addon's reported table; connect-time
│   │                            assertion; gui_ping / reset_dispatch_health / set_gui_budget
│   ├── budgets.py            ▸  pure functions: M = 30.0, assert_table(), socket_for()
│   ├── payload_screen.py     ▸  raw-string XML-1.0 screen with byte/offset/line/col
│   ├── headless.py              unchanged (screen is applied by server.py, before the call)
│   ├── server_state.py       Δ  holds the reported budget table, bridge_contract, jobs_dir
│   ├── responses.py             unchanged
│   └── prompt_text.py           unchanged
├── addon/FreeCADMCP/            ← BUILT HERE, INSTALLED BY A HUMAN (§3.2)
└── tests/
    ├── <the 12 named baseline modules>   preserved, must still pass
    └── test_bridge_contract.py  ▸  BC-01..BC-08 (§10)
```

### 3.2 ADDON — built at `C:\Claude\freecad-mcp-v0.2.0\addon\FreeCADMCP\`, **installed by a human** to `%APPDATA%\FreeCAD\v1-1\Mod\FreeCADMCP\`

**The build target directory cannot install the addon half.** FreeCAD loads addons from its
versioned user app-data dir only. Stage 3 writes the addon source into the build tree; a human
copies it. Stated plainly because it is the single most likely silent failure of this release.

```
addon/FreeCADMCP/
├── Init.py, InitGui.py              unchanged (InitGui.py owns the RPC auto-start)
└── rpc_server/
    ├── __init__.py                  unchanged
    ├── rpc_server.py            Δ   RPC surface; execute_code takes R from the budget table
    │                                (90 preserved) + optional per-call timeout capped at it;
    │                                gui_ping / reset_dispatch_health / set_gui_budget methods;
    │                                bridge_contract + budgets + connected_clients + jobs_dir
    │                                in get_rpc_status. KEEPS the async worker launch (§8.2).
    ├── gui_dispatch.py          Δ   split into public dispatch_to_gui (rejection check,
    │                                track_health=True, signature UNCHANGED incl. timeout=60)
    │                                over inner _enqueue_and_wait(task, run_budget, queue_budget,
    │                                *, track_health); heartbeat timestamp per tick (§5.2)
    ├── dispatch_health.py       Δ   snapshot gains stuck_since + last_gui_heartbeat_age_s;
    │                                new reset_stale() clearing ONLY when _timed_out is set
    ├── health_probe.py          ▸   probe_gui(cap) on the BYPASS path; gui_ping;
    │                                reset_dispatch_health(force)
    ├── async_jobs.py            ▸   job record store + state machine + emit/result/plan/chunk +
    │                                commit_many + the reference validator + file persistence
    ├── budgets.py               ▸   DEFAULT_BUDGETS table, get_budgets(), set_gui_budget()
    │                                — see OPEN-1: the wireframe puts this in settings.py
    ├── connections.py           ▸   connect/disconnect tracking with peer port (§8.4)
    ├── settings.py                  unchanged surface (load_settings/save_settings) — see OPEN-1
    ├── commands.py                  unchanged
    ├── object_factory.py            unchanged
    ├── property_mapper.py           unchanged
    ├── serialize.py                 unchanged
    ├── view_manager.py              unchanged
    ├── parts_library.py             unchanged
    ├── ip_filter.py                 unchanged
    ├── fem_executor.py              unchanged — FEM out of contract scope
    └── object_validation.py         unchanged
```

### 3.3 The split, by responsibility

| concern | CLIENT (`freecad-mcp-v0.2.0`) | ADDON (`%APPDATA%\FreeCAD\v1-1\Mod\FreeCADMCP`) |
|---|---|---|
| MCP transport (stdio), tool schemas, docstrings | ✔ | — |
| payload screen (BC-03) | ✔ **before marshalling** | **not built** — a payload that reached the addon already parsed |
| budget **ownership** | — | ✔ one table, persisted |
| budget **derivation** (socket S) | ✔ per call from the reported table | — |
| GUI-thread boundary, health, stuck flag | — | ✔ |
| probe / reset (BC-04/05) | ✔ tool + client method | ✔ mechanism |
| async job store, result channel, chunking (BC-01/07) | ✔ rendering only | ✔ store, state machine, file receipts |
| lifecycle: stdin EOF / SIGTERM (BC-06) | ✔ | — |
| connection counting (BC-06) | — | ✔ |
| headless `freecadcmd` (P binding) | ✔ entirely | — |

### 3.4 Install ordering — a property that de-risks the human swap

**The addon half is backward compatible with the v0.1.23 client; the client half is not backward
compatible with the v0.1.23 addon — by design.**

- Addon-side `execute_code(self, code, timeout=None)` and the additive `get_rpc_status` fields mean
  a **v0.1.23 client keeps working against a v0.2.0 addon**.
- A **v0.2.0 client against a v0.1.23 addon is refused at connect** with the version named
  (BC-08's canary) — deliberately, because it would otherwise call `execute_code(code, timeout)`
  with two arguments and get an opaque XML-RPC `Fault`.

**Therefore the human swap is: install the addon first, restart FreeCAD, verify, then swap the
client.** Never the reverse. Delete `rpc_server\__pycache__\` when copying. The user app-data dir
is **versioned** — `%APPDATA%\FreeCAD\v1-1\`, not `%APPDATA%\FreeCAD\`; installing to the
unversioned path fails silently.

---

## 4 · Per-tool binding table — all 20 tools, none unmapped

Binding keys per §2. `R`/`Q` are the addon-side run/queue budgets drawn from the table (§5.1);
`S` is the client socket timeout, derived per call. `ctx: Context` is FastMCP's injected parameter
and is **never** an input-schema property.

| # | MCP tool | binding | concrete call pattern | R / Q | socket **S** | env vars read | tier |
|--:|---|:--:|---|---|---|---|---|
| 1 | `create_document` | **G** | `proxy.create_document(name)` → `dispatch_to_gui(_create_document_gui, timeout=R, queue_timeout=Q, operation_name="create_document")` | 60/60 | 150 | HOST, PORT | — |
| 2 | `create_object` | **G** | `proxy.create_object(doc_name, obj_data)` → `dispatch_to_gui(..., "create_object")` | 60/60 | 150 | HOST, PORT | — |
| 3 | `edit_object` | **G** | `proxy.edit_object(doc_name, obj_name, properties)` → `dispatch_to_gui(..., "edit_object")` | 60/60 | 150 | HOST, PORT | — |
| 4 | `delete_object` | **G** | `proxy.delete_object(doc_name, obj_name)` → `dispatch_to_gui(..., "delete_object")` | 60/60 | 150 | HOST, PORT | — |
| 5 | `execute_code` | **G** | `screen(code)` **client-side first**; then `proxy.execute_code(code, timeout)` → `dispatch_to_gui(task, timeout=min(timeout or R, R), queue_timeout=Q, operation_name="execute_code")` | **90/90** | **210** (`max(configured, Q+R+M)`) | HOST, PORT | — |
| 6 | `execute_code_async` | **W** | `screen(code)` first; then `proxy.execute_code_async(code)` → worker thread; namespace gains `emit/result/plan/chunk/commit/commit_many` | worker: n/a; each `commit` 120/120 | 150 | HOST, PORT | — |
| 7 | `execute_code_headless` | **P** | `screen(code)` first; then `subprocess.run([freecadcmd, script], timeout=timeout)` — **no XML-RPC at all** | n/a | n/a (600 s process timeout) | FREECADCMD | — |
| 8 | `get_async_status` | **X** | `proxy.get_async_status(job_id)` on a **fresh** proxy (never shares a connection with a blocked GUI request) | off-thread | 150 | HOST, PORT, JOBS_DIR (read-side) | — |
| 9 | `get_object` | **G** | `proxy.get_object(doc, obj)` → `_query_on_gui(...)` → `dispatch_to_gui(..., "get_object")` | 60/60 | 150 | HOST, PORT | — |
| 10 | `get_objects` | **G** | `proxy.get_objects(doc)` → `_query_on_gui(...)` → `dispatch_to_gui(..., "get_objects")` | 60/60 | 150 | HOST, PORT | — |
| 11 | `get_parts_list` | **X** | `proxy.get_parts_list()` — library scan, off-thread | off-thread | 150 | HOST, PORT | — |
| 12 | `get_rpc_status` | **X** | `proxy.get_rpc_status()` on a fresh proxy. **The connect-time source of the budget table**, `bridge_contract`, `connected_clients`, `jobs_dir`, heartbeat age | off-thread | 150 | HOST, PORT | — |
| 13 | `get_view` | **G** | client method name differs: `FreeCADConnection.get_active_screenshot(view_name, width, height, focus_object)` → `proxy.get_active_screenshot(...)` → `dispatch_to_gui(..., "get_active_screenshot")` | 60/60 | 150 | HOST, PORT | — |
| 14 | `insert_part_from_library` | **G** | `proxy.insert_part_from_library(relative_path)` → `dispatch_to_gui(..., "insert_part_from_library")` | 60/60 | 150 | HOST, PORT | **DENY** |
| 15 | `list_documents` | **G** | `proxy.list_documents()` → `_query_on_gui(...)` — the cheapest *real* GUI round trip | 60/60 | 150 | HOST, PORT | — |
| 16 | `reload_document` | **G** | `proxy.reload_document(doc_name)` → `dispatch_to_gui(..., "reload_document")` | 60/60 | 150 | HOST, PORT | — |
| 17 | `run_fem_analysis` | **G** | `proxy.run_fem_analysis(doc, analysis, timeout)` → `dispatch_to_gui(..., timeout=timeout, "run_fem_analysis")` | 600/600 (call param overrides R) | **1230** | HOST, PORT | UNPROVEN |
| **18** | **`gui_ping`** ▸ | **B** | `proxy.gui_ping(cap)` → `health_probe.probe_gui(cap)` → `_enqueue_and_wait(noop, run_budget=cap, queue_budget=cap, track_health=False)` — **the only caller allowed past a `stuck` flag** | cap/cap (default 5/5) | **40** | HOST, PORT | **ALLOW / P0** |
| **19** | **`reset_dispatch_health`** ▸ | **X→B** | `proxy.reset_dispatch_health(force)` → `probe_gui(cap=5)`; on `alive` → `DispatchHealth.reset_stale()`; else return unchanged | probe 5/5 | **40** | HOST, PORT | **RESTRICTED** |
| **20** | **`set_gui_budget`** ▸ | **X** | `proxy.set_gui_budget(tool, R, Q)` → bounds-check `[5, 3600]`, apply to the table, persist, **echo the new table**; client re-derives | off-thread | 150 | HOST, PORT | **RESTRICTED** |

**Count: 20.** 17 preserved signatures + `gui_ping` + `reset_dispatch_health` + `set_gui_budget`.

### 4.1 Notes carried onto the table

- **N-1 `ViewName`** is a closed `Literal` declared once in `server.py`; renders as
  `{"type":"string","enum":[…]}`. Unchanged.
- **N-2 `get_view.view_name` carries no default.** Preserved — a default would change the schema's
  `required` array.
- **N-3 `create_object.obj_properties: dict[str, Any] = None`** — the non-`Optional` annotation
  with a `None` default is preserved **verbatim**. "Fixing" it turns `{"type":"object"}` into an
  `anyOf` and is a signature change under BC-08.
- **N-4 return encoding (GAP-A, resolved).** All three new tools are
  `@mcp.tool(structured_output=False)` returning `list[TextContent]` built by
  `responses.json_response(dict)` — the baseline's universal convention, confirmed at
  `operations/core.py:282-288`. rxCAD parses text; a structured-output tool would change how its
  preflight reads the probe.
- **N-5** `include_screenshot` is overridden by the server-wide `--only-text-feedback`. Preserved.
- **N-6 `execute_code.timeout`** may request **less** than the table's R; requesting more is a
  **structured error naming the cap**, never a silent clamp.
- **N-7 `gui_ping.cap`** is passed as **both** run and queue budget. `alive` means exactly *"a
  no-op completed on the GUI thread within `cap`"*. A legitimately busy GUI returns
  `alive: false` with `health.state: "busy"` — **not the same as dead**; dead is
  `last_gui_heartbeat_age_s` growing without bound. The structural worst case is
  `cap + cap = 10 s` (start just inside the queue deadline, then run); in practice it is `cap + ε`
  because a no-op cannot outlive its own start. `S = 40` covers even the structural case, so the
  outcome always arrives as a **structured result**, never a bare socket timeout.
- **N-9 no credentials anywhere.** No tool takes a token, key or licence value. The only
  authentication in the stack is the addon's loopback host filter. `.env.example` therefore carries
  paths and hosts only (§7).
- **N-10 units.** FreeCAD is millimetre-native; every geometry value crossing these tools is mm.
  Every budget/timeout is **seconds**.

---

## 5 · Budgets — the addon owns one table, the client derives one socket

### 5.1 The table (addon-owned, persisted, echoed)

Every value is the measured `5dbfe2c` behaviour. **There is no single knob and no server flag.**

```python
DEFAULT_BUDGETS = {                       # seconds
    "execute_code": {"R": 90,  "Q": 90},  # rpc_server.py:120, passed explicitly at :364
    "gui_default":  {"R": 60,  "Q": 60},  # dispatch_to_gui signature default, gui_dispatch.py:183
    "fem":          {"R": 600, "Q": 600}, # run_fem_analysis timeout param default
    "commit":       {"R": 120, "Q": 120}, # _commit_async default, rpc_server.py:86
    "probe":        {"R": 5,   "Q": 5},   # gui_ping cap; health-UNTRACKED
}
```

- `Q` defaults to `R` for every call, exactly as `gui_dispatch.py:212` does today.
- **`gui_dispatch.py` does not import the table.** Its signature default stays the literal
  `timeout: float = 60` — that literal is simultaneously the preserved signature and
  `gui_default.R`. Every call site in `rpc_server.py` passes explicit `timeout=R,
  queue_timeout=Q` resolved from the table. This keeps `tests/test_gui_dispatch.py` untouched and
  keeps the budget import out of the hot path. **Build decision.**
- `FreeCADRPC.TIMEOUT = 60` (`rpc_server.py:119`) is **dead** — declared, never referenced.
  **Delete it.** A forge re-authoring budgets would otherwise mistake it for the source of R.

### 5.2 The derivation (client-side, pure)

```python
M = 30.0                                       # RPC_TIMEOUT_MARGIN, preserved
def assert_table(table: dict) -> None          # every tool has R >= 1 and Q >= 1
def socket_for(table, tool, R_override=None) -> float:
    b = table[tool]; R = R_override if R_override is not None else b["R"]
    return b["Q"] + R + M                      # S = Q + R + M
```

At the shipped table: **210 / 150 / 1230** for `execute_code` / `gui_default` / `fem` — the
baseline exactly, now with one reason instead of three `2·X + margin` expressions.

> **THE ENVELOPE IS LOAD-BEARING.** `socket_for` is pure and returns `Q + R + M`. The **caller** in
> `freecad_client.py` applies `max(self._timeout, socket_for(...))`. Dropping the `max(...)` — as
> the wireframe's §4 pseudocode literally reads — **fails two preserved baseline rows** of
> `tests/test_client_timeouts.py::test_long_call_socket_budgets_and_cleanup`: the rows
> `("execute_code", ("pass",), 500, 500)` and `("run_fem_analysis", (...,600), 2000, 2000)` assert
> `make_proxy.call_args.args[0] >= configured` for a connection configured **above** the derived
> value. See OPEN-2.

**Connect sequence, ordered:** construct `FreeCADConnection` at the literal default `timeout=150`
→ `ping()` → `get_rpc_status()` → `assert_table(budgets)` and `bridge_contract == "v0.2.0"` →
store table + `jobs_dir` in `server_state` → rebuild the persistent proxy at
`socket_for(table, "gui_default")`. The assertion lives inside `get_freecad_connection()`, **not**
at process start, so the baseline's "start even when FreeCAD is not running" behaviour survives; a
stale addon is refused per call with **both versions named**, not by refusing to boot.

### 5.3 The invariant

> **`S ≥ Q + R + M`, with `M ≥ 30`.** Asserted **in `socket_for`'s tests, not at runtime.**

Today's `execute_code` nesting is `90 + 90 + 30 = 210 = S` — an **equality**, and it **must pass**.
A strict `<` would refuse to boot the working baseline. There is **no `C`** ("client tool wait"):
the client blocks on the socket and nothing else (GAP-D, deleted). Nothing else is asserted;
`Q ≤ R` is **not** required, because a cold-start `Q` may legitimately exceed `R`.

### 5.4 Configuration is an RPC, not a flag

`set_gui_budget(tool, R, Q=None)` — bounds `[5, 3600]`, applied to the table, persisted, and the
**new table echoed**; the client re-derives from the echo. **RESTRICTED** in the harness tiering:
an operator's decision, never a payload's. One copy, one derivation, nothing pushed and nothing
compared across a boundary.

`--gui-budget` / `--queue-budget` / `--execute-timeout` **do not exist**. The contract's BC-02
acceptance text (`--execute-timeout 300`) reads as `set_gui_budget("execute_code", 300)`.

---

## 6 · Health, probe and reset — the bypass path

### 6.1 The split (CONFLICT-04, resolved at source)

`dispatch_to_gui` is refactored into:

- **public `dispatch_to_gui(task, timeout=60, operation_name=None, queue_timeout=None)`** —
  signature **unchanged**; keeps the rejection check (`gui_dispatch.py:208-210`) and calls the
  inner with `track_health=True`. Behaviour for every one of the 17 existing tools is unchanged.
- **inner `_enqueue_and_wait(task, run_budget, queue_budget, *, track_health)`** — the FIFO
  `_rpc_request_queue`, the per-call response queue, the Qt `_waker` wake, the two-phase
  queue/run budgeting, and the mouse/popup/modal and re-entrancy guards, all verbatim.

**`probe_gui(cap)` in `health_probe.py` calls the inner DIRECTLY with `track_health=False`** —
same request queue, same per-call response queue, same waker, but:

- **no rejection check** — the probe is the one caller allowed to look past the flag;
- **no `DispatchHealth.start()` / `finish()` / `mark_timed_out()`** — `start()` overwrites
  `_active_task_id` and would **corrupt the stuck task's identity**. The probe stays out of
  `DispatchHealth` entirely and reads the snapshot only to *report* it.

Outcomes become meaningful: thread free + flag stale → `alive: true` with `health.state: "stuck"`
(the signature BC-05 acts on); thread genuinely blocked → queue give-up at `cap` → `alive: false`
with a large `heartbeat_age`; GUI busy inside budget → `alive: false`, `health.state: "busy"`,
latency ≈ `cap`.

`reset_dispatch_health(force)` calls **`probe_gui`, not the `gui_ping` tool**, and clears through
`DispatchHealth.reset_stale()`, **which clears only when `_timed_out` is set**. On a dead thread it
returns `{reset: false, reason: "GUI thread not answering", stuck_since, heartbeat_age_s}` and
**changes nothing**.

### 6.2 Heartbeat — where the tick is recorded

**Build decision, because the placement is the whole mechanism.** Record
`_last_gui_tick = time.monotonic()` inside `process_gui_tasks` **after the `_processing`
re-entrancy guard's early return** (`gui_dispatch.py:121-122`) and before the queue-empty check.

- After the guard, so a long task calling `updateGui()` / `processEvents()` — which re-enters
  `process_gui_tasks` and returns at line 122 — **cannot forge a tick** and cannot make a wedged
  thread look alive.
- Before the queue-empty check, so an **idle** GUI still ticks every 500 ms. The `finally` at
  `gui_dispatch.py:165-168` reschedules the chain on every path, so the chain does not stall when
  idle; it stalls only when the GUI thread is genuinely not returning — which is precisely the
  signal.
- Canonical owner: the **dispatch snapshot**. `get_rpc_status` **embeds** it; it does not compute
  its own. One value, one home (the manifest's "two homes for one value" minor, closed).

### 6.3 Nothing interrupts a running GUI task

Impossible from outside the GUI thread; this design does not pretend otherwise. A GUI timeout
remains a **session event**, not a tool failure: the mutation is indeterminate and the caller
re-preflights (`gui_ping`) and performs a measured read-back before any further mutation.

---

## 7 · Configuration surface and `.env.example`

**Precedence: CLI flag > environment variable > default.** The flags are preserved exactly —
`--only-text-feedback`, `--host`, and the **quoted** `--freecadcmd` — and no flag is added.
Env vars are additive fallbacks so Stage 3 can externalise install-specific paths.

| variable | side | default | purpose |
|---|---|---|---|
| `FREECAD_MCP_HOST` | client | `localhost` | XML-RPC host. `--host` wins. Loopback in practice. |
| `FREECAD_MCP_PORT` | client | `9875` | XML-RPC port. **New**: the port is hard-coded at `server.py:83` today; env-only, no flag. |
| `FREECAD_MCP_FREECADCMD` | client | auto-detect | `freecadcmd` command for `execute_code_headless`. `--freecadcmd` wins. **Quote the path.** |
| `FREECAD_MCP_ONLY_TEXT_FEEDBACK` | client | unset | `1` ⇒ suppress screenshots. `--only-text-feedback` wins. |
| `FREECAD_MCP_LOG_LEVEL` | client | `INFO` | logger level. |
| `FREECAD_MCP_JOBS_DIR` | **addon** | `%LOCALAPPDATA%\freecad-mcp\jobs` | where job receipts are written. |
| `FREECAD_MCP_OUTPUT_CAP` | **addon** | `1048576` | `emit()` buffer cap in bytes; overflow sets `output_truncated`. |

**The two addon-side variables are read in the FreeCAD process, not the client's.** An env var set
for `freecad-mcp.exe` does not reach the addon. Resolution order addon-side: env → 
`freecad_mcp_settings.json` → default; and **`get_rpc_status` echoes the effective `jobs_dir`**, so
the client reads receipts from a reported path and never guesses one.

**There are no secrets.** No token, key, licence value or credential exists anywhere in this
server; `.env.example` therefore contains hosts, ports and paths only, with **no real values for
anything install-specific** — placeholders only.

---

## 8 · Build constraints derived from the preserved baseline tests

BC-08 requires the **12 named baseline modules** to still pass. Four of them constrain the
re-authoring in ways no design document mentions. Stage 3 must honour all four or it will produce
a tree that looks green on the new tests and fails the old ones.

### 8.1 `test_rpc_handlers.py:56` stubs `rpc_server.settings` with **only** `load_settings` / `save_settings`

Any `from rpc_server.settings import get_budgets, …` in `rpc_server.py` raises **ImportError**
against that stub and kills the whole module. This is why §3.2 places the table in a **new addon
module `rpc_server/budgets.py`** that persists *through* `settings.load_settings/save_settings`
(which the stub does provide, returning `{}` → the defaults). See **OPEN-1** — the wireframe names
`settings.py` as the home. The disagreement is about *file location only*; "the addon owns one
table, persisted" holds either way.

### 8.2 `test_rpc_handlers.py` monkeypatches **`rpc_module.dispatch_to_gui`, `rpc_module.time` and `rpc_module.threading`**

(lines 413, 415, 442, 456-463). Those names must still resolve **in `rpc_server.py`'s globals at
call time**. Therefore: `async_jobs.py` takes the **record store, state machine and helpers**, and
`rpc_server.py` **keeps** the worker launch (`threading.Thread(...)`), the `time.time()` stamps and
the `dispatch_to_gui(...)` calls for `_set_status` / `_clear_status` / `_commit_async`, injecting
the dispatch callable into `async_jobs` rather than importing it there. `_clear_status` must keep
being called with `operation_name="clear_async_status"` **as a keyword** — line 457 keys on it.

### 8.3 `test_async_status_text.py:38-41` asserts the **exact rendered string** for one job

```
"Async job job-7: failed\nError: ValueError: boom\nTraceback ...\nValueError: boom"
```

`get_async_status_operation` renders `error` and `traceback` **only when present**
(`operations/core.py:183-188`). The v0.2.0 renderer must extend that rule, not break it: emit
`result`, `output`, `output_truncated` and a `chunks n/plan_chunks` line **only when the field is
present**, so a record without them renders byte-identically to `5dbfe2c`.

### 8.4 `connected_clients` needs a connection *lifetime*, not an accept

`ip_filter.verify_request` sees only the accept and never the close. Count in the
**request handler's `setup()` / `finish()`** (peer port from `client_address[1]`), which is the
true connection lifetime. See **OPEN-3** for why the resulting number may not answer the question
BC-06 asks.

---

## 9 · Build order

The contract's order, with one step prepended that the contract assumes and does not name.

| step | scope | gate |
|--:|---|---|
| **0** | **Scaffold**: copy `5dbfe2c` into the build tree byte-for-byte (both halves), bump to 0.2.0, pin `mcp[cli]>=2.1.1,<3` | the **12 named baseline modules** green **before anything changes**. Without this baseline every later step is unmeasurable. |
| **1** | **BC-03** payload screen (client-only, no design risk) + **BC-06** stdin-EOF / SIGTERM + connection counting | `TestBC03_*` green; `TestBC06_*` green |
| **2** | **BC-04** `probe_gui` bypass split, heartbeat, `gui_ping` — **probe before reset** | `TestBC04_*` green, incl. `test_ping_alive_while_flag_stuck_and_thread_free` |
| **3** | **BC-05** `reset_stale()` + `reset_dispatch_health` | `TestBC05_*` green |
| **4** | **BC-01** job-result schema, `emit/result/plan/chunk`, file receipts, reference validator — **result channel before chunking** | `TestBC01_*` green |
| **5** | **BC-07** `commit_many`, `partial` as a first-class state | `TestBC07_*` green |
| **6** | **BC-02** budget table, `socket_for`, connect assertion, `set_gui_budget` (tool 20), `execute_code.timeout` | `TestBC02_*` green; 12 baseline modules **still** green |
| **7** | **BC-08** canaries, written **from the measured results** | all of §10 green |
| **8** | **HUMAN**: install the addon → restart FreeCAD → verify → swap the client (§3.4) | rxCAD rev-E payload completes via `execute_code_async` + `commit_many`, verdict from `get_async_status` **matching its own result file**; no orphan `freecad-mcp.exe` |

Steps 1-7 write **only** into `C:\Claude\freecad-mcp-v0.2.0\`. Step 8 is the only one that touches
an installed location, and it is not this pipeline's to take.

---

## 10 · BC-01 job-result schema — a first-class shared interface

**A second harness consumes this as a receipt (rxCAD pass 2, checks A1/A2). Its state rules are
part of the interface, not commentary.** Returned by `get_async_status(job_id)`; also written to
`<jobs_dir>\<job_id>.json` on every state change, by **atomic rename**. Two independent routes to
the same record: when the RPC thread is unreachable, the verdict is still readable from disk.

```json
{
  "id":               "job-<hex32>",
  "state":            "running | done | partial | failed",
  "started":          1789572000.12,
  "finished":         1789572310.88,
  "code_preview":     "first 120 chars",
  "plan_chunks":      5,
  "chunks": [
    { "name": "TransverseSection", "state": "ok | failed",
      "started": 0.0, "finished": 0.0,
      "result": {"edges": 28}, "error": null }
  ],
  "result":           { "...": "json from result(), else the payload's return value" },
  "output":           "text appended by emit(), per-job, thread-local",
  "output_truncated": false,
  "error":            null,
  "traceback":        null
}
```

Types: `id:string`; `state:string` (**open** enum); `started|finished:number|null`;
`code_preview:string`; `plan_chunks:integer|null`; `chunks:array<object>`; `result:any|null`;
`output:string`; `output_truncated:boolean`; `error:string|null`; `traceback:string|null`.

### 10.1 State rules — shipped in the schema's own documentation and as a reference validator beside it

1. `done` **requires** `len(chunks) == plan_chunks` **and** every chunk `ok`. **The store enforces
   this**; a worker cannot mark itself `done` otherwise.
2. A worker that returns before reporting `plan_chunks` chunks is **`partial`**, not `done`, even
   if it raised nothing.
3. A worker that raises is `failed`; chunks already recorded are **kept**.
4. Single-shot jobs (`plan()` never called) have `plan_chunks: null`, `chunks: []`; completeness is
   `state == "done"`.
5. **A consumer validates completeness by the COUNT, not the WORD:**

   > `valid_receipt := (state == "done") and (plan_chunks is None or len(chunks) == plan_chunks)`

   Checking the state word alone is the bug this design exists to remove; the count is the
   cross-check that stops a five-minute payload from presenting a partial verdict as a whole one.
6. Fields are **never removed** within v0.2.x; additions are allowed.
7. **`state` is an OPEN enum.** Widening it is as breaking for a strict consumer as a field
   removal. The rule, stated here because rxCAD A2 is such a consumer: **any `state` other than
   `done` is not-done.** A validator that enumerates `running|done|failed` and treats the rest as
   an error — or worse as success — is wrong by this rule, and a validator that **raises** on an
   unknown state instead of **rejecting** it is equally wrong.

**The reference validator ships beside the schema** and implements rules 5 and 7. It is the thing
BC-08 feeds, not prose.

### 10.2 Injected async namespace

| helper | signature | behaviour |
|---|---|---|
| `commit` | `commit(fn, timeout=120) -> Any` | **preserved**, including the `120` written into `execute_code_async`'s public docstring. 120 is the table's `commit.R`, so a default commit trips its own budget, not `gui_default`'s. |
| `commit_many` | `commit_many(fns, timeout_each=None) -> list` | BC-07 convenience; each `fn` in its own budget; **stops at the first failure** and returns results so far. `None` ⇒ the `commit` table entry's `R` (120), matching `commit`. See OPEN-12. |
| `emit` | `emit(text) -> None` | appends to a **per-job, thread-local** buffer. **No process-wide redirect** — the baseline's reason for not capturing stdout (`rpc_server.py:286`) is correct and stands. Capped (default 1 MiB); overflow sets `output_truncated`. |
| `result` | `result(obj) -> None` | stores a JSON-serialisable value; if never called, the payload's own return value is stored. |
| `plan` | `plan(n_chunks) -> None` | declares the chunk count; recorded as `plan_chunks`. |
| `chunk` | `chunk(name, obj) -> None` | appends `{name, state, started, finished, result|error}`. |

`execute_code_async`'s **MCP signature is unchanged** — these live in the payload's namespace, not
the tool's input schema.

---

## 11 · Test strategy

### 11.1 `tests/test_bridge_contract.py` — one class per requirement, **a failing case per test**

Tests needing a live FreeCAD are marked `@live` and **skip** without one. **The forge writes them
regardless.** The named list, as amended:

- **`TestBC01_ResultChannel`** — `test_emit_and_result_round_trip` · `test_failed_job_carries_traceback` ·
  `test_output_cap_sets_truncated` · `test_return_value_stored_when_result_not_called`
- **`TestBC02_Budgets`** — `test_default_table_is_the_baseline` (90/60/600/120) ·
  `test_socket_for_reproduces_5dbfe2c` (210/150/1230) ·
  `test_invariant_accepts_baseline_equality` (**the failing case is a strict `<`**) ·
  `test_invariant_rejects_short_socket` · `test_set_gui_budget_bounds_and_persists` ·
  `test_execute_code_timeout_capped` · `@live test_connect_refuses_addon_without_table`
- **`TestBC03_PayloadScreen`** — `test_control_byte_reported_with_position` ·
  `test_tab_lf_cr_allowed` · `test_u2028_rejected` · `test_screen_never_uses_splitlines` (static)
- **`TestBC04_GuiPing`** — `test_ping_alive_while_flag_stuck_and_thread_free` (**the test that
  defines the probe**) · `test_ping_does_not_perturb_health` · `test_ping_never_calls_rejection` ·
  `@live test_ping_alive_when_idle` · `@live test_ping_reports_busy_within_cap` ·
  `@live test_ping_reports_wedge`
- **`TestBC05_Reset`** — `@live test_reset_clears_stale_flag` · `test_reset_refuses_when_ping_fails` ·
  `test_force_still_requires_alive`
- **`TestBC06_Lifecycle`** — `test_stdin_eof_exits_zero_and_closes_proxy` · `test_sigterm_exits` ·
  `@live test_connected_clients_count`
- **`TestBC07_Chunking`** — `@live test_commit_many_stays_healthy_between_chunks` ·
  `@live test_single_long_commit_is_stuck` · `test_partial_when_chunks_short_of_plan` ·
  `test_done_requires_all_chunks_ok` · `test_consumer_rule_count_not_word`
- **`TestBC08_Canaries`** — `test_status_reports_bridge_contract_version` ·
  `test_baseline_signatures_are_a_superset` · `test_tool_count_is_20` ·
  `test_baseline_test_modules_by_name` · `test_partial_state_is_not_done_for_reference_validator` ·
  `test_unknown_future_state_is_not_done`

> **`test_ping_is_a_gui_dispatch` is DELETED.** It **passed for a broken implementation** — one
> routed through `dispatch_to_gui`, which is rejected while stuck — so it confirmed the wrong
> property and the suite would not have caught the failure `gui_ping` exists to fix.
> **`test_ping_alive_while_flag_stuck_and_thread_free` replaces it** and fails on exactly that
> implementation.

### 11.2 The 12 named baseline modules — **by NAME, never by count**

`test_async_status_client`, `test_async_status_text`, `test_client_timeouts`,
`test_dispatch_health`, `test_gui_dispatch`, `test_headless`, `test_object_validation`,
`test_parts_library`, `test_rpc_concurrency`, `test_rpc_handlers`, `test_serialize`,
`test_serialize_shape`.

**Verified on disk at `5dbfe2c`: exactly these 12, and no `conftest.py`.** A count is the same
brittleness as an unscoped entity total (the documents once said 13; a glob that reaches `.venv`
says 27). §8 lists the four ways re-authoring can break them.

### 11.3 Signature canary — a **SUPERSET** check

`test_baseline_signatures_are_a_superset`: for each of the 17 preserved tools, **every `5dbfe2c`
parameter is present with the same name, same type and the same default; new optional parameters
are permitted.** Not equality — equality collides with the deliberate addition of `timeout` to
`execute_code` and would fail the very change it exists to permit. **Fails** on a rename, a removed
parameter, or a changed default.

---

## 12 · Reconciliation register — confirmed RESOLVED, not re-raised

| item | status in this blueprint |
|---|---|
| **CONFLICT-01** budget flags vs one knob | **RESOLVED.** No budget flags exist. Ownership is the addon's; configuration is `set_gui_budget` (§5.4). |
| **CONFLICT-02** budget table misread the baseline | **RESOLVED.** `execute_code`'s addon-side R **is 90** (`rpc_server.py:120`, passed at `:364`); the 60 is only `dispatch_to_gui`'s **signature default** for the other GUI tools. A **per-tool table** preserves every value; the client derives **210 / 150 / 1230** — the baseline exactly. No regression, no single knob. |
| **CONFLICT-03** `state` enum 3 vs 4 | **RESOLVED.** `state` is an **OPEN** enum; schema rule 7: **any state other than `done` is not-done**. BC-08 gains `test_partial_state_is_not_done_for_reference_validator` and `test_unknown_future_state_is_not_done`. |
| **CONFLICT-04** probe rejected by the flag it clears | **RESOLVED.** Public wrapper over inner `_enqueue_and_wait`; `probe_gui(cap)` calls the inner **directly** with `track_health=False`, no rejection check, no `DispatchHealth` mutation. `reset_dispatch_health` calls `probe_gui`, not the tool, and clears via `reset_stale()` (§6.1). |
| **CONFLICT-05** `alive` under a busy GUI | **RESOLVED.** Architecture §3.2 pins it: busy inside budget ⇒ `alive: false`, `health.state: "busy"`, latency ≈ cap. `alive` is defined in the schema as *"a no-op completed on the GUI thread within cap"* so `alive:false` is never misread as "FreeCAD is dead" (N-7). |
| **CONFLICT-07** signature canary vs `execute_code.timeout` | **RESOLVED.** Superset check (§11.3). |
| **CONFLICT-08** baseline test count 13 vs 12 | **RESOLVED.** Pinned as a **named list of 12**; verified on disk; no `conftest.py`. |
| **CONFLICT-09** `commit()`'s 120 | **RESOLVED by the table.** `commit` has its own entry at 120/120, so a default `commit()` trips **its own** budget, not `gui_default`'s. The public docstring's `commit(fn, timeout=120)` is preserved. |
| **GAP-A** return encoding of the new tools | **RESOLVED.** `structured_output=False` → `list[TextContent]` via `json_response` (N-4). |
| **GAP-B** wireframe layout omitted files | **RESOLVED.** §3 is the depth-5 tree with every file named, including `src/freecad_mcp/operations/` marked `Δ` (three new operations for three new tools). |
| **GAP-C** nobody sets the addon's R and Q | **RESOLVED.** `set_gui_budget(tool, R, Q)` — bounds `[5, 3600]`, persisted, echoed, **RESTRICTED**. One copy, one derivation. `execute_code(timeout=)` may request **less** than R, never more. |
| **GAP-D** `C` (client tool wait) | **RESOLVED — `C` is DELETED.** It was vacuous: the client blocks on the socket and nothing else. |
| **invariant** | **RESOLVED.** `S ≥ Q + R + M` with `M ≥ 30`; today's `90+90+30 = 210 = S` is an **equality** and **passes**; asserted in `socket_for`'s tests, not at runtime. |
| **tool count** | **RESOLVED: 20** — 17 preserved + `gui_ping` + `reset_dispatch_health` + `set_gui_budget`. Every one of the 17 baseline signatures preserved. |
| **minor: two homes for heartbeat** | **RESOLVED.** Canonical owner is the dispatch snapshot; `get_rpc_status` embeds it (§6.2). |
| **minor: `last_gui_heartbeat` vs `_age_s`** | **RESOLVED.** The **age** form wins (architecture); the contract's acceptance sentence reads "the age grows without bound" instead of "the timestamp stops advancing". |
| **minor: `FreeCADRPC.TIMEOUT = 60` is dead** | **RESOLVED: delete it** (§5.1). |

---

## 13 · Conflicts that genuinely REMAIN — reported, not silently chosen

Each carries a recommendation so Stage 3 is not blocked, and each is marked with what a human
must confirm. **Nine of the twelve are new at this stage**, found by reading the baseline and its
preserved tests rather than the design documents.

### OPEN-1 · The budget table's home collides with a preserved test's stub — **NEW**

Architecture §2.3 and wireframe §1 put the table in **`rpc_server/settings.py`**. But
`tests/test_rpc_handlers.py:56` stubs that module as
`SimpleNamespace(load_settings=…, save_settings=…)`. `from rpc_server.settings import get_budgets`
then raises **ImportError** and the whole preserved module errors out.

**Recommendation (adopted provisionally in §3.2):** a new addon module **`rpc_server/budgets.py`**
holding `DEFAULT_BUDGETS`, `get_budgets()`, `set_gui_budget()`, persisting **through**
`settings.load_settings/save_settings` — which the stub does provide (returning `{}` ⇒ defaults).
Zero test edits, one persistence mechanism, one owner. **Alternative:** keep `settings.py` and
amend one line of the preserved stub, conceding that "preserved" now means "preserved except one
line". **This is a file-location decision only** — "the addon owns one table, persisted" holds
either way. **A human confirms which.**

### OPEN-2 · The wireframe's `socket_for` drops the `max(configured, derived)` envelope — **NEW**

Wireframe §4 literally reads `return b["Q"] + R + M`. The baseline computes
`max(self._timeout, 2·X + margin)` (`freecad_client.py:78,117`), and
`tests/test_client_timeouts.py::test_long_call_socket_budgets_and_cleanup` asserts
`make_proxy.call_args.args[0] >= configured` for rows where the configured timeout **exceeds** the
derived value (`500` and `2000`). A bare `Q + R + M` **fails two rows of a preserved module**.

**Recommendation:** keep `socket_for` pure (so `test_socket_for_reproduces_5dbfe2c` stays exactly
210/150/1230) and apply `max(self._timeout, socket_for(...))` **at the call site** in
`freecad_client.py`. Mechanical, preserves both tests. **Flagged because it contradicts the
authoritative wireframe's pseudocode and Stage 3 must not "fix" it back.**

### OPEN-3 · `connected_clients` may answer a different question than BC-06 asks — **NEW**

`SimpleXMLRPCRequestHandler` inherits `BaseHTTPRequestHandler`'s **HTTP/1.0**, so the server closes
after each response and the client reconnects per call. A count of *currently open* handler
connections is therefore **≈ 0 at idle even with a live bridge**, and BC-06's acceptance
(`connected_clients == 0` after the parent dies) **passes vacuously**. `verify_request` sees only
the accept and never the close.

**Options, not chosen here:** (a) count open connections in the handler's `setup()`/`finish()` and
accept that the number is a *concurrency* gauge, not a *bridge-presence* gauge; (b) add a
**distinct-peer-in-window** counter (peer host+port seen in the last N seconds) so an idle-but-live
bridge is visible; (c) have the client announce itself at connect (a `hello(pid)` RPC) and expire
the registration on EOF — the only option that genuinely measures "exactly one live bridge", and
the only one that adds an RPC method. rxCAD's **G2 halt** condition (count > 1) is only meaningful
under (b) or (c). **A human must pick.** §3.2 builds (a) as the floor.

### OPEN-4 · `execute_code_async` is not immediate when the GUI is busy — **NEW**

`rpc_server.py:326` calls `_set_status(...)` — a **synchronous, health-tracked** `dispatch_to_gui`
at the default 60/60 — **before** starting the worker thread. Against a *busy* (not stuck) GUI the
tool blocks up to `Q = 60 s` before the job even starts, contradicting "returns a `job_id`
immediately" and directly degrading the BC-01/BC-07 entry point this release is built around.
(Against a *stuck* GUI it returns instantly via the rejection, so the defect is invisible in the
stuck case.)

**Recommendation:** move `_set_status` **inside the worker**, or dispatch it with the `probe`-sized
budget and ignore the result. Addon-internal, no signature change. **Not described in any of the
three documents; needs sign-off before Stage 3 changes measured behaviour.**

### OPEN-5 · `partial` jobs are never evicted — **NEW**

`_record_job` (`rpc_server.py:66-71`) keeps 20 **finished** records, where finished is
`state in {"done", "failed"}`. Adding `partial` means partial jobs **accumulate without bound**
for the life of the FreeCAD process.

**Recommendation:** invert the predicate to `state != "running"` so any future state is evicted
correctly — the same open-enum discipline schema rule 7 imposes on consumers. Low risk, but it is
a behaviour change to a preserved code path, so it is reported rather than assumed.

### OPEN-6 · `code` → `code_preview` is a field **rename** in the job record — **NEW**

The baseline writes `code=code_preview` (`rpc_server.py:324`); the BC-01 schema names the field
`code_preview`. Schema rule 6 forbids removals *within v0.2.x*, and 0.1.23 → 0.2.0 is the boundary
— but any consumer reading `job["code"]` breaks silently at the swap.

**Recommendation:** write **both** for v0.2.x (`code_preview` canonical, `code` retained as an
alias) and drop `code` in v0.3.0. Also note the truncation length disagrees: the baseline previews
**200** chars (`rpc_server.py:283`), the schema says **120**. **Pick one** — recommend keeping
**200** (preserved behaviour) and correcting the schema's comment.

### OPEN-7 · `ParamGet` vs the existing JSON settings file — **NEW**

Architecture §2.3 says "persisted through FreeCAD's parameter store"; the wireframe says "persisted
via `ParamGet`". But `settings.py` at `5dbfe2c` persists to **`freecad_mcp_settings.json`** in
`FreeCAD.getUserAppDataDir()` — and that file is live and load-bearing today (it carries
`auto_start_rpc: true`, on which the whole bridge's start-up depends). Introducing `ParamGet` adds
a **second** configuration store for the same subsystem, in two places a human would have to know
to look.

**Recommendation:** persist budgets in the existing JSON settings under a `"budgets"` key — one
store, one file, already backed up and already documented. **A human confirms**; if `ParamGet` is
preferred the change is confined to two functions.

### OPEN-8 · A preserved test becomes vacuous — **NEW**

`tests/test_client_timeouts.py:19` shortens the addon budget with `rpc.EXECUTE_CODE_TIMEOUT = 1`.
Once `execute_code` reads its R from the table, that line is a **no-op**. The test still *passes*
(its sleeps are 0.7 s), but it stops testing what its docstring claims ("with shortened budgets").
Lines 20-21 monkeypatch the client constants with `raising=False`, so **deleting** them is safe.

**Recommendation:** either accept a silently weakened test, or allow a **one-line** edit to a
preserved module (`monkeypatch` the budget table instead). Flagged because "the 12 named modules
are preserved" is otherwise read as "unchanged".

### OPEN-9 · `reset_dispatch_health(force)` still has no specified observable effect

Unchanged from the manifest's CONFLICT-06 and **not addressed by the amendments**. Both branches
require a successful ping; `force=True` is described only as clearing "a flag whose owning task id
is gone", which no document defines in testable terms. `test_force_still_requires_alive` pins the
refusal path and nothing else. **The parameter is on a NEW tool, so there is no compatibility
risk — it needs a definition or removal.** A human decides.

### OPEN-10 · The `probe` budget entry is claimed by two callers — **NEW**

`DEFAULT_BUDGETS["probe"] = {"R": 5, "Q": 5}` is documented as "gui_ping cap; health-untracked".
But the async status-bar clear also dispatches at `timeout=5` (`rpc_server.py:278`) and **is**
health-tracked. Mapping both to one key conflates a tracked call with an untracked one; adding a
sixth key (`status_clear`) changes the table shape that `test_default_table_is_the_baseline`
asserts.

**Recommendation:** `probe` is for `probe_gui` **only**; `_clear_status` keeps its literal `5` and
is documented as deliberately outside the table, since the table's client-facing purpose is socket
derivation and `_clear_status` has no socket.

### OPEN-11 · Harness facts go stale at the swap — **NEW**

rxCAD's project facts pin the installed bridge at **protocol `2025-11-25`, 17 tools**. v0.2.0 is
**`2026-07-28` dual-era, 20 tools**. Additionally the **wireframe contradicts itself**: §2 declares
20 tools and `test_tool_count_is_20`, while its own "Harness consequence" paragraph still says the
registry goes "17 → 19" and that `--check` must assert "19/19" — stale by one, because
`set_gui_budget` was added after that paragraph was written. The manifest inherited the same 19.

**Recommendation:** `tool-registry.md` goes **17 → 20**, `--check` asserts **20/20**, the protocol
fact becomes `2026-07-28`, `gui_ping` is **ALLOW / P0** in `rxc-preflight`'s allowlist (replacing
the six 120 s warm-up attempts), and `reset_dispatch_health` **and** `set_gui_budget` are
**RESTRICTED**, in no default allowlist. **This is a harness edit outside this build tree and it is
not this pipeline's to make.**

### OPEN-12 · `commit_many`'s `timeout_each` default is unspecified

No document states it. §10.2 proposes `None ⇒ the commit table's R (120)`, matching `commit`'s own
default. Cheap to change, recorded so it is a decision rather than an accident.

### OPEN-13 · BC-06's mechanism may not fire on Windows — **NEW**

The design says "stdin EOF is shutdown". Under the SDK's stdio transport, EOF already ends the read
loop, the lifespan closes the proxy, and the process exits — yet **three orphans were measured**.
So either the pipe's write end stayed open (an inherited handle in a sibling process) or a
non-daemon thread held the process up. `SIGTERM` is not delivered the POSIX way on Windows.

**Recommendation:** implement the specified mechanism (explicit EOF shutdown + `SIGTERM`/`SIGBREAK`
handlers + closing the XML-RPC proxy and cancelling in-flight headless children) **first**, then
**measure**. Only if orphans survive that, add a parent-liveness watchdog thread (`ctypes`
`OpenProcess` + `WaitForSingleObject` on the parent handle — the one place native FFI would enter
this server). **Do not build the watchdog speculatively**; it is a second shutdown authority and
its failure mode is killing a live server.

---

## 14 · What Stage 3 must not do

- **Must not** write to `C:\Claude\freecad-mcp` — not a file, not a test, not a `.pyc`.
- **Must not** install the addon anywhere. It is written into the build tree and copied by a human.
- **Must not** dispatch anything to a running FreeCAD. `@live` tests are written and **skipped**
  unless a live GUI is explicitly present, and another session may be driving that GUI.
- **Must not** introduce a third transport, an HTTP entrypoint, a `server_http.py`, or any
  HTTP+SSE dual-endpoint path. The choice is `stdio`, and it is made.
- **Must not** rename, remove, or change the default of **any** of the 17 preserved parameters.
- **Must not** "fix" `obj_properties: dict[str, Any] = None`, give `get_view.view_name` a default,
  or capture stdout process-wide in an async worker.
- **Must not** improvise the probe path, the rejection bypass, or the heartbeat placement — §6
  specifies all three.
- **Must not** resolve anything in §13 by choosing quietly. Those are reported upward.

---

*Stage 2 artifact. Design input to Stages 3–4. It builds nothing, dispatches nothing to FreeCAD,
and writes nothing to `C:\Claude\freecad-mcp`.*
