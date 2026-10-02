import logging
import os
import uuid
import xmlrpc.client

from typing import Any

from . import budgets, client_identity


logger = logging.getLogger("FreeCADMCPserver")

BRIDGE_CONTRACT = "v0.2.0"


class StaleAddonError(RuntimeError):
    """Raised at connect when the addon does not speak this client's contract.

    BC-08's canary. A v0.2.0 client against a v0.1.23 addon is refused HERE,
    deliberately and with both versions named, because it would otherwise call
    ``execute_code(code, timeout)`` with two arguments and get an opaque
    xmlrpc Fault. The converse is fine: a v0.1.23 client against a v0.2.0 addon
    keeps working, which is why the human swap order is ADDON FIRST.
    """


# This client process's bridge identity, minted ONCE. It is what makes a
# reconnect within one process supersede its own earlier hello() instead of
# counting as a second bridge - see the addon's rpc_server/bridges.py. The pid
# alone would do for that, but an instance token also keeps the identity honest
# if a pid is ever recycled or shared.
BRIDGE_INSTANCE = uuid.uuid4().hex

# Fallback only. The live value is REPORTED by the addon in get_rpc_status
# ("bridge_header"), so the wire constant has one source of truth.
BRIDGE_HEADER_DEFAULT = "X-FreeCAD-MCP-Bridge"


class _TimeoutTransport(xmlrpc.client.Transport):
    """XML-RPC transport with a configurable socket timeout.

    The default Transport has no timeout, so a frozen FreeCAD GUI thread
    causes the MCP client to hang indefinitely (observed: 4+ minute waits).
    """
    def __init__(self, timeout: float = 30, bridge_header: str | None = None,
                 bridge_token: str | None = None, **kwargs):
        super().__init__(**kwargs)
        self._timeout = timeout
        self._bridge_header = bridge_header
        self._bridge_token = bridge_token

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn

    def get_host_info(self, host):
        """Stamp the bridge token on EVERY request.

        Renewal rides on the transport rather than on any individual call, so a
        new tool added later cannot forget to renew and no method signature
        carries a token. The addon reads it in BridgeAwareRequestHandler.
        """
        host, extra_headers, x509 = super().get_host_info(host)
        if self._bridge_header and self._bridge_token:
            extra_headers = list(extra_headers or [])
            extra_headers.append((self._bridge_header, self._bridge_token))
        return host, extra_headers, x509



