# MCP Tool Manifest — freecad-mcp v0.2.0

**Server slug** `freecad-mcp`
**Release** v0.2.0 (rebuild of fork `PWereh/freecad-mcp` at `5dbfe2c`, v0.1.23)
**Stage** 1 of 4 — spec-analyst. This manifest specifies; it builds nothing.
**Build root (this run)** `C:\Claude\freecad-mcp-v0.2.0\`

## Binding inputs

This manifest is **derived from, not independent of**, three documents. Where it appears to
differ from them, they win and this file is wrong:

1. `260916-freecad-mcp-v0.2.0-architecture-rev0.md` — authoritative design
2. `260916-freecad-mcp-v0.2.0-wireframe-rev0.md` — authoritative module/tool/schema layout
3. `260916-freecad-mcp-bridge-contract-rev0.md` — the requirements contract, BC-01..BC-08

Precedence, as declared by the wireframe's own status line: **architecture > wireframe >
baseline at `5dbfe2c`**. The contract states the requirements; where the architecture supersedes
a contract *mechanism*, §9 of this manifest records it as a conflict rather than resolving it
silently. Everything in §9 is reported upward, unresolved, for a human or Stage 2.

## The constraint that does not move

`mcp-builder` scaffolds into a **new** tree. **Nothing in this pipeline writes to
`C:\Claude\freecad-mcp`** — that is the installed, working bridge and the only thing currently
able to drive FreeCAD. The swap to the installed location is a **human decision after BC-08 is
green**. Architecture line 11; binding on every stage.

## Deployment target — recorded, not chosen

| requested | transport implied | note |
|---|---|---|
| **LOCAL** | **stdio** | the only target requested for this run |
| CONTAINERIZED | Streamable HTTP | **not requested**; see below |

Target revision for the two live transports: **2026-07-28**. No third transport exists and none
is introduced here. Stage 2 (transport-architect) makes the selection; this stage only records
the request.

CONTAINERIZED is not requested and is not viable for this server: `freecad-mcp` is a **local
XML-RPC client of a GUI process on `127.0.0.1:9875`**. The MCP transport (stdio, MCP client ↔
`freecad-mcp.exe`) and the downstream link (XML-RPC loopback, `freecad-mcp.exe` ↔ FreeCAD addon)
are distinct; only the first is the MCP transport, and only it is in scope for Stage 2.

**Runtime** Python. Server venv 3.12; addon runs inside FreeCAD's bundled Python 3.11.

---

## 1 · Tool manifest — 19 tools

`ctx: Context` is FastMCP's injected context parameter on every baseline tool. It is **not** an
input-schema property and must not appear in any generated JSON Schema; it is omitted from the
params column and preserved in every Python signature.

All 17 baseline rows reproduce the **exact** `5dbfe2c` signature, read from
`src/freecad_mcp/server.py`. Argument names, order, types and defaults are the compatibility
surface: rxCAD is a live consumer and keys its probes, hook and allowlists on them.

| # | tool_name | purpose | schema params (`name:type`) |
|--:|---|---|---|
| 1 | `create_document` | Create a new FreeCAD document. | `name:string` **required** |
| 2 | `create_object` | Create an object (`Part::` / `Draft::` / `PartDesign::` / `Fem::`) in a document. | `doc_name:string` **required**; `obj_type:string` **required**; `obj_name:string` **required**; `analysis_name:string\|null` optional (default `null`); `obj_properties:object` optional (default `null` — see N-3); `include_screenshot:boolean` optional (default `true`); `view_name:ViewName` optional (default `"Isometric"`) |
| 3 | `edit_object` | Edit an existing object's properties when `create_object` cannot express the change. | `doc_name:string` **required**; `obj_name:string` **required**; `obj_properties:object` **required**; `include_screenshot:boolean` optional (default `true`); `view_name:ViewName` optional (default `"Isometric"`) |
| 4 | `delete_object` | Delete an object from a document. | `doc_name:string` **required**; `obj_name:string` **required**; `include_screenshot:boolean` optional (default `true`); `view_name:ViewName` optional (default `"Isometric"`) |
| 5 | `execute_code` | Execute Python on FreeCAD's **GUI thread** and wait for the result. The safe default for all document automation. | `code:string` **required**; `include_screenshot:boolean` optional (default `true`); `view_name:ViewName` optional (default `"Isometric"`); **`timeout:number` optional (default `null`) — NEW in v0.2.0**, per-call run budget, **capped at the server's R** (N-6) |
| 6 | `execute_code_async` | Start background-safe Python in a worker thread; returns a `job_id` immediately. No direct document or view writes — all writes go through injected `commit()`. | `code:string` **required** |
| 7 | `execute_code_headless` | Run a script in a separate `freecadcmd` process. Crash-isolated from the GUI session. | `code:string` **required**; `timeout:number` optional (default `600`) |
| 8 | `get_async_status` | Report background jobs started by `execute_code_async`. Answered **off** the GUI thread. | `job_id:string` optional (default `""` — empty lists all running jobs plus up to 20 finished) |
| 9 | `get_object` | Get one object's serialised properties. | `doc_name:string` **required**; `obj_name:string` **required**; `include_screenshot:boolean` optional (default `true`); `view_name:ViewName` optional (default `"Isometric"`) |
| 10 | `get_objects` | Get all objects in a document. | `doc_name:string` **required**; `include_screenshot:boolean` optional (default `true`); `view_name:ViewName` optional (default `"Isometric"`) |
| 11 | `get_parts_list` | List parts available in the parts-library addon. | *(none)* |
| 12 | `get_rpc_status` | Report RPC and GUI-dispatch health **without touching the GUI thread**. Remains available after a GUI timeout. | *(none)* |
| 13 | `get_view` | Screenshot of the active 3D view. | `view_name:ViewName` **required** (no default here — see N-2); `width:integer\|null` optional (default `null`); `height:integer\|null` optional (default `null`); `focus_object:string\|null` optional (default `null`) |
| 14 | `insert_part_from_library` | Insert a part from the parts-library addon. | `relative_path:string` **required**; `include_screenshot:boolean` optional (default `true`); `view_name:ViewName` optional (default `"Isometric"`) |
| 15 | `list_documents` | List open documents. The cheapest *real* GUI round trip. | *(none)* |
| 16 | `reload_document` | Close and re-open a document to pick up on-disk changes made outside the GUI. | `doc_name:string` **required** |
| 17 | `run_fem_analysis` | Run CalculiX on an existing `Fem::AnalysisPython` container; return stress/displacement summary. | `doc_name:string` **required**; `analysis_name:string` **required**; `timeout:integer` optional (default `600`); `include_screenshot:boolean` optional (default `true`); `view_name:ViewName` optional (default `"Isometric"`) |
| **18** | **`gui_ping`** — NEW | **Liveness probe.** Dispatches a no-op *through the GUI thread* under its own short budget and reports whether the thread answered. The question `get_rpc_status` structurally cannot answer. | `cap:number` optional (default `5.0`) — seconds, used as **both** `timeout` and `queue_timeout` |
| **19** | **`reset_dispatch_health`** — NEW | **Bounded recovery.** Clears a *stale* `stuck` flag when `gui_ping` proves the thread free; reports and changes nothing when the thread is genuinely wedged. | `force:boolean` optional (default `false`) — see N-8 and CONFLICT-06 |

**Tool count: 19** (17 preserved + 2 added). BC-08 asserts exactly this
(`test_tool_count_is_19`); rxCAD's `tool-registry.md --check` must assert 19/19.

### 1.1 Routing and tiering

`GUI` = via `dispatch_to_gui`; `off` = answered without the GUI thread; `worker` = async thread;
`proc` = separate `freecadcmd`. Tier is rxCAD's harness classification, carried per the
wireframe §2.

| # | tool | client method (`freecad_client.py`) | addon RPC | thread | tier | v0.2.0 change |
|--:|---|---|---|---|---|---|
| 1 | `create_document` | `create_document` | `create_document` | GUI | — | — |
| 2 | `create_object` | `create_object` | `create_object` | GUI | — | — |
| 3 | `edit_object` | `edit_object` | `edit_object` | GUI | — | — |
| 4 | `delete_object` | `delete_object` | `delete_object` | GUI | — | — |
| 5 | `execute_code` | `execute_code` | `execute_code` | GUI | — | **screen**; optional capped `timeout` |
| 6 | `execute_code_async` | `execute_code_async` | `execute_code_async` | worker | — | **screen**; namespace gains `emit/result/plan/chunk/commit_many` |
| 7 | `execute_code_headless` | — (spawns) | — | proc | — | **screen** |
| 8 | `get_async_status` | `get_async_status` | `get_async_status` | off | — | returns the **§3 job-result schema** |
| 9 | `get_object` | `get_object` | `get_object` | GUI | — | — |
| 10 | `get_objects` | `get_objects` | `get_objects` | GUI | — | — |
| 11 | `get_parts_list` | `get_parts_list` | `get_parts_list` | off | — | — |
| 12 | `get_rpc_status` | `get_rpc_status` | `get_rpc_status` | off | — | gains `budgets{R,Q}`, `connected_clients`, `bridge_contract`, heartbeat age |
| 13 | `get_view` | **`get_active_screenshot`** | `get_active_screenshot` | GUI | — | — (MCP name ≠ client/RPC name; preserved, N-1) |
| 14 | `insert_part_from_library` | `insert_part_from_library` | `insert_part_from_library` | GUI | **DENY** | — (kept for signature only) |
| 15 | `list_documents` | `list_documents` | `list_documents` | GUI | — | — |
| 16 | `reload_document` | `reload_document` | `reload_document` | GUI | — | — |
| 17 | `run_fem_analysis` | `run_fem_analysis` | `run_fem_analysis` | GUI | UNPROVEN | — (FEM explicitly out of contract scope) |
| **18** | **`gui_ping`** | `gui_ping` | `gui_ping` | **GUI, capped** | **ALLOW / P0** | new; in `rxc-preflight`'s allowlist, replaces the 120 s warm-up loop |
| **19** | **`reset_dispatch_health`** | `reset_dispatch_health` | `reset_dispatch_health` | off → pings GUI | **RESTRICTED** | new; human-invoked only, in **no** default allowlist |

### 1.2 Notes on types, enums and defaults

- **N-1 · `ViewName` enum.** A closed string enum, identical everywhere it appears:
  `"Isometric" | "Front" | "Top" | "Right" | "Back" | "Left" | "Bottom" | "Dimetric" |
  "Trimetric"`. Declared once in `server.py` as a `Literal`; the JSON Schema must render it as
  `{"type": "string", "enum": [...]}`.
- **N-2 · `get_view.view_name` is required.** Alone among the tools, it carries no default. This
  is a baseline asymmetry, not an oversight to fix: giving it a default changes the generated
  schema's `required` array and therefore breaks the preserved signature.
- **N-3 · `create_object.obj_properties` quirk.** The baseline annotation is
  `obj_properties: dict[str, Any] = None` — a non-`Optional` annotation with a `None` default.
  **Preserve it verbatim.** "Correcting" it to `dict[str, Any] | None = None` changes the emitted
  schema (`{"type":"object"}` → an `anyOf`) and is a signature change under BC-08.
- **N-4 · Return encoding.** Every baseline tool is declared `@mcp.tool(structured_output=False)`
  and returns `list[TextContent]` or `list[TextContent | ImageContent]`. The two new tools follow
  the same convention: `list[TextContent]` carrying their JSON body as text. Neither the
  architecture nor the wireframe states the MCP-level encoding for `gui_ping` /
  `reset_dispatch_health` — the wireframe's own precedence rule ("where either is silent, the
  baseline wins") supplies it. Logged as a documented gap, GAP-A in §9.
- **N-5 · Screenshot suppression.** `include_screenshot` interacts with the server-wide
  `--only-text-feedback` flag; the flag wins. Preserved.
- **N-6 · `execute_code.timeout` cap.** Requesting more than the server's configured **R** is a
  **structured error naming the cap**, never a silent clamp (wireframe §4). Type `number`
  (seconds). `null`/absent means "use R".
- **N-7 · `gui_ping.cap`.** Passed as *both* `timeout` and `queue_timeout` to `dispatch_to_gui`,
  so the probe's total wall cost is bounded by `cap`, not `2 × cap`. `alive` means **"a no-op
  completed on the GUI thread within `cap`"** — a legitimately *busy* GUI therefore returns
  `alive: false` with `health.state: "busy"`, which is not the same as dead. Dead is
  distinguished by `last_gui_heartbeat_age_s` growing without bound. See CONFLICT-05.
- **N-8 · `reset_dispatch_health.force`.** Both `force=false` and `force=true` require a
  successful `gui_ping` first; `force` exists only to clear a flag whose owning task id is gone.
  Its observable difference from `force=false` is not specified by either document — CONFLICT-06.
- **N-9 · No credentials anywhere.** No tool takes a token, key, licence value or install-specific
  path. The addon binds loopback-only (`ip_filter.py`, default `127.0.0.1`); the sole
  authentication is host filtering. Nothing in this manifest is a secret.
- **N-10 · Units.** FreeCAD is millimetre-native. Every numeric geometry value crossing these
  tools is mm. Budget/timeout params are **seconds**.

---

## 2 · BC-02 — the budget invariant (one knob, derived, asserted twice)

Not five knobs. **One knob, four derived values, one inequality, asserted at two moments.** Two
independently settable numbers would let the pair drift back out of order on a later edit —
which is the exact cascade this rebuild exists to remove.

| symbol | meaning | derivation | default |
|---|---|---|---|
| **R** | GUI **run** budget — the one that marks `stuck` | the knob | **60** |
| **Q** | GUI **queue** budget — cancels, does **not** mark stuck | `= R` | 60 |
| **M** | processing margin | constant | 30 |
| **C** | client tool wait | `= Q + R + M` | 150 |
| **S** | client socket timeout | `= C + 30` | 180 |

> **The invariant: `Q + R + M < C ≤ S`.**

- `Q + R + M < C` — the addon's **structured** error (`stuck_failure`, or the queue give-up) must
  reach the client before the client stops listening. Smaller C: the client sees a bare socket
  timeout, learns nothing, and the addon believes it delivered.
- `C ≤ S` — the tool wait cannot exceed the transport's, or the transport aborts the tool
  mid-wait.

**Surface.** `budgets.py` exposes `derive(R=60.0, Q=None, M=30.0) -> Budgets` (frozen dataclass)
and `assert_nested(b) -> None`, which **raises** unless the inequality holds.

**Flags — exactly two, nothing else is a flag:** `--gui-budget R`, optional `--queue-budget Q`
(cold-start tuning; overriding Q re-derives C and S). The literal `EXECUTE_CODE_TIMEOUT = 90`
(`freecad_client.py:28`) is **deleted**; `execute_code`'s socket timeout becomes `S`. See
CONFLICT-01 and CONFLICT-02 before implementing either.

**Assertion 1 — at server start.** The derivation is computed, logged, and startup is **refused**
if any inequality fails.

**Assertion 2 — at connect.** The addon reports its own **R** and **Q** in `get_rpc_status`
(`budgets{R,Q}`). The client compares them against its local `Budgets` and **refuses to serve
tools on mismatch, naming which side is stale.** A stale addon under a new server — or the
reverse — is caught before the first payload. This is BC-08's canary and what turns "an upstream
reinstall silently re-breaks tools" into a named halt.

Budgets are **not** a solution for multi-minute GUI work. They make the failure honest.
Multi-minute work goes through §4 (chunking).

---

## 3 · BC-01 — the async job-result schema (first-class shared interface)

Returned by `get_async_status(job_id)`; also written to
`%LOCALAPPDATA%\freecad-mcp\jobs\<job_id>.json` on every state change, by atomic rename (SHOULD).
Two independent routes to the same record is rxCAD's standard for a receipt: when the RPC thread
is unreachable, the verdict is still readable from disk.

**This schema is a shared interface, not an implementation detail.** A second harness (rxCAD
pass 2, checks A1/A2) consumes it as a receipt. Its state rules are part of the interface.

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

Field types: `id:string`; `state:string` (closed enum, four values);
`started:number|null`, `finished:number|null` (epoch seconds);
`code_preview:string`; `plan_chunks:integer|null`;
`chunks:array<object>`; `result:any|null`; `output:string`;
`output_truncated:boolean`; `error:string|null`; `traceback:string|null`.

### 3.1 State rules — the consumer contract (carried into the schema's own documentation)

1. `done` **requires** `len(chunks) == plan_chunks` **and** every chunk `ok`. The **store**
   enforces this; a worker cannot mark itself `done` otherwise.
2. A worker that returns before reporting `plan_chunks` chunks is **`partial`**, not `done`, even
   if it raised nothing.
3. A worker that raises is `failed`; chunks already recorded are **kept**.
4. Single-shot jobs (`plan()` never called) have `plan_chunks: null` and `chunks: []`;
   completeness is `state == "done"`.
5. **A consumer validates completeness by the COUNT, not the WORD:**

   > `valid_receipt := (state == "done") and (len(chunks) == plan_chunks)`

   Checking the state word alone is the bug this design exists to remove; the count is the
   cross-check that stops a five-minute payload from presenting a partial verdict as a whole one.
   **This rule ships in the schema's documentation and as a reference validator beside it**, not
   only in prose. `TestBC07.test_consumer_rule_count_not_word` exercises the validator directly.
6. Fields are **never removed** within v0.2.x; additions are allowed. rxCAD asserts the field set
   at connect via `bridge_contract`.

### 3.2 Injected async namespace

Beside the preserved `commit`, injected into the `execute_code_async` worker namespace:

| helper | signature | behaviour |
|---|---|---|
| `commit` | `commit(fn, timeout=120) -> Any` | **preserved.** The only write path from a worker onto the GUI thread. See CONFLICT-09 on the default. |
| `commit_many` | `commit_many(fns, timeout_each) -> list` | BC-07 convenience: each `fn` in its own budget; **stops at the first failure** and returns results so far. |
| `emit` | `emit(text) -> None` | appends to a **per-job, thread-local** buffer. No process-wide redirect, so no race with the GUI thread — the baseline's reason for not capturing stdout stands and is preserved. Capped (default 1 MiB); overflow sets `output_truncated: true`. |
| `result` | `result(obj) -> None` | stores a JSON-serialisable value. If never called, the payload's own return value is stored as `result`. |
| `plan` | `plan(n_chunks) -> None` | declares how many chunks the job will report; recorded as `plan_chunks`. |
| `chunk` | `chunk(name, obj) -> None` | appends `{name, state, started, finished, result\|error}` to `chunks[]`. |

`execute_code_async`'s **MCP signature is unchanged** — these helpers live in the payload's
namespace, not in the tool's input schema.

---

## 4 · BC-03..BC-07 — the rest of the behavioural surface

### BC-03 · Payload screen (client-side, before marshalling)

`payload_screen.screen(code) -> None | dict`, called by `server.py` at the top of
**`execute_code`, `execute_code_async` and `execute_code_headless`**. Nothing is transmitted when
it returns a dict.

- Illegal set: `< 0x20` except `TAB/LF/CR`, plus `0x7F`, `0x80–0x9F`, `U+2028`, `U+2029`.
- Walks the **raw string**. Never `splitlines()` — it consumes exactly the characters sought.
  `TestBC03.test_screen_never_uses_splitlines` is a static assertion on the module source.
- Error names the **byte, offset, line and column in the code** — not an XML position:
  `{"success": false, "error": "illegal character 0x0b at line 2 col 6 (offset 11)"}`.
- **No addon-side screen is built.** A payload that reached the addon has already parsed; the
  screen there would be dead code.

### BC-04/BC-05 · Health — three states, a probe, a bounded reset

| state | meaning | GUI calls |
|---|---|---|
| `healthy` | no active task | served |
| `busy` | task running within R | queued (Q applies) |
| `stuck` | task past R, still running | rejected immediately, **until `finish()` or a successful reset** |

- Health snapshot gains `stuck_since`, `last_gui_heartbeat_age_s`, and the addon's `R`, `Q`.
- **Heartbeat exposure.** The GUI already ticks every 500 ms (`_waker` / `QTimer.singleShot(500,
  process_gui_tasks)`). Record `monotonic()` on each tick. A heartbeat that stops advancing while
  the RPC thread still answers is the signature of a **real** wedge, distinct from a flagged one.
  This is the only mechanism that separates *stuck-flag* from *wedged-thread*.
- `gui_ping` returns `{alive:boolean, latency_s:number, health:object,
  last_gui_heartbeat_age_s:number}`. It **must be a real GUI dispatch** — the wireframe's
  `test_ping_is_a_gui_dispatch` fails any implementation that reads the snapshot alone, because
  that is what `get_rpc_status` already does and why it read `healthy` while a real dispatch hung
  45 s.
- `reset_dispatch_health` returns `{reset:boolean}` on success, or
  `{reset:false, reason:"GUI thread not answering", stuck_since:number,
  heartbeat_age:number}` and **changes nothing** on a real wedge. A stale flag is recoverable; a
  real wedge is reported honestly, never papered over.
- Nothing here interrupts a running GUI task. That is impossible from outside the GUI thread and
  this design does not pretend otherwise.

> **BLOCKER — see CONFLICT-04.** As specified, `reset_dispatch_health` calls `gui_ping`, and
> `gui_ping` goes through `dispatch_to_gui`, whose **first action is
> `_dispatch_health.rejection()`** (`gui_dispatch.py:208-210`). While `stuck`, the probe is
> rejected before it ever reaches the GUI thread. BC-05 is unimplementable without an explicit
> rejection bypass on the probe path. Reported, not resolved.

### BC-06 · The client dies with its parent

- The stdio server's read loop treats **stdin EOF as shutdown**: close the XML-RPC proxy, cancel
  in-flight headless children, **exit 0**. `SIGTERM` does the same.
- The addon counts live handler connections and logs connect/disconnect **with the peer port**
  (`connections.py`, extending what `ip_filter.py` already inspects).
- `get_rpc_status` gains `connected_clients:integer`. A count above 1 is rxCAD's **G2 halt**
  condition — now measurable instead of inferred from `netstat`.
- Measured at `5dbfe2c`: three orphaned `freecad-mcp.exe` after three runs, each holding or
  half-closing a socket on 9875.

### BC-07 · Chunked commit

A five-minute TechDraw build is a **sequence** of GUI steps — create view, pump `updateGui()`
until `getVisibleEdges() > 0`, next view — **none of which may run for R inside one `commit()`**.
So the job is a worker calling `commit()` repeatedly, each call inside its own budget, with
health returning to `healthy` between calls. `commit_many(fns, timeout_each)` is the convenience.

The interaction with BC-01 is the whole point: **a chunked job that dies at chunk 3 of 5 has a
result file that looks complete unless completeness is part of the schema.** Hence `plan_chunks`,
`chunks[]`, and `partial` as a first-class state — §3.1.

### BC-08 · Regression canaries

`get_rpc_status` reports `bridge_contract: "v0.2.0"` so rxCAD's preflight can assert it and halt
against a v0.1.23 addon **with the version named**.

---

## 5 · `tests/test_bridge_contract.py` — one class per requirement, a failing case per test

Tests needing a live FreeCAD are marked `@live` and skip without one. **The forge writes them
regardless.**

| class | test | failing case it pins |
|---|---|---|
| `TestBC01_ResultChannel` | `test_emit_and_result_round_trip` | **Fails on `5dbfe2c`**: no `output`, no `result` field exists |
| | `test_failed_job_carries_traceback` | job raises → `state=="failed"`, `traceback` non-empty |
| | `test_output_cap_sets_truncated` | 10 MiB emit → `output_truncated is True`, `len(output) <= cap` |
| | `test_return_value_stored_when_result_not_called` | payload return value lands in `result` |
| `TestBC02_Budgets` | `test_derive_defaults_nest` | `derive()` → R 60, Q 60, C 150, S 180; `assert_nested` passes |
| | `test_assert_nested_rejects_inversion` | `Budgets(R=60,Q=60,M=30,C=100,S=90)` **raises** — *the current hazard: independently set numbers that do not nest* |
| | `test_execute_code_timeout_capped` | `timeout=900` under R=300 → structured error **naming 300** |
| | `@live test_connect_assertion_refuses_stale_addon` | addon R=60 vs client R=300 → tools refused, **both values named** |
| `TestBC03_PayloadScreen` | `test_control_byte_reported_with_position` | `"x = 1\n# bad\x0bchar"` → names `0x0b`, line 2, col 6. **Fails on `5dbfe2c`**: `ExpatError` naming an XML position |
| | `test_tab_lf_cr_allowed` | TAB/LF/CR pass |
| | `test_u2028_rejected` | `U+2028` rejected |
| | `test_screen_never_uses_splitlines` | static: module source contains no `splitlines(` |
| `TestBC04_GuiPing` | `@live test_ping_alive_when_idle` | `alive is True`, latency under cap |
| | `@live test_ping_reports_busy_within_cap` | during a 120 s task, returns within 5 s with `health.state == "busy"` |
| | `test_ping_is_a_gui_dispatch` | static/mocked: calls `dispatch_to_gui`. **Fails** if implemented like `get_rpc_status` |
| `TestBC05_Reset` | `@live test_reset_clears_stale_flag` | 70 s task under R=60 → stuck; after finish, `reset` → `{reset:true}`; next `execute_code` succeeds |
| | `test_reset_refuses_when_ping_fails` | mocked dead thread → `{reset:false, reason:...}`, **flag unchanged**. **Fails** if reset clears unconditionally |
| | `test_force_still_requires_alive` | `force=True` on a dead thread still refuses |
| `TestBC06_Lifecycle` | `test_stdin_eof_exits_zero_and_closes_proxy` | EOF → exit 0 within 5 s; proxy `close()` called |
| | `test_sigterm_exits` | SIGTERM → clean exit |
| | `@live test_connected_clients_count` | one client → 1; after exit → 0 within 5 s. **Fails on `5dbfe2c`**: no such field, and the orphan lives on |
| `TestBC07_Chunking` | `@live test_commit_many_stays_healthy_between_chunks` | 10 × `commit(sleep 20)` under R=60 completes; `healthy` between chunks |
| | `@live test_single_long_commit_is_stuck` | one `commit(sleep 70)` under R=60 → stuck. Proves the budget is **per call** |
| | `test_partial_when_chunks_short_of_plan` | `plan(5)`, three chunks, return → `state=="partial"`, `len(chunks)==3`. **The receipt rxCAD A2 must never accept as whole** |
| | `test_done_requires_all_chunks_ok` | `plan(2)`, one ok one failed → **not** `done` |
| | `test_consumer_rule_count_not_word` | `{state:"done"}` with `len(chunks) != plan_chunks` → **rejected by the reference validator shipped with the schema** |
| `TestBC08_Canaries` | `test_status_reports_bridge_contract_version` | `get_rpc_status().bridge_contract == "v0.2.0"` |
| | `test_all_17_baseline_signatures_unchanged` | introspect the 17 preserved tools against a pinned table. **Fails on any rename or default change** — see CONFLICT-07 |
| | `test_tool_count_is_19` | the manifest above, asserted |
| | `test_existing_suite_still_green` | the baseline test modules import and pass — see CONFLICT-08 on their count |

---

## 6 · File-by-file — preserved vs re-authored

`▸` new · `Δ` re-authored · unmarked = **copied unchanged from `5dbfe2c`**. Mirrors the fork so a
reviewer can diff file-for-file. Rows marked **[+]** are baseline files the wireframe's layout
omits; they are preserved by the wireframe's own "silent → baseline wins" rule and are listed
here so the forge does not scaffold an unimportable tree (GAP-B).

```
C:\Claude\freecad-mcp-v0.2.0\
├── src/freecad_mcp/
│   ├── __init__.py                      [+]
│   ├── server.py              Δ   19 MCP tools; payload screen before every execute_code*
│   ├── freecad_client.py      Δ   budgets from budgets.py, not constants; connect assertion;
│   │                              gains gui_ping / reset_dispatch_health methods
│   ├── budgets.py             ▸   one knob R -> Q, M, C, S; the invariant; startup assertion
│   ├── payload_screen.py      ▸   raw-string XML-1.0 screen with offset/line/col
│   ├── headless.py                freecadcmd runner, 600 s (screen applied by server.py)
│   ├── server_state.py        Δ   holds the derived budget set and the addon's reported R/Q
│   ├── operations/            Δ   [+] server.py imports 17 *_operation fns from here; two new
│   │   ├── __init__.py            tools require two new operations. NOT optional.
│   │   └── core.py
│   ├── responses.py               unchanged
│   └── prompt_text.py             unchanged
├── addon/FreeCADMCP/
│   ├── Init.py, InitGui.py        unchanged
│   └── rpc_server/
│       ├── __init__.py            [+]
│       ├── rpc_server.py      Δ   RPC surface; job store and health extracted to the modules
│       │                          below; execute_code gains optional capped timeout
│       ├── gui_dispatch.py    Δ   mechanism preserved (FIFO, per-call response queue, Qt wake,
│       │                          stuck marking at R); budgets injected not defaulted inline;
│       │                          heartbeat timestamp on each tick; probe rejection bypass
│       ├── dispatch_health.py Δ   snapshot gains stuck_since, last_gui_heartbeat_age_s, R, Q
│       ├── health_probe.py    ▸   gui_ping(cap), reset_dispatch_health(force)
│       ├── async_jobs.py      ▸   _ASYNC_JOBS + lock + keep-20 (moved); emit/result/plan/chunk;
│       │                          commit_many; state machine; optional file persistence
│       ├── connections.py     ▸   connect/disconnect tracking (extends what ip_filter sees)
│       ├── ip_filter.py           unchanged
│       ├── fem_executor.py        unchanged — FEM out of scope
│       ├── object_validation.py   unchanged
│       ├── commands.py            [+] unchanged
│       ├── object_factory.py      [+] unchanged
│       ├── parts_library.py       [+] unchanged
│       ├── property_mapper.py     [+] unchanged
│       ├── serialize.py           [+] unchanged
│       ├── settings.py            [+] unchanged
│       └── view_manager.py        [+] unchanged
└── tests/
    ├── (all existing baseline test modules)  preserved, must still pass
    └── test_bridge_contract.py   ▸  BC-01..BC-08 (§5)
```

### 6.1 Preserved vs re-authored, by concern

| concern | preserved from `5dbfe2c` | re-authored / added in v0.2.0 |
|---|---|---|
| transport | XML-RPC loopback 9875; stdio MCP; `--only-text-feedback`; quoted `--freecadcmd`; `--host` | — |
| GUI boundary | `dispatch_to_gui` **mechanism**: FIFO `_rpc_request_queue`, per-call response queue, Qt `_waker`, mouse/popup/modal guards, re-entrancy guard, stuck marking at R | budget **derivation**; startup + connect assertions; heartbeat timestamp; probe rejection bypass |
| health | three states, `rejection()`, `stuck_failure()`, off-GUI `get_rpc_status` | `gui_ping`, heartbeat exposure, `reset_dispatch_health`, `stuck_since` |
| async | worker threads, `_ASYNC_JOBS` + lock + keep-20, `commit()`, **no process-wide stdout redirect** | `emit/result/plan/chunk`, `commit_many`, `partial` state, the §3 schema, file persistence |
| `execute_code` | GUI-thread execution, stdout capture via `redirect_stdout`, persistent `_EXEC_NAMESPACE` | optional capped `timeout`; payload screen |
| headless | separate `freecadcmd`, 600 s, independent failure | payload screen |
| FEM | `run_fem_analysis`, `fem_executor.py` | **untouched** — FEM is UNPROVEN in the harness and out of this contract |
| tool signatures | **all 17 preserved exactly** | 2 added (`gui_ping`, `reset_dispatch_health`) |
| lifecycle | — | stdin-EOF / SIGTERM shutdown; connection counting |

### 6.2 What must not change (contract §"What must not change", restated as a build rule)

- `execute_code` stays on the GUI thread and stays the safe default.
- `execute_code_async` stays off it and **still forbids direct document writes** — the
  docstring's warning that such writes *"can wedge FreeCAD's event loop"* describes the very
  wedge this contract exists to remove, and it stays the rule.
- The **per-call response queue** in `dispatch_to_gui` — a timeout in one call must never corrupt
  another call's response.
- `get_rpc_status` remains answered **off** the GUI thread. That is a feature; `gui_ping` exists
  so nobody misreads it.
- `--only-text-feedback` and the quoted `--freecadcmd` path.
- The default 90 s budget (contract) — **but see CONFLICT-02, which this collides with.**

---

## 7 · Context the forge must not design around

Proven on 16 September 2026, run B4b: **`exportPageAsPdf` is not at fault.** It returned in 4.4 s
with the GUI alive before and after. The wedge was the **bridge cascade**: a payload passing R
marked `DispatchHealth` `stuck`, and every later GUI call was rejected until it returned. There
is no export defect. Do not build a workaround for one.

---

## 8 · Definition of done (carried from the wireframe §7, for Stage 4)

All of §5 green in the new tree; the baseline test modules green; the rxCAD rev E payload
completes through `execute_code_async` + `commit_many` with its verdict read from
`get_async_status` **and** matching its own result file; no orphan `freecad-mcp.exe` after the
run. **Then, and only then, a human swaps the installed bridge.**

---

## 9 · Conflicts and gaps — reported, NOT resolved

Every item below is a place where the three binding documents disagree with each other or with
the measured baseline. This stage reports; it does not choose.

### CONFLICT-01 · Budget model: contract flags vs architecture's one knob — **architecture wins, but the contract's wording is now dead**

- **Contract BC-02** requires two flags, `--execute-timeout` (default **90**, "unchanged") and
  `--queue-timeout`, and says *"the addon's `dispatch_to_gui` defaults read from a preference the
  server sets at connect"* — i.e. the **server pushes** budgets to the addon.
- **Architecture §2.3 / wireframe §4** replace both with `--gui-budget R` (default **60**) plus
  optional `--queue-budget Q`, delete `EXECUTE_CODE_TIMEOUT = 90`, and replace the push with a
  connect-time **assertion** (addon reports R/Q; client refuses on mismatch; nothing is pushed).
- Per precedence the architecture wins. Two consequences must be accepted explicitly:
  1. **No flag named `--execute-timeout` will exist.** The contract's BC-02 acceptance text
     (`--execute-timeout 300`) has to be read as `--gui-budget 300`.
  2. **Nothing in v0.2.0 configures the addon's budgets.** They are only *compared*. **Who sets
     the addon's R and Q, and by what mechanism (`settings.py`? a preference? a constant?), is
     specified by neither document.** This is a hole in the middle of BC-02 — GAP-C.

### CONFLICT-02 · The architecture's budget table misreads the baseline, and one-knob R=60 silently regresses `execute_code` — **highest-impact item in this manifest**

Measured at `5dbfe2c`:

- `addon/.../rpc_server.py:120` — `FreeCADRPC.EXECUTE_CODE_TIMEOUT = 90`
- `addon/.../rpc_server.py:364` — `execute_code` dispatches with `timeout=self.EXECUTE_CODE_TIMEOUT`

So **`execute_code`'s addon-side run budget R is 90, not 60.** The 60 in
`gui_dispatch.py:183` is the *signature default*, used by every **other** GUI tool
(`create_document`, `create_object`, `edit_object`, `delete_object`, `get_object`, `get_objects`,
`list_documents`, `reload_document`, `insert_part_from_library`, `get_active_screenshot`).
`run_fem_analysis` overrides it to 600; `_commit_async` to 120; the async status-bar clear to 5.

The architecture §2.1 table records only "addon `gui_dispatch.py:183` timeout (**R**) 60" and
attributes the 90 exclusively to the client. Three things follow:

1. **Under one knob at R=60, `execute_code`'s run budget drops 90 → 60** — a behaviour regression
   on a *preserved-signature* tool, and a direct collision with the contract's "What must not
   change: the default 90 s budget; only its configurability is new."
2. The architecture's remark that C=150 at defaults *"equals today's socket default, which is not
   a coincidence"* binds to the wrong number for the one tool budgets are about:
   `freecad_client.py:78` computes `max(150, 2·90 + 30) = **210**` for `execute_code`. 150 is the
   default for everything else.
3. Today's nesting for `execute_code` is `Q(90) + R(90) + M(30) = 210 = S(210)` — an **equality**,
   not the strict `Q + R + M < C` the architecture says holds "by construction". The structured
   error is produced at exactly the instant the socket gives up.

**Options, not chosen here:** (a) default R to **90**, giving C=210, S=240 — preserves today's
`execute_code` behaviour and matches today's socket exactly; (b) keep R=60 and grant `execute_code`
a documented per-tool multiplier; (c) accept the regression and amend the contract's "must not
change" clause. **A human must pick.**

### CONFLICT-03 · Job `state` enum: 3 values (contract) vs 4 (architecture/wireframe)

Contract BC-01's schema declares `"state": running|done|failed`. Architecture §4.4 and wireframe
§3 add **`partial`** as a first-class state that is *never coerced to done*. Architecture wins and
the rebuild's whole point depends on it. **But:** wireframe rule 6 promises only that *fields* are
never removed — it says nothing about **enum widening**, which is equally breaking for a strict
consumer that validates `state in {"running","done","failed"}`. rxCAD pass 1 is such a consumer.
No canary in §5 covers the widening. Recommend BC-08 gain one; not added unilaterally.

### CONFLICT-04 · `reset_dispatch_health` → `gui_ping` → `dispatch_to_gui` → **rejected by the flag it is trying to clear** (design blocker)

`gui_dispatch.py:208-210` is the *first* thing `dispatch_to_gui` does:

```python
rejection = _dispatch_health.rejection()
if rejection is not None:
    return rejection
```

While `stuck`, **any** dispatch — including `gui_ping`'s no-op — returns `GUI_DISPATCH_STUCK`
without ever reaching the GUI thread. Therefore:

- `reset_dispatch_health`, which per architecture §3.2 "first calls `gui_ping(cap=5)`", can
  **never** see `alive: true` while the flag is set. BC-05 is unimplementable as written.
- `gui_ping` in the stuck state degenerates into exactly what `get_rpc_status` already is — a
  snapshot read — which is the failure `gui_ping` was created to fix. Worse, the wireframe's
  `test_ping_is_a_gui_dispatch` (static/mocked) **passes** for this broken implementation, so the
  suite would not catch it.

**Needs an explicit rejection bypass on the probe path** (e.g. `dispatch_to_gui(...,
bypass_rejection=True)` reserved for `gui_ping`, or a dedicated probe queue). Neither document
mentions it. Stage 2 must specify it; Stage 3 must not improvise it.

### CONFLICT-05 · `gui_ping.alive` under a busy GUI: pinned by the contract, unpinned by the wireframe

Contract BC-04 acceptance: during a 120 s busy task, `gui_ping(cap=5)` returns **`alive: false`,
`health.state: busy`** in ≤ 5 s. Wireframe `test_ping_reports_busy_within_cap` asserts only
`health.state == "busy"` and leaves `alive` unspecified. Architecture §3.2 says rxCAD's warm-up
loops "until `alive`", which only works if `alive` is false while the GUI is still registering
workbenches. The reading in N-7 (`alive` == "a no-op completed on the GUI thread within cap")
satisfies all three, but **it is a reading, not a quoted definition**. The schema must state it
explicitly so `alive: false` is never misread as "FreeCAD is dead".

### CONFLICT-06 · `reset_dispatch_health(force)` has no specified observable effect

Contract BC-05 defines `reset_dispatch_health()` — **no parameter**. Architecture §3.2 adds
`force=False` and then states force *"is refused unless alive as well"*, making both branches
require a successful ping. What `force=True` then does *differently* is described only as
clearing "a flag whose owning task id is gone" — which is not a condition either document defines
in testable terms. `test_force_still_requires_alive` pins the refusal path and nothing else. The
parameter is on a **new** tool, so no compatibility risk; it needs a definition or removal.

### CONFLICT-07 · BC-08's signature canary collides with the `execute_code` change it is meant to allow

`test_all_17_baseline_signatures_unchanged` "**Fails** on any rename or default change", while
wireframe §2 row 5 deliberately **adds** `timeout` to `execute_code`. Adding an optional parameter
is neither a rename nor a default change, but a naive `inspect.signature` equality test fails it.
The pinned table must be a **compat/superset** check (every baseline param present, same name,
same type, same default; additions permitted) rather than equality — or `execute_code` must carry
a stated exemption. Not decided here.

### CONFLICT-08 · Baseline test-module count: documents say 13, disk says 12

Wireframe §1 says "(all **13** existing test modules)" and `test_existing_suite_still_green` says
"the **13** baseline test modules". Measured under `C:\Claude\freecad-mcp\tests\` at `5dbfe2c`:
**12** modules — `test_async_status_client`, `test_async_status_text`, `test_client_timeouts`,
`test_dispatch_health`, `test_gui_dispatch`, `test_headless`, `test_object_validation`,
`test_parts_library`, `test_rpc_concurrency`, `test_rpc_handlers`, `test_serialize`,
`test_serialize_shape`. No `conftest.py`. A canary asserting 13 fails on a *correct* tree.

### CONFLICT-09 · `commit()`'s default timeout (120) is in neither budget table, and exceeds R

`_commit_async(fn, timeout=120)` (`rpc_server.py:86`) is a **third** budget. Under BC-07 each
chunk must complete within R, yet a `commit()` called with its documented default gets 120 —
**double R at defaults**, i.e. a default-timeout commit is itself a stuck-flag generator. Whether
`commit`'s default becomes `R` is unspecified. Note the tension: the default `120` is written into
`execute_code_async`'s **preserved public docstring** (`commit(fn, timeout=120)`), so changing it
alters documented behaviour of a preserved tool.

### GAP-A · MCP-level return encoding of the two new tools

Neither document states whether `gui_ping` / `reset_dispatch_health` return `list[TextContent]`
(the fork's universal `structured_output=False` convention) or structured output. Resolved in N-4
by the wireframe's own "silent → baseline wins" rule; flagged because rxCAD parses text and a
structured-output tool would change how its preflight reads the probe.

### GAP-B · Wireframe module layout omits files the tree cannot run without

Wireframe §1 lists 9 of the 16 baseline `rpc_server/` modules and **omits `operations/`
entirely**, though `server.py` imports 17 `*_operation` functions from it. Worse, `operations/`
is not marked `Δ` although two new tools cannot be added without two new operations. Corrected in
§6 above by the "silent → baseline wins" rule; flagged because a forge following §1 literally
produces a tree that does not import.

### GAP-C · Nobody sets the addon's R and Q

See CONFLICT-01.2. The invariant is asserted on both sides and configured on neither.

### GAP-D · C ("client tool wait") has no implementation today and none is specified

`freecad_client.execute_code` has **no** tool-level wait — it blocks on the XML-RPC socket, so the
effective client wait *is* the socket timeout. `EXECUTE_CODE_TIMEOUT = 90` is used **only** to
compute that socket timeout (`freecad_client.py:78`); it is not a wait. Wireframe §4 says "the
`execute_code` socket timeout becomes `S`" but never says what *implements* `C`. Either C is a
real new wait layer (a `concurrent.futures` deadline around the RPC call) or `C` is a derived
number that nothing enforces and `C ≤ S` is vacuous. Must be decided in Stage 2.

### Minor

- **`FreeCADRPC.TIMEOUT = 60` (`rpc_server.py:119`) is dead** — declared, never referenced. A
  forge re-authoring budgets may mistake it for the source of R. Delete it or wire it explicitly.
- **Two homes for one value** — wireframe §1 puts `last_gui_heartbeat_age_s` in
  `dispatch_health.py`'s snapshot, while §2 row 12 has `get_rpc_status` "gain heartbeat age".
  Pick one canonical owner (the dispatch snapshot) and have `get_rpc_status` embed it.
- **Field name/type drift** — contract BC-04 asks for `last_gui_heartbeat` (a *timestamp*, tested
  by "stops advancing"); architecture and wireframe expose `last_gui_heartbeat_age_s` (an *age*,
  which *grows* under the same failure). Architecture wins; the contract's acceptance sentence
  must be restated for the age form.
- **Wireframe §7 BC-01 row** says "a 200 s job's `result` readable after the tool call itself
  timed out" — `execute_code_async` returns immediately and cannot time out. The sentence
  describes `execute_code`; harmless, but the acceptance wording should name the right tool.

---

*Stage 1 artifact. Design input to Stages 2–4. It builds nothing, dispatches nothing to FreeCAD,
and writes nothing to `C:\Claude\freecad-mcp`.*
