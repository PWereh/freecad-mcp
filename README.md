# freecad-mcp v0.2.0

A **two-process** MCP bridge to FreeCAD 1.1, rebuilt from `PWereh/freecad-mcp` at `5dbfe2c`
(v0.1.23) against `260916-freecad-mcp-bridge-contract-rev0` (BC-01..BC-08).

```
MCP client ──stdio──▶ freecad-mcp.exe ──XML-RPC / 127.0.0.1:9875──▶ FreeCAD addon (GUI process)
```

**Transport is `stdio`, and only `stdio`.** There is no HTTP entrypoint, no container and no
gateway. The MCP server is a loopback client of a GUI process on the same machine, and BC-06 —
*the client dies with its parent* — is defined in terms of stdin EOF, a signal that exists only
under stdio.

**This tree is not installed anywhere.** It is a build tree beside the working bridge at
`C:\Claude\freecad-mcp`, which is untouched. A human performs the swap, in the order below.

---

## The swap procedure — ADDON FIRST, and never the reverse

The two halves are **deliberately asymmetric in compatibility**:

| combination | result |
|---|---|
| v0.1.23 client + **v0.2.0 addon** | **works.** `execute_code(code, timeout=None)` accepts the old one-argument call, and every new `get_rpc_status` field is additive. |
| **v0.2.0 client** + v0.1.23 addon | **refused at connect, with both versions named.** Deliberate: it would otherwise call `execute_code(code, timeout)` with two arguments and get an opaque XML-RPC `Fault`. |

So there is exactly one safe order:

