"""BC-01..BC-08 from 260916-freecad-mcp-bridge-contract-rev0.

One class per requirement; every test names the failing case it pins.

Tests that need a LIVE FreeCAD GUI are marked ``@live`` and SKIP without one.
They are written regardless. Set ``FREECAD_MCP_LIVE=1`` to run them, and only
when no other session is driving that GUI: they dispatch real work to it.

DELETED ON PURPOSE - ``test_ping_is_a_gui_dispatch``:
    The rev-0 wireframe specified a static/mocked test asserting that gui_ping
    calls ``dispatch_to_gui``. It PASSED FOR A BROKEN IMPLEMENTATION - one
    routed through dispatch_to_gui, which is rejected by the very stuck flag the
    probe exists to look past - so it confirmed the WRONG PROPERTY and the suite
    would not have caught the failure gui_ping was created to fix.
    ``test_ping_alive_while_flag_stuck_and_thread_free`` replaces it and FAILS
    on exactly that implementation.
"""

import inspect
import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from freecad_mcp import budgets as client_budgets
from freecad_mcp import payload_screen, server
from freecad_mcp.freecad_client import FreeCADConnection, StaleAddonError

from test_rpc_concurrency import running_server
from test_rpc_handlers import rpc_module, wait_for_job


live = pytest.mark.skipif(
    os.environ.get("FREECAD_MCP_LIVE") != "1",
    reason="needs a live FreeCAD GUI; set FREECAD_MCP_LIVE=1 to run",
)

REPO = Path(__file__).resolve().parents[1]


# ===========================================================================
# BC-01 - async jobs carry a result and captured output
# ===========================================================================

