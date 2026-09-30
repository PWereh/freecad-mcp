"""Live XML-RPC connection tracking, counted over the connection LIFETIME.

``ip_filter.verify_request`` sees only the accept and never the close, so the
count is taken in the request handler's ``setup()`` / ``finish()`` - the true
lifetime of one HTTP connection - with the peer port from
``client_address[1]``. ``rpc_server.start_rpc_server`` installs
``CountingRequestHandler`` by passing it to FilteredXMLRPCServer as
``requestHandler=``; ``ip_filter.py`` itself is unchanged.

Stdlib only, and deliberately no ``import FreeCAD``: the preserved module
``tests/test_rpc_concurrency.py`` imports ip_filter under a FreeCAD stub that
it withdraws immediately afterwards, so an import-time FreeCAD dependency here
would be a latent failure. Logging goes through ``set_logger`` instead, which
``rpc_server.py`` wires to FreeCAD.Console at start-up.

OPEN-3 (build blueprint s13, UNRESOLVED - a human must pick):
    THIS COUNT MAY NOT ANSWER THE QUESTION BC-06 ASKS, and nothing here claims
    that it does. ``SimpleXMLRPCRequestHandler`` inherits BaseHTTPRequestHandler's
    HTTP/1.0: the server closes after each response and the client reconnects
    per call, so the number of *currently open* connections is ~0 at idle even
    with a live, healthy bridge. BC-06's acceptance ("connected_clients == 0
    after the parent dies") therefore passes VACUOUSLY, and rxCAD's G2 halt
    ("exactly one live bridge", count > 1) is NOT measurable from this number.
    The blueprint offers three options - (a) this one, a concurrency gauge;
    (b) a distinct-peer-in-window counter; (c) a client ``hello(pid)``
    registration expired on EOF, the only option that genuinely measures bridge
    presence and the only one that adds an RPC method. It recommends (c) and
    explicitly leaves the choice to a human. Option (a) is built here as the
    floor because it is the baseline-preserving one: it adds no RPC method and
    changes no existing behaviour. ``tests/test_bridge_contract.py``
    TestBC06_Lifecycle records the gap as a skipped-as-failure test rather than
    a passing one.
"""

import threading
from typing import Any, Callable
from xmlrpc.server import SimpleXMLRPCRequestHandler


_lock = threading.Lock()
_open: dict[int, dict[str, Any]] = {}
_accepted_total = 0
_logger: Callable[[str], None] | None = None


def set_logger(logger: Callable[[str], None] | None) -> None:
    """Install the connect/disconnect log sink (FreeCAD.Console.PrintMessage)."""
    global _logger
    _logger = logger


def _log(message: str) -> None:
    if _logger is not None:
        try:
            _logger(message)
        except Exception:
            pass


def register(client_address: tuple) -> int:
    """Record one open connection; returns its key (the peer port)."""
    global _accepted_total
    host, port = (client_address + ("", 0))[:2] if client_address else ("", 0)
    key = id(client_address) if not isinstance(port, int) else port
    with _lock:
        _accepted_total += 1
        _open[key] = {"host": host, "port": port}
        live = len(_open)
    _log(f"MCP RPC: client connected from {host}:{port} (live connections: {live})\n")
    return key


def unregister(key: int) -> None:
    with _lock:
        info = _open.pop(key, None)
        live = len(_open)
    if info is not None:
        _log(
            f"MCP RPC: client disconnected from {info['host']}:{info['port']} "
            f"(live connections: {live})\n"
        )


def count() -> int:
    """Number of connections open RIGHT NOW. See OPEN-3 before reading meaning into it."""
    with _lock:
        return len(_open)


def peers() -> list[dict[str, Any]]:
    with _lock:
        return [dict(info) for info in _open.values()]


def accepted_total() -> int:
    """Connections accepted since start. Rises once per RPC call under HTTP/1.0."""
    with _lock:
        return _accepted_total


def reset() -> None:
    """Test helper; not part of the RPC surface."""
    global _accepted_total
    with _lock:
        _open.clear()
        _accepted_total = 0


class CountingRequestHandler(SimpleXMLRPCRequestHandler):
    """SimpleXMLRPCRequestHandler that counts a connection for its whole lifetime."""

    def setup(self) -> None:
        super().setup()
        self._mcp_connection_key = register(self.client_address)

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            key = getattr(self, "_mcp_connection_key", None)
            if key is not None:
                unregister(key)