1. **Install the addon.** Copy `addon\FreeCADMCP\` over
   `%APPDATA%\FreeCAD\v1-1\Mod\FreeCADMCP\`.
   - The user app-data dir is **versioned**: `%APPDATA%\FreeCAD\v1-1\`, **not** `%APPDATA%\FreeCAD\`.
     Installing to the unversioned path fails silently. Confirm with
     `freecadcmd -c "import FreeCAD; print(FreeCAD.getUserAppDataDir())"`.
   - **Delete `rpc_server\__pycache__\`** in the destination when copying.
2. **Restart FreeCAD** (with the GUI — the RPC server lives in `InitGui.py`, so `freecadcmd`
   alone will not serve it). With `auto_start_rpc: true` the bridge binds about 15 s after launch.
3. **Verify, using the still-installed v0.1.23 client:** `get_rpc_status` now reports
   `bridge_contract: "v0.2.0"`, a `budgets` table, `connected_clients` and `jobs_dir`, and every
   existing tool still behaves as before.
4. **Only then swap the client**: point the MCP registration at this tree's
   `freecad-mcp.exe` (see `registration\claude_desktop_config.snippet.json`) and restart the
   MCP client.

Step 4 before step 1 leaves you with a client that refuses every call until you go back.

---

## Install (client half)

```powershell
cd C:\Claude\freecad-mcp-v0.2.0
uv sync --group dev          # creates .venv and installs freecad-mcp plus pytest
```

The console script lands at `.venv\Scripts\freecad-mcp.exe`. `pyproject.toml` is the **only**
dependency manifest; there is no requirements file.

### Run

```powershell
.venv\Scripts\freecad-mcp.exe [--host HOST] [--freecadcmd "PATH"] [--only-text-feedback]
```

Configuration precedence is **CLI flag > environment variable > default**. See `.env.example`
for every variable, including the two that are read **inside the FreeCAD process** rather than
this one. There are no secrets: no tool takes a token, key or licence value, and the only
authentication in the stack is the addon's loopback host filter.

### Test

```powershell
$env:FREECAD_MCP_JOBS_DIR = "C:\Claude\freecad-mcp-v0.2.0\.pytest-jobs"
.venv\Scripts\python.exe -m pytest tests -q
```

`FREECAD_MCP_JOBS_DIR` keeps the job receipts written during the run inside this tree. The
`@live` tests in `tests\test_bridge_contract.py` **dispatch real work to a running FreeCAD GUI**
and skip unless `FREECAD_MCP_LIVE=1`.

---

## What is new in v0.2.0

**Twenty tools**: the 17 preserved ones, with every argument name, type and default unchanged,
plus three.

| tool | what it answers | tier |
|---|---|---|
| `gui_ping(cap=5.0)` | *Is the GUI thread alive?* Dispatches a no-op **through** the GUI thread on a bypass path that no stuck flag can reject. This is the question `get_rpc_status` structurally cannot answer, because it never touches the GUI thread. | ALLOW |
| `reset_dispatch_health(force=False)` | Clears a **stale** stuck flag after proving the thread is free; reports a **real** wedge unchanged. | RESTRICTED — operator only |
| `set_gui_budget(tool, R, Q=None)` | Sets one per-tool GUI budget on the addon, within `[5, 3600]` s, persisted and echoed. | RESTRICTED — operator only |

`execute_code` gains one optional parameter, `timeout`, which may request **less** than the
server's run budget. Requesting more is a structured error naming the cap, never a silent clamp.

**Reading `gui_ping` correctly matters:**

- `alive: true` means *a no-op completed on the GUI thread within `cap`*. If `health.state` is
  `"stuck"` at the same time, the flag is **stale** and `reset_dispatch_health` can clear it.
- `alive: false` with `health.state: "busy"` means the GUI is legitimately busy. **That is not
  the same as dead.**
- Dead is `last_gui_heartbeat_age_s` growing without bound while the RPC thread still answers.

### Budgets: the addon owns one table, the client derives one socket

```
execute_code 90/90   gui_default 60/60   fem 600/600   commit 120/120   probe 5/5      (R/Q, seconds)
```

Every value is the measured `5dbfe2c` behaviour. The client reads the table from
`get_rpc_status()` and derives its socket timeout **per call** as `S = Q + R + M` with `M = 30`,
reproducing the baseline's 210 / 150 / 1230 exactly — one derivation instead of three
`2·X + margin` expressions. There are no budget flags: changing a budget is `set_gui_budget`.

Budgets do not make multi-minute GUI work safe. **They make the failure honest.** Multi-minute
work belongs in `execute_code_async` with `commit_many()`.

### Async jobs now carry a verdict (BC-01 / BC-07)

The payload namespace gains `emit`, `result`, `plan`, `chunk` and `commit_many` beside the
preserved `commit`. `get_async_status` returns the full record, and the same record is written to
`<jobs_dir>\<job_id>.json` on every state change — two independent routes to one verdict, so it
stays readable when the RPC thread is not.

**If you consume that record, read `addon\FreeCADMCP\rpc_server\async_jobs.py` first.** Its
docstring is the schema and its state rules are part of the interface. The one rule that matters
most:

> **Validate completeness by the chunk COUNT, not the state word:**
> `state == "done" and (plan_chunks is None or len(chunks) == plan_chunks)`

`valid_receipt()` ships in that module as executable form of the rule — import it rather than
re-implementing it. A job that returns after 3 of 5 planned chunks is **`partial`**, never
`done`, and `state` is an **open enum**: any state other than `done` is not-done, and an unknown
future state must be *rejected*, not raised on.

### Payload screening (BC-03)

`execute_code`, `execute_code_async` and `execute_code_headless` screen `code` for
XML-1.0-illegal characters **before** it is marshalled, and name the byte, offset, line and
column **in your code** rather than a position in the XML. Nothing is transmitted when the screen
rejects.

### Lifecycle (BC-06)

stdin EOF (the parent is gone) and `SIGTERM`/`SIGINT`/`SIGBREAK` both close the XML-RPC proxy and
exit 0 explicitly, so the server does not outlive its spawner. `get_rpc_status` reports
`connected_clients`. **Read the OPEN-3 note in `rpc_server\connections.py` before acting on that
number**: under HTTP/1.0 it is a concurrency gauge, not a bridge-presence gauge.

---

## Layout

```
freecad-mcp-v0.2.0\
├── src\freecad_mcp\          the MCP server (stdio) - installed via uv/pip
│   ├── server.py             20 tools, payload screen, connect assertion, lifecycle
│   ├── freecad_client.py     XML-RPC client; per-call socket derived from the reported table
│   ├── budgets.py            pure: M = 30, assert_table, socket_for
│   ├── payload_screen.py     BC-03
│   ├── operations\core.py    the *_operation functions the tools delegate to
│   └── headless.py           freecadcmd runner (unchanged)
├── addon\FreeCADMCP\         THE ADDON - copied to %APPDATA%\FreeCAD\v1-1\Mod\ BY A HUMAN
│   └── rpc_server\
│       ├── rpc_server.py     the RPC surface
│       ├── gui_dispatch.py   public dispatch_to_gui over inner _enqueue_and_wait
│       ├── dispatch_health.py stuck_since, heartbeat age, reset_stale()
│       ├── health_probe.py   probe_gui on the BYPASS path
│       ├── async_jobs.py     the job store, THE BC-01 SCHEMA and its validator
│       ├── budgets.py        the per-tool table, persisted through settings.py
│       └── connections.py    connection-lifetime counting
├── tests\                    12 preserved baseline modules + test_bridge_contract.py
├── design\                   the tool manifest and build blueprint this tree implements
└── registration\             the stdio registration snippet
```

## Open items a human must close

The code carries a comment at every site named below. They are **reported, not resolved**: each
is implemented in the conservative, baseline-preserving way, and none of them is claimed as a
property in a docstring or asserted by a passing test.

| id | where | what is undecided |
|---|---|---|
| OPEN-1 | `addon\...\budgets.py` | the budget table lives here, not in `settings.py` as the wireframe says, because a preserved test stubs that module |
| OPEN-3 | `addon\...\connections.py` | `connected_clients` counts open connections, which is ~0 at idle under HTTP/1.0; BC-06's acceptance passes vacuously against it |
| OPEN-4 | `addon\...\rpc_server.py`, `_set_status` | `execute_code_async` can block up to 60 s on a busy GUI before the job starts |
| OPEN-5 | `addon\...\async_jobs.py`, `_FINISHED_STATES` | `partial` jobs are never evicted from the keep-20 history |
| OPEN-6 | `addon\...\rpc_server.py`, `_CODE_PREVIEW_CHARS` | `code` → `code_preview` is a rename (both are written); 200 vs 120 preview length |
| OPEN-7 | `addon\...\budgets.py` | budgets persist in the existing JSON settings file, not `ParamGet` |
| OPEN-8 | `addon\...\rpc_server.py`, `_execute_code_budgets` | whether the per-instance `EXECUTE_CODE_TIMEOUT` override should exist at all |
| OPEN-9 | `addon\...\health_probe.py`, `reset_dispatch_health` | `force` has no observable effect distinct from `force=False` |
| OPEN-10 | `addon\...\rpc_server.py`, `_clear_status` | the literal 5 s status-bar clear is deliberately outside the budget table |
| OPEN-12 | `addon\...\async_jobs.py`, `commit_many` | `timeout_each=None` is taken to mean the `commit` table entry (120) |
| OPEN-13 | `src\freecad_mcp\server.py`, `_install_signal_handlers` | whether stdin EOF actually fires on Windows — **measure before adding a watchdog** |

`OPEN-2` and `OPEN-11` are resolved in code as the blueprint directs: `socket_for` stays pure
with the envelope at the call site (`src\freecad_mcp\budgets.py`), and the tool count is **20**,
not the wireframe's stale 19.

---

MIT, as the upstream fork. Derived from `neka-nat/freecad-mcp` via `PWereh/freecad-mcp`.