class FreeCADConnection:
    """XML-RPC client for the FreeCAD addon on loopback.

    The socket timeout for every call is DERIVED from the budget table the
    addon reports (``budgets.socket_for``), enveloped by the configured
    ``timeout`` so an explicitly long-lived connection is never shortened. The
    literals ``EXECUTE_CODE_TIMEOUT = 90`` and ``RPC_TIMEOUT_MARGIN = 30`` and
    the two ``2 * X + margin`` expressions they fed are gone; M now lives once,
    in ``budgets.M``.

    Constructing a connection performs NO RPC. The connect-time assertion lives
    in ``server.get_freecad_connection()``, so the baseline's "start even when
    FreeCAD is not running" behaviour survives and a stale addon is refused per
    call rather than by refusing to boot.
    """

    def __init__(self, host: str = "localhost", port: int = 9875, timeout: float = 150):
        self._uri = f"http://{host}:{port}"
        self._timeout = timeout
        # Until the connect sequence has read the real table, derive from the
        # baseline one. See budgets.BASELINE_TABLE for why that is a fallback
        # and not a second copy.
        self._budgets: dict[str, dict[str, float]] = {
            tool: dict(entry) for tool, entry in budgets.BASELINE_TABLE.items()
        }
        self.bridge_contract: str | None = None
        self.jobs_dir: str | None = None
        # OPEN-3 option (c). Set by hello(); until then this client is simply
        # not registered and does not appear in live_bridges.
        self.bridge_token: str | None = None
        self.bridge_lease_s: float | None = None
        self._bridge_header: str = BRIDGE_HEADER_DEFAULT
        self.server = self._make_proxy(timeout)

    def _make_proxy(self, timeout: float) -> xmlrpc.client.ServerProxy:
        return xmlrpc.client.ServerProxy(
            self._uri,
            allow_none=True,
            transport=_TimeoutTransport(
                timeout=timeout,
                bridge_header=self._bridge_header,
                bridge_token=self.bridge_token,
            ),
        )

    # ---------------------------------------------------------------- budgets

    @property
    def budgets(self) -> dict[str, dict[str, float]]:
        """The addon's table as last reported. One copy, read-only here."""
        return {tool: dict(entry) for tool, entry in self._budgets.items()}

    def socket_for(self, tool: str, R_override: float | None = None) -> float:
        """The socket timeout for one call: max(configured, Q + R + M).

        THE ENVELOPE IS LOAD-BEARING and is applied HERE, not inside
        ``budgets.socket_for``, which stays pure - see OPEN-2 in budgets.py.
        """
        return max(self._timeout, budgets.socket_for(self._budgets, tool, R_override))

    def apply_status(self, status: dict[str, Any]) -> dict[str, Any]:
        """Adopt the addon's reported table, contract version and jobs_dir.

        Raises StaleAddonError when the addon is older than this client.
        """
        contract = status.get("bridge_contract") if isinstance(status, dict) else None
        table = status.get("budgets") if isinstance(status, dict) else None
        if contract != BRIDGE_CONTRACT or table is None:
            raise StaleAddonError(
                f"FreeCAD addon reports bridge_contract={contract!r}; this client "
                f"is {BRIDGE_CONTRACT} and requires a matching addon. Install "
                f"addon/FreeCADMCP into %APPDATA%\\FreeCAD\\v1-1\\Mod\\FreeCADMCP "
                f"and restart FreeCAD (addon first, then the client)."
            )
        budgets.assert_table(table)
        self._budgets = {tool: dict(entry) for tool, entry in table.items()}
        self.bridge_contract = contract
        self.jobs_dir = status.get("jobs_dir")
        self._bridge_header = status.get("bridge_header") or BRIDGE_HEADER_DEFAULT
        # Rebuild the persistent proxy at the derived GUI-default socket.
        self._timeout = max(self._timeout, budgets.socket_for(self._budgets, "gui_default"))
        self.server = self._make_proxy(self._timeout)
        return status

    # ------------------------------------------------------------- lifecycle

    def disconnect(self) -> None:
        # Transport.close() clears cached HTTP connections if one was opened.
        transport = getattr(self.server, "_ServerProxy__transport", None)
        close = getattr(transport, "close", None)
        if callable(close):
            close()

    # ------------------------------------------------- OPEN-3: bridge presence

    def hello(self, lease_s: float | None = None) -> dict[str, Any]:
        """Register this bridge, then stamp the token on every later request.

        Called once per connect, AFTER apply_status, because the header name
        comes from the status echo. The pid is passed for diagnostics and
        identity; the addon never asks the operating system whether it is alive.
        """
        client = client_identity.client_pid()
        with self._make_proxy(self._timeout) as proxy:
            try:
                reply = proxy.hello(os.getpid(), BRIDGE_INSTANCE, lease_s, BRIDGE_CONTRACT, client)
            except xmlrpc.client.Fault:
                # An addon older than v0.3.1 takes four arguments. Register the old way rather
                # than not at all - an unregistered bridge is invisible to G2.
                reply = proxy.hello(os.getpid(), BRIDGE_INSTANCE, lease_s, BRIDGE_CONTRACT)
        if isinstance(reply, dict) and reply.get("token"):
            self.bridge_token = reply["token"]
            self.bridge_lease_s = reply.get("lease_s")
            # Rebuild so the persistent proxy carries the header from now on.
            self.server = self._make_proxy(self._timeout)
        return reply if isinstance(reply, dict) else {}

    def heartbeat(self) -> bool:
        """Keep an IDLE bridge live. A busy one renews through the header."""
        if not self.bridge_token:
            return False
        with self._make_proxy(self._timeout) as proxy:
            reply = proxy.heartbeat(self.bridge_token)
        return bool(isinstance(reply, dict) and reply.get("renewed"))

    def goodbye(self) -> bool:
        """Deregister on clean shutdown. Never raises: this is a shutdown path.

        A hard kill never reaches here, which is exactly what the lease is for.
        """
        token, self.bridge_token = self.bridge_token, None
        if not token:
            return False
        try:
            with self._make_proxy(self._timeout) as proxy:
                reply = proxy.goodbye(token)
            return bool(isinstance(reply, dict) and reply.get("found"))
        except Exception:
            return False

    def live_bridges(self) -> dict[str, Any]:
        with self._make_proxy(self._timeout) as proxy:
            return proxy.live_bridges()

    def ping(self) -> bool:
        return self.server.ping()

    def get_rpc_status(self) -> dict[str, Any]:
        with self._make_proxy(self._timeout) as proxy:
            return proxy.get_rpc_status()

    # ----------------------------------------------------------- GUI tools

    def create_document(self, name: str) -> dict[str, Any]:
        return self.server.create_document(name)

    def create_object(self, doc_name: str, obj_data: dict[str, Any]) -> dict[str, Any]:
        return self.server.create_object(doc_name, obj_data)

    def edit_object(self, doc_name: str, obj_name: str, obj_data: dict[str, Any]) -> dict[str, Any]:
        return self.server.edit_object(doc_name, obj_name, obj_data)

    def delete_object(self, doc_name: str, obj_name: str) -> dict[str, Any]:
        return self.server.delete_object(doc_name, obj_name)


    def reload_document(self, doc_name: str) -> dict[str, Any]:
        return self.server.reload_document(doc_name)

    def insert_part_from_library(self, relative_path: str) -> dict[str, Any]:
        return self.server.insert_part_from_library(relative_path)

    def execute_code(self, code: str, timeout: float | None = None) -> dict[str, Any]:
        # The addon permits a full queue budget followed by a full run budget;
        # the socket must outlast both plus the processing margin, or the
        # structured error never reaches us and we learn nothing from a bare
        # transport timeout.
        socket_timeout = self.socket_for("execute_code", timeout)
        with self._make_proxy(socket_timeout) as proxy:
            # One argument when no per-call timeout was asked for, so the wire
            # shape is byte-identical to v0.1.23's.
            if timeout is None:
                return proxy.execute_code(code)
            return proxy.execute_code(code, timeout)

    def execute_code_async(self, code: str) -> dict[str, Any]:
        return self.server.execute_code_async(code)

    def get_async_status(self, job_id: str = "") -> dict[str, Any]:
        # Polling must not share an HTTP connection with a blocked GUI request.
        with self._make_proxy(self._timeout) as proxy:
            return proxy.get_async_status(job_id)

    def get_active_screenshot(
        self,
        view_name: str = "Isometric",
        width: int | None = None,
        height: int | None = None,
        focus_object: str | None = None,
    ) -> str | None:
        try:
            return self.server.get_active_screenshot(view_name, width, height, focus_object)
        except Exception as e:
            logger.error(f"Error getting screenshot: {e}")
            return None

    def get_objects(self, doc_name: str) -> list[dict[str, Any]]:
        return self.server.get_objects(doc_name)

    def get_object(self, doc_name: str, obj_name: str) -> dict[str, Any]:
        return self.server.get_object(doc_name, obj_name)

    def get_parts_list(self) -> list[str]:
        return self.server.get_parts_list()

    def list_documents(self) -> list[str]:
        return self.server.list_documents()

    def run_fem_analysis(self, doc_name: str, analysis_name: str, timeout: int = 600) -> dict[str, Any]:
        # Both queueing and solving can consume `timeout` seconds each.
        socket_timeout = self.socket_for("fem", timeout)
        with self._make_proxy(socket_timeout) as proxy:
            return proxy.run_fem_analysis(doc_name, analysis_name, timeout)

    # --------------------------------------------------- v0.2.0 health tools

    def gui_ping(self, cap: float = 5.0) -> dict[str, Any]:
        """BC-04 probe. Own proxy: it must answer while another call is blocked.

        The socket is derived from the probe budget, so even the structural
        worst case (cap queued + cap run) arrives as a STRUCTURED result rather
        than a bare socket timeout.
        """
        with self._make_proxy(self.socket_for("probe", cap)) as proxy:
            return proxy.gui_ping(cap)

    def reset_dispatch_health(self, force: bool = False) -> dict[str, Any]:
        """BC-05 bounded reset. Own proxy, for the same reason as gui_ping."""
        with self._make_proxy(self.socket_for("probe")) as proxy:
            return proxy.reset_dispatch_health(force)

    def set_gui_budget(self, tool: str, R: float, Q: float | None = None) -> dict[str, Any]:
        """BC-02 configuration RPC. Re-derives from the echoed table on success."""
        with self._make_proxy(self._timeout) as proxy:
            result = proxy.set_gui_budget(tool, R, Q)
        if isinstance(result, dict) and result.get("success") and result.get("budgets"):
            try:
                budgets.assert_table(result["budgets"])
                self._budgets = {t: dict(e) for t, e in result["budgets"].items()}
            except ValueError as exc:
                logger.error(f"addon echoed an unusable budget table: {exc}")
        return result