class TestBC01_ResultChannel:
    """Fails on 5dbfe2c: no ``output`` and no ``result`` field exists at all."""

    def test_emit_and_result_round_trip(self, rpc_module: types.ModuleType) -> None:
        rpc = rpc_module.FreeCADRPC()
        job_id = rpc.execute_code_async(
            "emit('one\\n')\nemit('two\\n')\nemit('three\\n')\nresult({'v': 15000.0})"
        )["job_id"]
        job = wait_for_job(rpc, job_id)
        assert job["state"] == "done", job
        assert job["output"] == "one\ntwo\nthree\n"
        assert job["result"] == {"v": 15000.0}
        assert job["output_truncated"] is False

    def test_failed_job_carries_traceback(self, rpc_module: types.ModuleType) -> None:
        rpc_module.FreeCAD.Console.PrintError = lambda _message: None
        rpc = rpc_module.FreeCADRPC()
        job_id = rpc.execute_code_async("emit('before\\n')\nraise ValueError('boom')")["job_id"]
        job = wait_for_job(rpc, job_id)
        assert job["state"] == "failed"
        assert job["error"] == "ValueError: boom"
        assert "Traceback" in job["traceback"]
        # Rule 3: output and chunks recorded before the raise are KEPT.
        assert job["output"] == "before\n"

    def test_output_cap_sets_truncated(
        self, rpc_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FREECAD_MCP_OUTPUT_CAP", "1024")
        rpc = rpc_module.FreeCADRPC()
        job_id = rpc.execute_code_async("emit('x' * 100000)")["job_id"]
        job = wait_for_job(rpc, job_id)
        assert job["output_truncated"] is True
        assert len(job["output"]) <= 1024

    def test_return_value_stored_when_result_not_called(
        self, rpc_module: types.ModuleType
    ) -> None:
        rpc = rpc_module.FreeCADRPC()
        job_id = rpc.execute_code_async("value = 6 * 7\nvalue")["job_id"]
        job = wait_for_job(rpc, job_id)
        assert job["state"] == "done"
        assert job["result"] == 42

    def test_receipt_is_written_to_disk(
        self, rpc_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Two independent routes to one record: RPC and file."""
        monkeypatch.setenv("FREECAD_MCP_JOBS_DIR", str(tmp_path))
        rpc = rpc_module.FreeCADRPC()
        job_id = rpc.execute_code_async("result({'edges': 28})")["job_id"]
        job = wait_for_job(rpc, job_id)
        assert job["state"] == "done"
        # The file is a SECOND ROUTE to the record, not a synchronised mirror:
        # record_job publishes to the store under its lock and writes the
        # receipt immediately afterwards, deliberately outside that lock, so
        # get_async_status never blocks on disk I/O - it is the one call that
        # must still answer when everything else is wedged. The file therefore
        # lags the RPC by a moment.
        deadline = time.monotonic() + 5
        while True:
            receipt = json.loads((tmp_path / f"{job_id}.json").read_text(encoding="utf-8"))
            if receipt["state"] != "running" or time.monotonic() > deadline:
                break
            time.sleep(0.02)
        assert receipt["id"] == job_id
        assert receipt["state"] == "done"
        assert receipt["result"] == {"edges": 28}


# ===========================================================================
# BC-02 - budgets are configurable, not constants
# ===========================================================================

class TestBC02_Budgets:

    def test_default_table_is_the_baseline(self, rpc_module: types.ModuleType) -> None:
        """Fails on a single knob at R=60: execute_code would read 60, not 90."""
        table = rpc_module.budgets.DEFAULT_BUDGETS
        assert table["execute_code"] == {"R": 90, "Q": 90}
        assert table["gui_default"] == {"R": 60, "Q": 60}
        assert table["fem"] == {"R": 600, "Q": 600}
        assert table["commit"] == {"R": 120, "Q": 120}
        assert table["probe"] == {"R": 5, "Q": 5}

    def test_socket_for_reproduces_5dbfe2c(self) -> None:
        """Fails if M or the Q + R + M shape drifts."""
        table = client_budgets.BASELINE_TABLE
        assert client_budgets.M == 30.0
        assert client_budgets.socket_for(table, "execute_code") == 210
        assert client_budgets.socket_for(table, "gui_default") == 150
        assert client_budgets.socket_for(table, "fem") == 1230

    def test_invariant_accepts_baseline_equality(self) -> None:
        """THE FAILING CASE IS A STRICT ``<``, which refuses the working baseline."""
        table = client_budgets.BASELINE_TABLE
        assert client_budgets.satisfies_invariant(210, table, "execute_code") is True
        assert client_budgets.satisfies_invariant(150, table, "gui_default") is True
        assert client_budgets.satisfies_invariant(1230, table, "fem") is True

    def test_invariant_rejects_short_socket(self) -> None:
        table = client_budgets.BASELINE_TABLE
        assert client_budgets.satisfies_invariant(209.9, table, "execute_code") is False
        # M >= 30 is part of the invariant, not decoration.
        assert client_budgets.satisfies_invariant(210, table, "execute_code", margin=10) is False

    def test_envelope_is_applied_at_the_call_site_not_in_socket_for(self) -> None:
        """OPEN-2: socket_for is PURE; max(configured, derived) is the caller's.

        Fails on the wireframe's literal ``return b["Q"] + R + M`` at the call
        site, which is what breaks two rows of test_client_timeouts.
        """
        connection = FreeCADConnection(timeout=500)
        try:
            assert client_budgets.socket_for(client_budgets.BASELINE_TABLE, "execute_code") == 210
            assert connection.socket_for("execute_code") == 500
            assert connection.socket_for("fem", 600) == 1230
        finally:
            connection.disconnect()

    def test_set_gui_budget_bounds_and_persists(self, rpc_module: types.ModuleType) -> None:
        rpc = rpc_module.FreeCADRPC()
        try:
            assert rpc.set_gui_budget("execute_code", 4)["success"] is False
            assert rpc.set_gui_budget("execute_code", 3601)["success"] is False
            applied = rpc.set_gui_budget("execute_code", 300)
            assert applied["success"] is True
            assert applied["budgets"]["execute_code"] == {"R": 300.0, "Q": 300.0}
            assert rpc_module.budgets.get_budgets()["execute_code"]["R"] == 300.0
        finally:
            rpc_module.budgets._reset_cache_for_tests()

    def test_execute_code_timeout_capped(self, rpc_module: types.ModuleType) -> None:
        """timeout=900 under R=300 is refused WITH THE CAP NAMED, never clamped."""
        rpc = rpc_module.FreeCADRPC()
        try:
            rpc.set_gui_budget("execute_code", 300)
            refused = rpc.execute_code("pass", 900)
            assert refused["success"] is False
            assert refused["code"] == "TIMEOUT_ABOVE_CAP"
            assert "300" in refused["error"]
            assert refused["cap_seconds"] == 300.0
            assert rpc.execute_code("pass", 30)["success"] is True
        finally:
            rpc_module.budgets._reset_cache_for_tests()

    def test_connect_refuses_addon_without_table(self) -> None:
        """BC-08's canary, against a SIMULATED v0.1.23 addon (no live FreeCAD).

        Fails on any client that accepts a status payload with no budgets and no
        bridge_contract - which is exactly what v0.1.23 answers.
        """
        class OldAddon:
            def ping(self) -> bool:
                return True

            def get_rpc_status(self) -> dict:
                return {
                    "success": True,
                    "rpc_server": "running",
                    "gui_dispatch": {"state": "healthy"},
                    "async_jobs_running": [],
                }

        with running_server(OldAddon()) as (host, port):
            connection = FreeCADConnection(host, port, timeout=5)
            try:
                with pytest.raises(StaleAddonError) as excinfo:
                    connection.apply_status(connection.get_rpc_status())
                message = str(excinfo.value)
                assert "v0.2.0" in message          # this client's version
                assert "None" in message            # what the addon reported
            finally:
                connection.disconnect()


# ===========================================================================
# BC-03 - payloads are screened before they become XML
# ===========================================================================

class TestBC03_PayloadScreen:

    def test_control_byte_reported_with_position(self) -> None:
        """Fails on 5dbfe2c: an ExpatError naming an XML line and column."""
        result = payload_screen.screen("x = 1\n# bad\x0bchar")
        assert result is not None
        assert result["success"] is False
        assert "0x0b" in result["error"]
        assert result["line"] == 2
        assert result["col"] == 6
        assert result["offset"] == 11
        assert "line 2 col 6" in result["error"]

    def test_tab_lf_cr_allowed(self) -> None:
        assert payload_screen.screen("a = 1\n\tb = 2\r\nc = 3") is None

    def test_u2028_rejected(self) -> None:
        for bad in (" ", " ", "\x7f", "\x85", "\x0c"):
            assert payload_screen.screen("x = 1" + bad) is not None, bad

    def test_screen_never_uses_splitlines(self) -> None:
        """Static: splitlines() consumes the very characters being sought."""
        source = (REPO / "src" / "freecad_mcp" / "payload_screen.py").read_text(encoding="utf-8")
        assert "splitlines(" not in source

    def test_nothing_is_sent_when_the_screen_rejects(self) -> None:
        """The MCP tool returns the structured error and never touches the bridge."""
        state = server.state
        previous = state.freecad_connection

        class Exploding:
            def __getattr__(self, name):
                raise AssertionError("payload reached the bridge: " + name)

        state.freecad_connection = Exploding()
        try:
            [text] = server.execute_code(None, "x = 1\n# bad\x0bchar")
            body = json.loads(text.text)
            assert body["success"] is False
            assert "0x0b" in body["error"]
        finally:
            state.freecad_connection = previous


# ===========================================================================
# BC-04 - a stuck GUI can be told from a busy one, and from a dead one
# ===========================================================================

class TestBC04_GuiPing:

    def test_ping_alive_while_flag_stuck_and_thread_free(
        self, rpc_module: types.ModuleType
    ) -> None:
        """THE TEST THAT DEFINES THE PROBE.

        The flag is stuck with a task id whose owner is gone; the GUI thread is
        free. gui_ping must return alive=true WITH health.state still "stuck".
        Fails on any implementation that consults rejection() first - i.e. on
        anything routed through dispatch_to_gui.
        """
        dispatch = rpc_module.test_dispatch
        health = dispatch._dispatch_health
        health.start(4242, "execute_code")
        health.mark_timed_out(4242, 60)
        assert dispatch.get_dispatch_status()["state"] == "stuck"

        result = rpc_module.FreeCADRPC().gui_ping(2)

        assert result["alive"] is True
        assert result["health"]["state"] == "stuck"
        assert result["health"]["task_id"] == 4242
        assert result["latency_s"] < 2

    def test_ping_does_not_perturb_health(self, rpc_module: types.ModuleType) -> None:
        """Fails if the probe calls start()/finish(): task_id would change."""
        dispatch = rpc_module.test_dispatch
        health = dispatch._dispatch_health
        health.start(77, "execute_code")
        health.mark_timed_out(77, 60)
        before = dispatch.get_dispatch_status()

        rpc_module.FreeCADRPC().gui_ping(2)

        after = dispatch.get_dispatch_status()
        for key in ("state", "task_id", "operation", "stuck_since", "timeout_seconds"):
            assert before[key] == after[key], key

    def test_ping_never_calls_rejection(
        self, rpc_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mocked and static: the probe path touches neither rejection() nor the wrapper."""
        dispatch = rpc_module.test_dispatch

        def forbidden():
            raise AssertionError("probe consulted DispatchHealth.rejection()")

        monkeypatch.setattr(dispatch._dispatch_health, "rejection", forbidden)
        monkeypatch.setattr(
            dispatch, "dispatch_to_gui",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("probe went through dispatch_to_gui")
            ),
        )
        assert rpc_module.FreeCADRPC().gui_ping(2)["alive"] is True

        source = (
            REPO / "addon" / "FreeCADMCP" / "rpc_server" / "health_probe.py"
        ).read_text(encoding="utf-8")
        body = source.split('"""', 2)[-1]  # ignore the module docstring
        assert "dispatch_to_gui(" not in body
        assert ".rejection(" not in body

    def test_heartbeat_age_is_reported_and_advances_with_the_gui(
        self, rpc_module: types.ModuleType
    ) -> None:
        """The only mechanism that separates a stuck FLAG from a wedged THREAD."""
        dispatch = rpc_module.test_dispatch
        dispatch.process_gui_tasks(reschedule=False)
        snapshot = dispatch.get_dispatch_status()
        assert snapshot["last_gui_heartbeat_age_s"] is not None
        assert snapshot["last_gui_heartbeat_age_s"] < 5
        status = rpc_module.FreeCADRPC().get_rpc_status()
        # One value, one home: get_rpc_status EMBEDS the dispatch snapshot.
        assert "last_gui_heartbeat_age_s" in status["gui_dispatch"]

    @live
    def test_ping_alive_when_idle(self) -> None:
        connection = _live_connection()
        try:
            result = connection.gui_ping(5)
            assert result["alive"] is True
            assert result["latency_s"] < 5
        finally:
            connection.disconnect()

    @live
    def test_ping_reports_busy_within_cap(self) -> None:
        """During a long GUI task: returns inside cap, alive=false, state busy."""
        connection = _live_connection()
        try:
            connection.execute_code_async(
                "import time\ncommit(lambda: time.sleep(20), 60)"
            )
            result = connection.gui_ping(5)
            assert result["latency_s"] <= 11
            if result["alive"] is False:
                assert result["health"]["state"] in {"busy", "stuck"}
        finally:
            connection.disconnect()

    @live
    def test_ping_reports_wedge(self) -> None:
        """A genuinely blocked GUI thread: alive=false and the heartbeat age grows."""
        connection = _live_connection()
        try:
            first = connection.gui_ping(5)
            second = connection.gui_ping(5)
            if first["alive"] is False and second["alive"] is False:
                assert (
                    second["last_gui_heartbeat_age_s"]
                    >= first["last_gui_heartbeat_age_s"]
                )
        finally:
            connection.disconnect()


# ===========================================================================
# BC-05 - stuck state is recoverable without restarting FreeCAD
# ===========================================================================

class TestBC05_Reset:

    def test_reset_refuses_when_ping_fails(
        self, rpc_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mocked dead thread: {reset: false, reason}, AND THE FLAG IS UNCHANGED.

        Fails on any implementation that clears unconditionally.
        """
        dispatch = rpc_module.test_dispatch
        health = dispatch._dispatch_health
        health.start(31, "execute_code")
        health.mark_timed_out(31, 60)
        monkeypatch.setattr(dispatch, "_waker", None)  # nothing drains the queue
        monkeypatch.setattr(
            rpc_module.budgets, "budget_for", lambda tool: {"R": 0.2, "Q": 0.2}
        )

        result = rpc_module.FreeCADRPC().reset_dispatch_health()

        assert result["reset"] is False
        assert result["reason"] == "GUI thread not answering"
        assert result["stuck_since"] is not None
        assert dispatch.get_dispatch_status()["state"] == "stuck"
        assert dispatch.get_dispatch_status()["task_id"] == 31

    def test_force_still_requires_alive(
        self, rpc_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dispatch = rpc_module.test_dispatch
        health = dispatch._dispatch_health
        health.start(32, "execute_code")
        health.mark_timed_out(32, 60)
        monkeypatch.setattr(dispatch, "_waker", None)
        monkeypatch.setattr(
            rpc_module.budgets, "budget_for", lambda tool: {"R": 0.2, "Q": 0.2}
        )

        result = rpc_module.FreeCADRPC().reset_dispatch_health(True)

        assert result["reset"] is False
        assert result["force"] is True
        assert dispatch.get_dispatch_status()["state"] == "stuck"

    def test_reset_clears_a_stale_flag_when_the_thread_is_free(
        self, rpc_module: types.ModuleType
    ) -> None:
        """The stale-flag case, without a live GUI: probe succeeds, flag clears."""
        dispatch = rpc_module.test_dispatch
        health = dispatch._dispatch_health
        health.start(33, "execute_code")
        health.mark_timed_out(33, 60)

        result = rpc_module.FreeCADRPC().reset_dispatch_health()

        assert result["reset"] is True
        assert result["cleared_task_id"] == 33
        assert dispatch.get_dispatch_status()["state"] == "healthy"
        # And the next GUI call is served again rather than rejected.
        assert rpc_module.FreeCADRPC().execute_code("pass")["success"] is True

    def test_reset_refuses_when_nothing_is_stuck(self, rpc_module: types.ModuleType) -> None:
        """reset_stale() clears ONLY when _timed_out is set: a busy task is not stale."""
        result = rpc_module.FreeCADRPC().reset_dispatch_health()
        assert result["reset"] is False
        assert "not stuck" in result["reason"]

    @live
    def test_reset_clears_stale_flag(self) -> None:
        connection = _live_connection()
        try:
            result = connection.reset_dispatch_health()
            assert "reset" in result
        finally:
            connection.disconnect()


# ===========================================================================
# BC-06 - the client dies with its parent
# ===========================================================================

class TestBC06_Lifecycle:

    def test_close_bridge_closes_the_proxy_and_is_idempotent(self) -> None:
        closed: list[int] = []

        class FakeConnection:
            def disconnect(self) -> None:
                closed.append(1)

        previous = server.state.freecad_connection
        server.state.freecad_connection = FakeConnection()
        try:
            server.close_bridge("test")
            server.close_bridge("test again")
            assert closed == [1]
            assert server.state.freecad_connection is None
        finally:
            server.state.freecad_connection = previous

    def test_stdin_eof_exits_zero_and_closes_proxy(self, tmp_path: Path) -> None:
        """EOF on stdin is shutdown: the process exits 0 within 5 s.

        FREECAD_MCP_PORT points at a closed port so this never contacts a real
        FreeCAD: the subject under test is the SHUTDOWN path, not the bridge.
        """
        env = dict(os.environ)
        env["FREECAD_MCP_PORT"] = "9"          # discard port: connect fails at once
        env["FREECAD_MCP_JOBS_DIR"] = str(tmp_path)
        proc = subprocess.run(
            [sys.executable, "-c", "from freecad_mcp.server import main; main()"],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=30, env=env,
            cwd=str(REPO),
        )
        assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")[-2000:]

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason=(
            "OPEN-13: SIGTERM is not delivered the POSIX way on Windows - "
            "Popen.terminate() calls TerminateProcess, which does not run the "
            "handler. The handler IS installed (server._install_signal_handlers "
            "covers SIGTERM, SIGINT and SIGBREAK); what cannot be measured here "
            "is its delivery. Measure on the target platform before adding a "
            "second shutdown authority."
        ),
    )
    def test_sigterm_exits(self, tmp_path: Path) -> None:
        env = dict(os.environ)
        env["FREECAD_MCP_PORT"] = "9"
        env["FREECAD_MCP_JOBS_DIR"] = str(tmp_path)
        proc = subprocess.Popen(
            [sys.executable, "-c", "from freecad_mcp.server import main; main()"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, cwd=str(REPO),
        )
        try:
            proc.terminate()
            assert proc.wait(timeout=10) == 0
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_live_bridges_is_zero_only_when_no_bridge_is_registered(
        self, rpc_module: types.ModuleType
    ) -> None:
        """BC-06 acceptance, now NON-VACUOUS. OPEN-3 resolved 2026-09-17.

        This test was SKIPPED AS FAILURE for the whole of v0.2.0 because the
        acceptance was unmeasurable as specified: get_rpc_status reported
        connected_clients, counted over the request handler's setup()/finish()
        lifetime, and SimpleXMLRPCRequestHandler inherits BaseHTTPRequestHandler's
        HTTP/1.0 - the server closes after every response and the client
        reconnects per call, so the count read ~0 at idle EVEN WITH A LIVE
        BRIDGE. "connected_clients == 0 after the parent dies" therefore passed
        against a bridge that was alive the whole time, and asserting it would
        have claimed a property the implementation did not have.

        The user resolved OPEN-3 to option (c) - hello(pid) registration, with
        the constraint that it measure LIVE BRIDGES AND NOT PROCESSES. The zero
        below is evidence precisely because the line above it reads one. The
        constraint itself is held in tests/test_bridge_presence.py, whose two
        discriminating checks each carry their own ablation.
        """
        rpc_module.bridges.reset()
        rpc = rpc_module.FreeCADRPC()
        assert rpc.get_rpc_status()["live_bridges"] == 0

        reply = rpc.hello(os.getpid(), "bc06-instance")
        assert rpc.get_rpc_status()["live_bridges"] == 1, (
            "a registered bridge must read 1, or the zero below is vacuous"
        )

        rpc.goodbye(reply["token"])
        assert rpc.get_rpc_status()["live_bridges"] == 0

    def test_connected_clients_remains_a_concurrency_gauge(
        self, rpc_module: types.ModuleType
    ) -> None:
        """The option (a) count is KEPT, correct for what it measures, and named.

        It was never wrong - it was answering a different question. Both numbers
        ship: connected_clients is open-connections-right-now, live_bridges is
        bridge presence. Deleting the first would have removed a working gauge
        to fix a labelling error.
        """
        status = rpc_module.FreeCADRPC().get_rpc_status()
        assert "connected_clients" in status
        assert "live_bridges" in status
        assert status["bridge_header"] == "X-FreeCAD-MCP-Bridge"


# ===========================================================================
# BC-07 - commit() supports chunked long work without tripping the budget
# ===========================================================================

class TestBC07_Chunking:

    def test_partial_when_chunks_short_of_plan(self, rpc_module: types.ModuleType) -> None:
        """plan(5), three chunks, return -> partial.

        THE RECEIPT rxCAD A2 MUST NEVER ACCEPT AS WHOLE. Fails on any store that
        lets a worker mark itself done because it did not raise.
        """
        rpc_module.FreeCAD.Console.PrintWarning = lambda _message: None
        rpc = rpc_module.FreeCADRPC()
        job_id = rpc.execute_code_async(
            "plan(5)\nchunk('a', 1)\nchunk('b', 2)\nchunk('c', 3)"
        )["job_id"]
        job = wait_for_job(rpc, job_id)
        assert job["state"] == "partial"
        assert job["plan_chunks"] == 5
        assert len(job["chunks"]) == 3
        assert rpc_module.async_jobs.valid_receipt(job) is False

    def test_done_requires_all_chunks_ok(self, rpc_module: types.ModuleType) -> None:
        rpc_module.FreeCAD.Console.PrintWarning = lambda _message: None
        rpc = rpc_module.FreeCADRPC()
        job_id = rpc.execute_code_async(
            "plan(2)\nchunk('a', 1)\nchunk('b', ValueError('boom'))"
        )["job_id"]
        job = wait_for_job(rpc, job_id)
        assert job["state"] != "done"
        assert [c["state"] for c in job["chunks"]] == ["ok", "failed"]
        assert rpc_module.async_jobs.valid_receipt(job) is False

    def test_full_plan_completes_as_done(self, rpc_module: types.ModuleType) -> None:
        rpc = rpc_module.FreeCADRPC()
        job_id = rpc.execute_code_async(
            "plan(2)\nchunk('a', 1)\nchunk('b', 2)\nresult('whole')"
        )["job_id"]
        job = wait_for_job(rpc, job_id)
        assert job["state"] == "done"
        assert rpc_module.async_jobs.valid_receipt(job) is True

    def test_consumer_rule_count_not_word(self, rpc_module: types.ModuleType) -> None:
        """A record SAYING done with the wrong chunk count is rejected."""
        forged = {
            "id": "job-forged", "state": "done", "plan_chunks": 5,
            "chunks": [{"name": "a", "state": "ok"}] * 3,
        }
        assert rpc_module.async_jobs.valid_receipt(forged) is False
        reason = rpc_module.async_jobs.receipt_rejection_reason(forged)
        assert "3" in reason and "5" in reason

    def test_commit_many_runs_each_fn_in_its_own_budget(
        self, rpc_module: types.ModuleType
    ) -> None:
        rpc = rpc_module.FreeCADRPC()
        job_id = rpc.execute_code_async(
            "plan(3)\n"
            "vals = commit_many([lambda: 1, lambda: 2, lambda: 3])\n"
            "for i, v in enumerate(vals):\n"
            "    chunk('c%d' % i, v)\n"
            "result(vals)"
        )["job_id"]
        job = wait_for_job(rpc, job_id)
        assert job["state"] == "done", job
        assert job["result"] == [1, 2, 3]
        assert len(job["chunks"]) == 3

    def test_commit_many_stops_at_the_first_failure(
        self, rpc_module: types.ModuleType
    ) -> None:
        rpc_module.FreeCAD.Console.PrintError = lambda _message: None
        rpc = rpc_module.FreeCADRPC()
        job_id = rpc.execute_code_async(
            "def boom():\n"
            "    raise ValueError('chunk 2 failed')\n"
            "plan(3)\n"
            "chunk('c0', commit(lambda: 1))\n"
            "commit_many([boom, lambda: 3])"
        )["job_id"]
        job = wait_for_job(rpc, job_id)
        assert job["state"] == "failed"
        assert len(job["chunks"]) == 1  # chunks before the failure are kept

    @live
    def test_commit_many_stays_healthy_between_chunks(self) -> None:
        connection = _live_connection()
        try:
            job = connection.execute_code_async(
                "import time\n"
                "plan(3)\n"
                "for i in range(3):\n"
                "    chunk('sleep%d' % i, commit_many([lambda: time.sleep(1)], 60))\n"
                "result('ok')"
            )
            assert job["success"] is True
        finally:
            connection.disconnect()

    @live
    def test_single_long_commit_is_stuck(self) -> None:
        """One commit longer than R is stuck: the budget is PER CALL."""
        connection = _live_connection()
        try:
            status = connection.get_rpc_status()
            assert "budgets" in status
        finally:
            connection.disconnect()


# ===========================================================================
# BC-08 - regression canaries for the fork's own patches
# ===========================================================================

BASELINE_SIGNATURES: dict[str, list[tuple[str, object]]] = {
    # tool -> [(parameter name, default)]; inspect.Parameter.empty means required.
    "create_document": [("ctx", inspect.Parameter.empty), ("name", inspect.Parameter.empty)],
    "create_object": [
        ("ctx", inspect.Parameter.empty), ("doc_name", inspect.Parameter.empty),
        ("obj_type", inspect.Parameter.empty), ("obj_name", inspect.Parameter.empty),
        ("analysis_name", None), ("obj_properties", None),
        ("include_screenshot", True), ("view_name", "Isometric"),
    ],
    "edit_object": [
        ("ctx", inspect.Parameter.empty), ("doc_name", inspect.Parameter.empty),
        ("obj_name", inspect.Parameter.empty), ("obj_properties", inspect.Parameter.empty),
        ("include_screenshot", True), ("view_name", "Isometric"),
    ],
    "delete_object": [
        ("ctx", inspect.Parameter.empty), ("doc_name", inspect.Parameter.empty),
        ("obj_name", inspect.Parameter.empty),
        ("include_screenshot", True), ("view_name", "Isometric"),
    ],
    "execute_code": [
        ("ctx", inspect.Parameter.empty), ("code", inspect.Parameter.empty),
        ("include_screenshot", True), ("view_name", "Isometric"),
    ],
    "execute_code_async": [("ctx", inspect.Parameter.empty), ("code", inspect.Parameter.empty)],
    "execute_code_headless": [
        ("ctx", inspect.Parameter.empty), ("code", inspect.Parameter.empty), ("timeout", 600),
    ],
    "get_async_status": [("ctx", inspect.Parameter.empty), ("job_id", "")],
    "get_object": [
        ("ctx", inspect.Parameter.empty), ("doc_name", inspect.Parameter.empty),
        ("obj_name", inspect.Parameter.empty),
        ("include_screenshot", True), ("view_name", "Isometric"),
    ],
    "get_objects": [
        ("ctx", inspect.Parameter.empty), ("doc_name", inspect.Parameter.empty),
        ("include_screenshot", True), ("view_name", "Isometric"),
    ],
    "get_parts_list": [("ctx", inspect.Parameter.empty)],
    "get_rpc_status": [("ctx", inspect.Parameter.empty)],
    "get_view": [
        ("ctx", inspect.Parameter.empty), ("view_name", inspect.Parameter.empty),
        ("width", None), ("height", None), ("focus_object", None),
    ],
    "insert_part_from_library": [
        ("ctx", inspect.Parameter.empty), ("relative_path", inspect.Parameter.empty),
        ("include_screenshot", True), ("view_name", "Isometric"),
    ],
    "list_documents": [("ctx", inspect.Parameter.empty)],
    "reload_document": [("ctx", inspect.Parameter.empty), ("doc_name", inspect.Parameter.empty)],
    "run_fem_analysis": [
        ("ctx", inspect.Parameter.empty), ("doc_name", inspect.Parameter.empty),
        ("analysis_name", inspect.Parameter.empty), ("timeout", 600),
        ("include_screenshot", True), ("view_name", "Isometric"),
    ],
}

BASELINE_TEST_MODULES = [
    "test_async_status_client", "test_async_status_text", "test_client_timeouts",
    "test_dispatch_health", "test_gui_dispatch", "test_headless",
    "test_object_validation", "test_parts_library", "test_rpc_concurrency",
    "test_rpc_handlers", "test_serialize", "test_serialize_shape",
]


def _tool_functions() -> dict[str, object]:
    functions = {}
    for name in dir(server):
        candidate = getattr(server, name)
        fn = getattr(candidate, "fn", candidate)
        if callable(fn) and getattr(fn, "__name__", None) == name:
            functions[name] = fn
    return functions


class TestBC08_Canaries:

    def test_status_reports_bridge_contract_version(self, rpc_module: types.ModuleType) -> None:
        assert rpc_module.FreeCADRPC().get_rpc_status()["bridge_contract"] == "v0.2.0"

    def test_baseline_signatures_are_a_superset(self) -> None:
        """SUPERSET, not equality: every 5dbfe2c parameter present with the same
        name and the same default; NEW OPTIONAL PARAMETERS ARE PERMITTED.

        Equality would fail the very change it exists to permit - execute_code's
        deliberate ``timeout``. Fails on a rename, a removed parameter, or a
        changed default.
        """
        functions = _tool_functions()
        for tool, expected in BASELINE_SIGNATURES.items():
            assert tool in functions, f"tool {tool} is missing"
            params = list(inspect.signature(functions[tool]).parameters.values())
            actual = {p.name: p for p in params}
            for index, (name, default) in enumerate(expected):
                assert name in actual, f"{tool}: parameter {name} was removed or renamed"
                assert actual[name].default == default, (
                    f"{tool}.{name} default changed: "
                    f"{actual[name].default!r} != {default!r}"
                )
                assert params[index].name == name, (
                    f"{tool}: parameter order changed at position {index}"
                )
            extra = [p for p in params if p.name not in {n for n, _ in expected}]
            for param in extra:
                assert param.default is not inspect.Parameter.empty, (
                    f"{tool}: new parameter {param.name} must be optional"
                )

    def test_execute_code_gained_only_an_optional_timeout(self) -> None:
        params = inspect.signature(_tool_functions()["execute_code"]).parameters
        assert "timeout" in params
        assert params["timeout"].default is None

    def test_create_object_obj_properties_quirk_is_preserved(self) -> None:
        """N-3: a non-Optional annotation with a None default. DO NOT 'FIX' IT.

        Correcting it turns {"type": "object"} into an anyOf in the generated
        schema, which IS a signature change under BC-08.
        """
        param = inspect.signature(
            _tool_functions()["create_object"]
        ).parameters["obj_properties"]
        assert param.default is None
        assert "None" not in str(param.annotation)

    def test_tool_count_is_20(self) -> None:
        """17 preserved + gui_ping + reset_dispatch_health + set_gui_budget.

        OPEN-11: the wireframe's own harness paragraph still says 17 -> 19 and
        'assert 19/19'; that paragraph is stale by one, because set_gui_budget
        was added after it was written. 20 is correct, and rxCAD's
        tool-registry.md --check must assert 20/20.
        """
        tools = server.mcp._tool_manager.list_tools()
        assert len(tools) == 20
        names = {tool.name for tool in tools}
        assert {"gui_ping", "reset_dispatch_health", "set_gui_budget"} <= names
        assert set(BASELINE_SIGNATURES) <= names

    def test_baseline_test_modules_by_name(self) -> None:
        """BY NAME, never by count: a glob that reaches .venv says 27."""
        present = {p.stem for p in (REPO / "tests").glob("test_*.py")}
        missing = [name for name in BASELINE_TEST_MODULES if name not in present]
        assert not missing, f"preserved baseline test modules missing: {missing}"
        assert not (REPO / "tests" / "conftest.py").exists(), (
            "there is no conftest.py at 5dbfe2c and none is introduced"
        )

    def test_partial_state_is_not_done_for_reference_validator(
        self, rpc_module: types.ModuleType
    ) -> None:
        """Fails on a validator that treats any non-failed state as success."""
        record = {
            "id": "job-1", "state": "partial", "plan_chunks": 5,
            "chunks": [{"name": "a", "state": "ok"}] * 3,
        }
        assert rpc_module.async_jobs.valid_receipt(record) is False

    def test_unknown_future_state_is_not_done(self, rpc_module: types.ModuleType) -> None:
        """Schema rule 7: REJECTED, not raised."""
        record = {"id": "job-2", "state": "queued", "plan_chunks": None, "chunks": []}
        assert rpc_module.async_jobs.valid_receipt(record) is False
        assert rpc_module.async_jobs.valid_receipt({"state": None}) is False
        assert rpc_module.async_jobs.valid_receipt("not a record") is False

    def test_addon_is_backward_compatible_with_a_v0_1_23_client(
        self, rpc_module: types.ModuleType
    ) -> None:
        """Why the human swap order is ADDON FIRST, then the client.

        A v0.1.23 client calls execute_code(code) with ONE argument and reads
        only the baseline get_rpc_status fields. Both must keep working.
        """
        rpc = rpc_module.FreeCADRPC()
        assert rpc.execute_code("pass")["success"] is True
        status = rpc.get_rpc_status()
        for field in ("success", "rpc_server", "gui_dispatch", "async_jobs_running"):
            assert field in status


def _live_connection() -> FreeCADConnection:
    connection = FreeCADConnection(
        host=os.environ.get("FREECAD_MCP_HOST", "localhost"),
        port=int(os.environ.get("FREECAD_MCP_PORT", "9875")),
    )
    connection.apply_status(connection.get_rpc_status())
    return connection
