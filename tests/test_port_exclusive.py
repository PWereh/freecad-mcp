"""The RPC port is exclusive: a second FreeCAD must fail to bind, never co-listen.

Measured 2026-10-02: with SimpleXMLRPCServer's default allow_reuse_address=True, two FreeCAD
processes were both LISTENING on 127.0.0.1:9875, each with its own documents. These tests fail
against that default (ablated: the first two fail, the restart test passes either way, which is
why it is here - the fix must not cost the Stop->Start path).
"""
import socket
import threading
import xmlrpc.client
from xmlrpc.server import SimpleXMLRPCServer

import pytest

from test_rpc_concurrency import filtered_server_class


def _serve(port=0):
    server = filtered_server_class()(("127.0.0.1", port), allow_none=True, logRequests=False)
    server.register_function(lambda: "pong", "ping")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_second_bridge_server_cannot_co_bind():
    first = _serve()
    try:
        port = first.server_address[1]
        with pytest.raises(OSError):
            second = filtered_server_class()(("127.0.0.1", port), logRequests=False)
            second.server_close()
    finally:
        first.shutdown()
        first.server_close()


def test_old_style_reuse_server_cannot_co_bind():
    # an older addon still binds with SO_REUSEADDR; it must not join a port the fixed one holds
    first = _serve()
    try:
        port = first.server_address[1]
        with pytest.raises(OSError):
            old = SimpleXMLRPCServer(("127.0.0.1", port), logRequests=False)
            old.server_close()
    finally:
        first.shutdown()
        first.server_close()


def test_stop_then_start_rebinds_with_time_wait_present():
    first = _serve()
    port = first.server_address[1]
    for _ in range(5):  # server-side closes leave TIME_WAIT on the port
        assert xmlrpc.client.ServerProxy(f"http://127.0.0.1:{port}").ping() == "pong"
    first.shutdown()
    first.server_close()
    again = _serve(port)
    try:
        assert xmlrpc.client.ServerProxy(f"http://127.0.0.1:{port}").ping() == "pong"
    finally:
        again.shutdown()
        again.server_close()


def test_exclusive_flag_is_set_on_windows():
    server = _serve()
    try:
        if not hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            pytest.skip("SO_EXCLUSIVEADDRUSE is Windows-only")
        assert server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE) != 0
    finally:
        server.shutdown()
        server.server_close()
