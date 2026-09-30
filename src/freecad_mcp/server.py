import logging
import os
import signal
import threading
import sys
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Literal

try:
    # mcp 1.x
    from mcp.server.fastmcp import Context, FastMCP
except ImportError:
    # mcp 2.x moved mcp.server.fastmcp to mcp.server.mcpserver and renamed
    # FastMCP to MCPServer; the API surface used here is unchanged.
    from mcp.server.mcpserver import Context
    from mcp.server.mcpserver import MCPServer as FastMCP
from mcp.types import ImageContent, TextContent

from .freecad_client import FreeCADConnection, StaleAddonError
from .operations import (
    create_document_operation,
    create_object_operation,
    delete_object_operation,
    edit_object_operation,
    execute_code_async_operation,
    execute_code_headless_operation,
    execute_code_operation,
    get_object_operation,
    get_objects_operation,
    get_parts_list_operation,
    get_async_status_operation,
    get_rpc_status_operation,
    get_view_operation,
    gui_ping_operation,
    insert_part_from_library_operation,
    list_documents_operation,
    reload_document_operation,
    reset_dispatch_health_operation,
    run_fem_analysis_operation,
    set_gui_budget_operation,
)
from .payload_screen import screen
from .prompt_text import ASSET_CREATION_STRATEGY
from .responses import json_response
from .server_state import ServerState


logging.basicConfig(
    level=logging.WARNING, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("FreeCADMCPserver")
logger.setLevel(logging.INFO)

ViewName = Literal[
    "Isometric", "Front", "Top", "Right", "Back", "Left", "Bottom", "Dimetric", "Trimetric"
]

state = ServerState()


@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    try:
        logger.info("FreeCADMCP server starting up")
        try:
            _ = get_freecad_connection()
            logger.info("Successfully connected to FreeCAD on startup")
        except Exception as e:
            logger.warning(f"Could not connect to FreeCAD on startup: {str(e)}")
            logger.warning(
                "Make sure the FreeCAD addon is running before using FreeCAD resources or tools"
            )
        yield {}
    finally:
        close_bridge("lifespan shutdown")
        logger.info("FreeCADMCP server shut down")


mcp = FastMCP(
    "FreeCADMCP",
    instructions="FreeCAD integration through the Model Context Protocol",
    lifespan=server_lifespan,
)


def get_freecad_connection() -> FreeCADConnection:
    """Get or create a persistent FreeCAD connection.

    Connect sequence, ordered: construct at the literal default timeout ->
    ping() -> get_rpc_status() -> assert the budget table is well-formed and
    bridge_contract == "v0.2.0" -> store the table and jobs_dir -> rebuild the
    persistent proxy at the derived gui_default socket.

    THE ASSERTION LIVES HERE, NOT AT PROCESS START, so the baseline's "start
    even when FreeCAD is not running" behaviour survives: a stale addon is
    refused per call, with both versions named, rather than by refusing to boot.
    """
    if state.freecad_connection is None:
        connection = FreeCADConnection(host=state.rpc_host, port=state.rpc_port)
        if not connection.ping():
            logger.error("Failed to ping FreeCAD")
            raise Exception(
                "Failed to connect to FreeCAD. Make sure the FreeCAD addon is running."
            )
        connection.apply_status(connection.get_rpc_status())
        state.budgets = connection.budgets
        state.bridge_contract = connection.bridge_contract
        state.jobs_dir = connection.jobs_dir
        state.freecad_connection = connection
        try:
            reply = connection.hello()
            if reply.get("live_bridges", 0) > 1:
                # Reported, never enforced here. rxCAD's G2 owns the halt; the
                # bridge's job is to make the number measurable and say so.
                logger.warning(
                    f"{reply['live_bridges']} live bridges are registered with this "
                    f"FreeCAD. rxCAD G2 expects exactly one; call live_bridges() "
                    f"for pids and ages."
                )
            _start_heartbeat(connection)
        except Exception as exc:
            # A v0.2.0 addon without hello() must still serve every other tool.
            logger.warning(f"Bridge registration unavailable: {exc}")
        logger.info(
            f"Connected to FreeCAD addon {connection.bridge_contract}; "
            f"budgets {connection.budgets}; jobs_dir {connection.jobs_dir}"
        )
    return state.freecad_connection


# --- OPEN-3 option (c): keep an IDLE bridge's lease fresh --------------------
# A busy bridge renews through the header on every call. An idle one makes no
# calls, so without this its lease expires and live_bridges reads 0 while the
# bridge is demonstrably alive. UNDER-counting is the dangerous direction for
# rxCAD's G2 halt: it fails to halt when a second bridge really is there.
#
# daemon=True IS DELIBERATE AND LOAD-BEARING. OPEN-13 measured three orphaned
# freecad-mcp.exe and names "a non-daemon thread held the process up" as one of
# the two candidate causes. This thread must never be the third orphan: it is a
# daemon, it waits on an Event so shutdown does not wait out an interval, it
# uses its own short-lived proxy rather than the shared one, and it returns on
# the first failure instead of retrying - a bridge that cannot heartbeat should
# expire, not be propped up by a thread that outlives it.
_heartbeat_stop = threading.Event()
_heartbeat_thread: threading.Thread | None = None


def _start_heartbeat(connection) -> None:
    global _heartbeat_thread
    if _heartbeat_thread is not None and _heartbeat_thread.is_alive():
        return
    lease = connection.bridge_lease_s or 120.0
    interval = max(5.0, float(lease) / 3.0)
    _heartbeat_stop.clear()

    def _loop() -> None:
        while not _heartbeat_stop.wait(interval):
            conn = state.freecad_connection
            if conn is None or not conn.bridge_token:
                return
            try:
                conn.heartbeat()
            except Exception as exc:
                logger.debug(f"Bridge heartbeat stopped: {exc}")
                return

    _heartbeat_thread = threading.Thread(
        target=_loop, name="freecad-mcp-heartbeat", daemon=True
    )
    _heartbeat_thread.start()
    logger.info(
        f"Bridge registered (lease {lease:.0f}s); heartbeat every {interval:.0f}s"
    )


def close_bridge(reason: str) -> None:
    """BC-06: close the XML-RPC proxy. Idempotent, never raises.

    Called on stdin EOF (the parent is gone), on SIGTERM/SIGINT/SIGBREAK, and
    from the lifespan's finally. Measured at 5dbfe2c: three orphaned
    freecad-mcp.exe after three runs, each holding or half-closing a socket on
    9875 while its FreeCAD lived.

    IN-FLIGHT ``execute_code_headless`` CHILDREN ARE NOT CANCELLED HERE, and
    nothing in this file claims they are. Cancelling them needs a process
    registry inside headless.py, which the design marks UNCHANGED and whose
    launch path (subprocess.run with a timeout) would have to be re-authored to
    obtain the handle - a change to a preserved module, for a mechanism OPEN-13
    says must be MEASURED before more machinery is added. A headless child stays
    bounded by its own timeout (600 s default) and is a separate process
    (freecadcmd), not the freecad-mcp.exe that BC-06 counts.
    """
    connection = state.freecad_connection
    state.freecad_connection = None
    _heartbeat_stop.set()
    if connection is None:
        return
    logger.info(f"Closing the FreeCAD XML-RPC connection: {reason}")
    # Deregister BEFORE dropping the socket, so live_bridges falls to 0 at once
    # on a clean exit. A hard kill never reaches here; that is what the lease is
    # for, and the two paths are deliberately different mechanisms.
    #
    # IN ITS OWN try, NOT SHARING ONE WITH disconnect(). Written as a single
    # block first, and tests/test_bridge_contract.py caught it: a connection
    # without goodbye() raised AttributeError and the socket was then NEVER
    # CLOSED. Deregistration is best-effort bookkeeping; closing the socket is
    # the actual job of BC-06, and a failure in the first must not be able to
    # skip the second. The lease covers a goodbye that never lands.
    try:
        goodbye = getattr(connection, "goodbye", None)
        if callable(goodbye):
            goodbye()
    except Exception as exc:
        logger.debug(f"Bridge deregistration failed (the lease will expire): {exc}")
    try:
        connection.disconnect()
    except Exception as exc:  # a shutdown path must not raise
        logger.warning(f"Error while closing the FreeCAD connection: {exc}")


def _install_signal_handlers() -> None:
    """BC-06: SIGTERM (and SIGINT/SIGBREAK) shut down the same way EOF does.

    OPEN-13 (build blueprint s13, UNRESOLVED - MEASURE BEFORE ADDING MORE):
        The design says "stdin EOF is shutdown", and under the SDK's stdio
        transport EOF already ends the read loop - yet THREE ORPHANS WERE
        MEASURED. So either the pipe's write end stayed open in a sibling
        process, or a non-daemon thread held the process up. SIGTERM is also not
        delivered the POSIX way on Windows. The specified mechanism is built
        here - explicit EOF shutdown, signal handlers, an explicit exit - and
        left MEASURABLE. A parent-liveness watchdog (ctypes OpenProcess +
        WaitForSingleObject) is deliberately NOT built: it is a second shutdown
        authority whose failure mode is killing a live server, and it comes only
        if orphans survive this.
    """
    def _handler(signum, _frame):
        close_bridge(f"signal {signum}")
        # A signal handler must not return into the SDK's read loop expecting a
        # graceful unwind; exit immediately and deterministically.
        os._exit(0)

    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError, RuntimeError):
            # Not the main thread, or unsupported on this platform.
            logger.debug(f"Could not install a handler for {name}")


@mcp.tool(structured_output=False)
def create_document(ctx: Context, name: str) -> list[TextContent]:
    """Create a new document in FreeCAD.

    Args:
        name: The name of the document to create.

    Returns:
        A message indicating the success or failure of the document creation.

    Examples:
        If you want to create a document named "MyDocument", you can use the following data.
        ```json
        {
            "name": "MyDocument"
        }
        ```
    """
    return create_document_operation(get_freecad_connection(), name)


@mcp.tool(structured_output=False)
def create_object(
    ctx: Context,
    doc_name: str,
    obj_type: str,
    obj_name: str,
    analysis_name: str | None = None,
    obj_properties: dict[str, Any] = None,
    include_screenshot: bool = True,
    view_name: ViewName = "Isometric",
) -> list[TextContent | ImageContent]:
    """Create a new object in FreeCAD.
    Object type is starts with "Part::" or "Draft::" or "PartDesign::" or "Fem::".

    Args:
        doc_name: The name of the document to create the object in.
        obj_type: The type of the object to create (e.g. 'Part::Box', 'Part::Cylinder', 'Draft::Circle', 'PartDesign::Body', etc.).
        obj_name: The name of the object to create.
        obj_properties: The properties of the object to create.
        include_screenshot: Whether to return a screenshot of the model (default True).
            Set to False to save tokens when visual feedback is not needed,
            e.g. for intermediate steps in a longer sequence of changes.
        view_name: The view orientation of the returned screenshot (default "Isometric").
            Pick the view that best shows the change being made.

    Returns:
        A message indicating the success or failure of the object creation and a screenshot of the object.

    Examples:
        If you want to create a cylinder with a height of 30 and a radius of 10, you can use the following data.
        ```json
        {
            "doc_name": "MyCylinder",
            "obj_name": "Cylinder",
            "obj_type": "Part::Cylinder",
            "obj_properties": {
                "Height": 30,
                "Radius": 10,
                "Placement": {
                    "Base": {
                        "x": 10,
                        "y": 10,
                        "z": 0
                    },
                    "Rotation": {
                        "Axis": {
                            "x": 0,
                            "y": 0,
                            "z": 1
                        },
                        "Angle": 45
                    }
                },
                "ViewObject": {
                    "ShapeColor": [0.5, 0.5, 0.5, 1.0]
                }
            }
        }
        ```

        If you want to create a circle with a radius of 10, you can use the following data.
        ```json
        {
            "doc_name": "MyCircle",
            "obj_name": "Circle",
            "obj_type": "Draft::Circle",
        }
        ```

        If you want to create a FEM analysis, you can use the following data.
        ```json
        {
            "doc_name": "MyFEMAnalysis",
            "obj_name": "FemAnalysis",
            "obj_type": "Fem::AnalysisPython",
        }
        ```

        If you want to create a FEM constraint, you can use the following data.
        ```json
        {
            "doc_name": "MyFEMConstraint",
            "obj_name": "FemConstraint",
            "obj_type": "Fem::ConstraintFixed",
            "analysis_name": "MyFEMAnalysis",
            "obj_properties": {
                "References": [
                    {
                        "object_name": "MyObject",
                        "face": "Face1"
                    }
                ]
            }
        }
        ```

        If you want to create a FEM mechanical material, you can use the following data.
        ```json
        {
            "doc_name": "MyFEMAnalysis",
            "obj_name": "FemMechanicalMaterial",
            "obj_type": "Fem::MaterialCommon",
            "analysis_name": "MyFEMAnalysis",
            "obj_properties": {
                "Material": {
                    "Name": "MyMaterial",
                    "Density": "7900 kg/m^3",
                    "YoungModulus": "210 GPa",
                    "PoissonRatio": 0.3
                }
            }
        }
        ```

        If you want to create a FEM mesh, you can use the following data.
        The `Shape` property is required (legacy `Part` is also accepted).
        On FreeCAD 1.x the size limits are `CharacteristicLengthMax/Min`;
        the legacy `ElementSizeMax/Min` keys are also accepted.
        ```json
        {
            "doc_name": "MyFEMMesh",
            "obj_name": "FemMesh",
            "obj_type": "Fem::FemMeshGmsh",
            "analysis_name": "MyFEMAnalysis",
            "obj_properties": {
                "Shape": "MyObject",
                "CharacteristicLengthMax": 10,
                "CharacteristicLengthMin": 0.1
            }
        }
        ```
    """
    return create_object_operation(
        get_freecad_connection(),
        state.only_text_feedback,
        doc_name,
        obj_type,
        obj_name,
        analysis_name,
        obj_properties,
        include_screenshot,
        view_name,
    )


@mcp.tool(structured_output=False)
def edit_object(
    ctx: Context,
    doc_name: str,
    obj_name: str,
    obj_properties: dict[str, Any],
    include_screenshot: bool = True,
    view_name: ViewName = "Isometric",
) -> list[TextContent | ImageContent]:
    """Edit an object in FreeCAD.
    This tool is used when the `create_object` tool cannot handle the object creation.

    Args:
        doc_name: The name of the document to edit the object in.
        obj_name: The name of the object to edit.
        obj_properties: The properties of the object to edit.
        include_screenshot: Whether to return a screenshot of the model (default True).
            Set to False to save tokens when visual feedback is not needed,
            e.g. for intermediate steps in a longer sequence of changes.
        view_name: The view orientation of the returned screenshot (default "Isometric").
            Pick the view that best shows the change being made.

    Returns:
        A message indicating the success or failure of the object editing and a screenshot of the object.
    """
    return edit_object_operation(
        get_freecad_connection(),
        state.only_text_feedback,
        doc_name,
        obj_name,
        obj_properties,
        include_screenshot,
        view_name,
    )


@mcp.tool(structured_output=False)
def delete_object(
    ctx: Context,
    doc_name: str,
    obj_name: str,
    include_screenshot: bool = True,
    view_name: ViewName = "Isometric",
) -> list[TextContent | ImageContent]:
    """Delete an object in FreeCAD.

    Args:
        doc_name: The name of the document to delete the object from.
        obj_name: The name of the object to delete.
        include_screenshot: Whether to return a screenshot of the model (default True).
            Set to False to save tokens when visual feedback is not needed,
            e.g. for intermediate steps in a longer sequence of changes.
        view_name: The view orientation of the returned screenshot (default "Isometric").
            Pick the view that best shows the change being made.

    Returns:
        A message indicating the success or failure of the object deletion and a screenshot of the object.
    """
    return delete_object_operation(
        get_freecad_connection(),
        state.only_text_feedback,
        doc_name,
        obj_name,
        include_screenshot,
        view_name,
    )


@mcp.tool(structured_output=False)
def execute_code_async(ctx: Context, code: str) -> list[TextContent]:
    """Execute Python code in FreeCAD without waiting for completion.

    Use this ONLY for long-running background computations that do NOT touch the
    FreeCAD GUI or mutate the FreeCAD document tree directly.

    This tool runs the submitted code in a background thread and returns
    immediately. Because it does not run on FreeCAD's main GUI thread, the code
    must NOT directly call FreeCADGui APIs, manipulate the active view or
    selection, create or edit document objects, change object properties, call
    doc.recompute(), or save documents. FreeCAD documents and the Coin3D
    scenegraph are not thread-safe: writing to them from this thread races the
    GUI thread and can wedge FreeCAD's event loop, after which the RPC server
    stops responding entirely and FreeCAD must be restarted.

    Every document or view write must instead be handed to the GUI thread through
    the injected commit() helper:

        commit(fn, timeout=120) -> fn's return value

    Scripts share a live namespace. Saved functions can use commit() in later
    async calls; calling it from execute_code or a GUI callback raises immediately.
    Coordinate concurrent scripts that intentionally modify the same variables.

    commit() queues fn on the GUI thread, waits for it, and raises RuntimeError if
    dispatch fails or times out. Example:

        fused = base.fuse(addition).removeSplitter()   # slow, safe in background

        def apply():                                   # runs on the GUI thread
            obj.Shape = fused
            doc.recompute()

        commit(apply)

    For code that is not dominated by heavy geometry computation, use execute_code
    instead. execute_code runs entirely on the FreeCAD GUI thread and is the safe
    default for normal FreeCAD automation.

    Use execute_code_async only when the heavy part is long-running OCCT geometry
    (e.g. fuse/cut/loft on already-fetched shapes) or other CPU-bound computation
    that would exceed execute_code's 90 s GUI-thread budget.

    Typical usage pattern:
    1. Fetch shapes into module-level variables first (via execute_code).
    2. Run the heavy computation via execute_code_async.
    3. Apply the result inside commit(), or store it in a module-level Python
       variable (not in the FreeCAD document) for a later execute_code call.

    Performance note: boolean operations against shapes with many faces (e.g. a
    ribbed lid) are expensive. Fuse the additions together first, then apply a
    single boolean against the heavy shape, and avoid doc.recompute() unless the
    dependency graph really needs it.

    Args:
        code: Background-safe Python code to execute. Use commit(fn) for all
            document and view writes.

    Returns:
        A message with the job_id of the started background execution.
    """
    illegal = screen(code)
    if illegal is not None:
        return json_response(illegal)
    return execute_code_async_operation(get_freecad_connection(), code)


@mcp.tool(structured_output=False)
def execute_code_headless(ctx: Context, code: str, timeout: float = 600) -> list[TextContent]:
    """Run a FreeCAD Python script in a separate headless `freecadcmd` process.

    Use this for OCCT work that can crash or block FreeCAD: helical threads
    (makeHelix + makePipeShell), lofts and sweeps, booleans with many or
    B-spline tools, long parametric rebuilds. A native OpenCascade crash
    here only kills the helper process; the GUI and its open documents
    survive, and the tool reports the crash signal and the script's output.

    The script runs on the MCP server machine, independently of --host, in a
    fresh process without GUI: import FreeCAD/Part
    yourself, open documents from disk (FreeCAD.openDocument(path)), save
    results with doc.save()/saveAs() or Shape.exportBrep(). Nothing from the
    execute_code namespace is available. Print progress to stdout; it is
    returned when the process ends. After the script saved a .FCStd that is
    open in the GUI, call reload_document(doc_name) to show the result.

    Args:
        code: Complete Python script for freecadcmd.
        timeout: Positive finite seconds to wait before killing the process
            (default 600). Partial output is preserved on timeout.

    Returns:
        Exit status, crash/timeout diagnosis and the script's printed output.
    """
    illegal = screen(code)
    if illegal is not None:
        return json_response(illegal)
    return execute_code_headless_operation(state.freecadcmd, code, timeout)


@mcp.tool(structured_output=False)
def get_async_status(ctx: Context, job_id: str = "") -> list[TextContent]:
    """Report the state of background jobs started by execute_code_async.

    Does not use the FreeCAD GUI thread, so it answers even while a job runs.

    Args:
        job_id: The id returned by execute_code_async. Empty lists all running
            jobs and up to 20 recently completed jobs.

    Returns:
        For one job: its state (running/done/failed), the error and traceback
        when it failed. History is held in memory until FreeCAD exits.
    """
    return get_async_status_operation(get_freecad_connection(), job_id)


@mcp.tool(structured_output=False)
def execute_code(
    ctx: Context,
    code: str,
    include_screenshot: bool = True,
    view_name: ViewName = "Isometric",
    timeout: float | None = None,
) -> list[TextContent | ImageContent]:
    """Execute arbitrary Python code in FreeCAD.

    Runs on FreeCAD's GUI thread and waits for the result. This is the safe
    default for all document automation.

    Args:
        code: The Python code to execute.
        include_screenshot: Whether to return a screenshot of the model (default True).
            Set to False to save tokens when the code does not change the model's
            appearance, e.g. analytical or computational scripts whose result is
            printed output, or intermediate steps in a longer sequence of changes.
        view_name: The view orientation of the returned screenshot (default "Isometric").
            Pick the view that best shows the change being made.
        timeout: Optional per-call GUI run budget in seconds. May request LESS
            than the server's configured budget for execute_code; requesting
            MORE is refused with the cap named, never silently clamped. Omit it
            to use the server's budget.

    Returns:
        A message indicating the success or failure of the code execution, the output of the code execution, and a screenshot of the object.
    """
    illegal = screen(code)
    if illegal is not None:
        return json_response(illegal)
    return execute_code_operation(
        get_freecad_connection(),
        state.only_text_feedback,
        code,
        include_screenshot,
        view_name,
        timeout,
    )


@mcp.tool(structured_output=False)
def get_view(
    ctx: Context,
    view_name: ViewName,
    width: int | None = None,
    height: int | None = None,
    focus_object: str | None = None,
) -> list[ImageContent | TextContent]:
    """Get a screenshot of the active view.

    Args:
        view_name: The name of the view to get the screenshot of.
        The following views are available:
        - "Isometric"
        - "Front"
        - "Top"
        - "Right"
        - "Back"
        - "Left"
        - "Bottom"
        - "Dimetric"
        - "Trimetric"
        width: The width of the screenshot in pixels. If not specified, uses the viewport width.
        height: The height of the screenshot in pixels. If not specified, uses the viewport height.
        focus_object: The name of the object to focus on. If not specified, fits all objects in the view.

    Returns:
        A screenshot of the active view.
    """
    return get_view_operation(get_freecad_connection(), view_name, width, height, focus_object)


@mcp.tool(structured_output=False)
def insert_part_from_library(
    ctx: Context,
    relative_path: str,
    include_screenshot: bool = True,
    view_name: ViewName = "Isometric",
) -> list[TextContent | ImageContent]:
    """Insert a part from the parts library addon.

    Args:
        relative_path: The relative path of the part to insert.
        include_screenshot: Whether to return a screenshot of the model (default True).
            Set to False to save tokens when visual feedback is not needed,
            e.g. for intermediate steps in a longer sequence of changes.
        view_name: The view orientation of the returned screenshot (default "Isometric").
            Pick the view that best shows the change being made.

    Returns:
        A message indicating the success or failure of the part insertion and a screenshot of the object.
    """
    return insert_part_from_library_operation(
        get_freecad_connection(),
        state.only_text_feedback,
        relative_path,
        include_screenshot,
        view_name,
    )


@mcp.tool(structured_output=False)
def get_objects(
    ctx: Context,
    doc_name: str,
    include_screenshot: bool = True,
    view_name: ViewName = "Isometric",
) -> list[TextContent | ImageContent]:
    """Get all objects in a document.
    You can use this tool to get the objects in a document to see what you can check or edit.

    Args:
        doc_name: The name of the document to get the objects from.
        include_screenshot: Whether to return a screenshot of the document (default True).
            Set to False to save tokens when only the object data is needed.
        view_name: The view orientation of the returned screenshot (default "Isometric").

    Returns:
        A list of objects in the document and a screenshot of the document.
    """
    return get_objects_operation(
        get_freecad_connection(),
        state.only_text_feedback,
        doc_name,
        include_screenshot,
        view_name,
    )


@mcp.tool(structured_output=False)
def get_object(
    ctx: Context,
    doc_name: str,
    obj_name: str,
    include_screenshot: bool = True,
    view_name: ViewName = "Isometric",
) -> list[TextContent | ImageContent]:
    """Get an object from a document.
    You can use this tool to get the properties of an object to see what you can check or edit.

    Args:
        doc_name: The name of the document to get the object from.
        obj_name: The name of the object to get.
        include_screenshot: Whether to return a screenshot of the document (default True).
            Set to False to save tokens when only the object data is needed.
        view_name: The view orientation of the returned screenshot (default "Isometric").

    Returns:
        The object and a screenshot of the object.
    """
    return get_object_operation(
        get_freecad_connection(),
        state.only_text_feedback,
        doc_name,
        obj_name,
        include_screenshot,
        view_name,
    )


@mcp.tool(structured_output=False)
def get_parts_list(ctx: Context) -> list[TextContent]:
    """Get the list of parts in the parts library addon.
    """
    return get_parts_list_operation(get_freecad_connection())


@mcp.tool(structured_output=False)
def reload_document(ctx: Context, doc_name: str) -> list[TextContent]:
    """Close and re-open a document to pick up external file changes.

    Use this AFTER the document's .FCStd file has been modified by
    something outside of FreeCAD's GUI process — for example, a
    headless `freecadcmd` script that edited and saved the file. The
    open GUI document is otherwise unaware of on-disk changes; this
    tool closes the stale in-memory copy and reopens the file from
    disk so the GUI shows current geometry.

    Args:
        doc_name: The name of the open document to reload. Must match
            the name shown by ``list_documents``.

    Returns:
        A message confirming the document was reloaded, or describing
        the failure (document not loaded, no associated file, etc).

    Examples:
        ```json
        {
            "doc_name": "chassis"
        }
        ```
    """
    return reload_document_operation(get_freecad_connection(), doc_name)


@mcp.tool(structured_output=False)
def list_documents(ctx: Context) -> list[TextContent]:
    """Get the list of open documents in FreeCAD.

    Returns:
        A list of document names.
    """
    return list_documents_operation(get_freecad_connection())


@mcp.tool(structured_output=False)
def get_rpc_status(ctx: Context) -> list[TextContent]:
    """Get RPC and FreeCAD GUI-dispatch health.

    This tool does not use FreeCAD's GUI thread, so it remains available after
    a GUI operation times out. A ``stuck`` state identifies the operation that
    is still running and indicates that FreeCAD may need to be restarted.
    """
    return get_rpc_status_operation(get_freecad_connection())


@mcp.tool(structured_output=False)
def run_fem_analysis(
    ctx: Context,
    doc_name: str,
    analysis_name: str,
    timeout: int = 600,
    include_screenshot: bool = True,
    view_name: ViewName = "Isometric",
) -> list[TextContent | ImageContent]:
    """Run the CalculiX solver on an existing Fem::FemAnalysis container and return summary results.

    Prerequisites in the document:
    - A Part-derived solid (e.g. Part::Box, PartDesign::Body) acting as the geometry.
    - A Fem::AnalysisPython container created via `create_object`.
    - A Fem::MaterialCommon assigned to the geometry, added to the analysis.
    - A Fem::FemMeshGmsh referencing the geometry, added to the analysis (the
      mesh is generated automatically when created via `create_object`).
    - At least one Fem::ConstraintFixed and one Fem::ConstraintForce (or
      ConstraintPressure) bound to faces of the geometry, added to the analysis.

    A SolverCcxTools is auto-created if the analysis has none.

    The solver runs synchronously on the FreeCAD GUI thread and blocks all
    other RPC calls for its duration; do not fan out parallel requests.

    Returns max von Mises stress (MPa), max/min displacement (mm), node count,
    and the working directory CalculiX wrote to. On failure, returns the
    prerequisite-check or solver error along with the working directory for
    triage.

    Args:
        doc_name: Name of the FreeCAD document.
        analysis_name: Name of the Fem::AnalysisPython object.
        timeout: Seconds to wait for the solver (default 600).
        include_screenshot: Whether to return a screenshot of the model (default True).
            Set to False to save tokens when only the numeric results are needed.
        view_name: The view orientation of the returned screenshot (default "Isometric").
    """
    return run_fem_analysis_operation(
        get_freecad_connection(),
        state.only_text_feedback,
        doc_name,
        analysis_name,
        timeout,
        include_screenshot,
        view_name,
    )


@mcp.tool(structured_output=False)
def gui_ping(ctx: Context, cap: float = 5.0) -> list[TextContent]:
    """Probe whether FreeCAD's GUI thread is alive, under a short budget.

    Dispatches a no-op THROUGH the GUI thread and reports whether the thread
    answered. This is the question get_rpc_status structurally cannot answer:
    get_rpc_status reads a snapshot without touching the GUI thread, which is
    why it can report "healthy" while a real dispatch hangs.

    Read the result carefully:
    - alive=true  -> a no-op completed on the GUI thread within cap. If
      health.state is "stuck" at the same time, the flag is STALE and
      reset_dispatch_health can clear it.
    - alive=false with health.state "busy" -> the GUI is legitimately busy.
      THIS IS NOT THE SAME AS DEAD. Wait, or poll again.
    - alive=false with last_gui_heartbeat_age_s growing without bound -> the GUI
      thread is genuinely wedged. Nothing outside that thread can interrupt it.

    Args:
        cap: Seconds, used as BOTH the run and the queue budget (default 5.0).

    Returns:
        {alive, latency_s, cap_s, health, last_gui_heartbeat_age_s}.
    """
    return gui_ping_operation(get_freecad_connection(), cap)


@mcp.tool(structured_output=False)
def reset_dispatch_health(ctx: Context, force: bool = False) -> list[TextContent]:
    """Clear a STALE GUI-dispatch stuck flag. Operator action, not automation.

    A task that exceeds its run budget marks dispatch "stuck", and every later
    GUI call is rejected until it returns. When such a task ended on a path that
    never reached finish(), the flag outlives it and only a FreeCAD restart
    cleared it before v0.2.0.

    This tool first probes the GUI thread (the bypassing probe, not the gui_ping
    tool, which the flag itself would reject). If the thread answers, the flag is
    stale and is cleared. IF THE THREAD DOES NOT ANSWER, NOTHING IS CHANGED and
    the refusal is reported with stuck_since and the heartbeat age: a real wedge
    is reported honestly, never papered over.

    Args:
        force: Accepted and echoed. It does NOT bypass the liveness requirement
            and currently has no other specified effect (see OPEN-9).

    Returns:
        {reset: true, cleared_task_id, was_stuck_for_s} or
        {reset: false, reason, stuck_since, heartbeat_age_s}.
    """
    return reset_dispatch_health_operation(get_freecad_connection(), force)


@mcp.tool(structured_output=False)
def set_gui_budget(
    ctx: Context, tool: str, R: float, Q: float | None = None
) -> list[TextContent]:
    """Set one per-tool GUI budget on the addon. Operator action, not automation.

    The ADDON owns the budget table; this is the only way to change it, and the
    change is persisted and echoed so the client re-derives its socket timeouts
    from one copy. Budgets do not make multi-minute GUI work safe - they make
    the failure honest. Multi-minute work belongs in execute_code_async with
    commit_many().

    Args:
        tool: Table key - "execute_code", "gui_default", "fem", "commit" or
            "probe".
        R: Run budget in seconds, within [5, 3600]. The budget after which a
            running task is marked stuck.
        Q: Queue budget in seconds, within [5, 3600]. Defaults to R. The wait to
            START, which cancels without marking stuck.

    Returns:
        {success, persisted, budgets: <the whole new table>}.
    """
    return set_gui_budget_operation(get_freecad_connection(), tool, R, Q)


@mcp.prompt()
def asset_creation_strategy() -> str:
    return ASSET_CREATION_STRATEGY


def _validate_host(value: str) -> str:
    """Validate that *value* is a valid IP address or hostname.

    Used as the ``type`` callback for the ``--host`` argparse argument.
    Raises ``argparse.ArgumentTypeError`` on invalid input.
    """
    import argparse

    import validators

    if validators.ipv4(value) or validators.ipv6(value) or validators.hostname(value):
        return value
    raise argparse.ArgumentTypeError(
        f"Invalid host: '{value}'. Must be a valid IP address or hostname."
    )


def _env_port(default: int = 9875) -> int:
    """FREECAD_MCP_PORT, env only - there is no --port flag and none is added."""
    raw = os.environ.get("FREECAD_MCP_PORT")
    if not raw:
        return default
    try:
        port = int(raw)
    except ValueError:
        logger.warning(f"Ignoring invalid FREECAD_MCP_PORT={raw!r}; using {default}")
        return default
    if not (1 <= port <= 65535):
        logger.warning(f"Ignoring out-of-range FREECAD_MCP_PORT={raw!r}; using {default}")
        return default
    return port


def main():
    """Run the MCP server.

    Configuration precedence is CLI FLAG > ENVIRONMENT VARIABLE > DEFAULT. The
    flags are exactly the preserved three; the environment variables are
    additive fallbacks so install-specific paths never have to be written into
    a source file or a registration snippet. There are no secrets in this
    server: the only authentication in the whole stack is the addon's loopback
    host filter.
    """
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--only-text-feedback", action="store_true", help="Only return text feedback")
    parser.add_argument("--host", type=_validate_host, default=None, help="Host address of the FreeCAD RPC server to connect to (default: $FREECAD_MCP_HOST, then localhost)")
    parser.add_argument("--freecadcmd", default=None, help="Command that starts headless FreeCAD for execute_code_headless, e.g. 'flatpak run --command=freecadcmd org.freecad.FreeCAD' (default: $FREECAD_MCP_FREECADCMD, then auto-detect PATH, then Flatpak)")
    args = parser.parse_args()

    logger.setLevel(os.environ.get("FREECAD_MCP_LOG_LEVEL", "INFO").upper())
    state.only_text_feedback = (
        args.only_text_feedback
        or os.environ.get("FREECAD_MCP_ONLY_TEXT_FEEDBACK", "") == "1"
    )
    state.rpc_host = args.host or os.environ.get("FREECAD_MCP_HOST") or "localhost"
    state.rpc_port = _env_port()
    from .headless import parse_command
    state.freecadcmd = parse_command(
        args.freecadcmd or os.environ.get("FREECAD_MCP_FREECADCMD")
    )
    logger.info(f"Only text feedback: {state.only_text_feedback}")
    logger.info(f"Connecting to FreeCAD RPC server at: {state.rpc_host}:{state.rpc_port}")

    _install_signal_handlers()
    try:
        # stdio transport: mcp.run() returns when stdin reaches EOF, i.e. when
        # the parent that spawned this process is gone. That return IS BC-06's
        # shutdown signal.
        mcp.run()
    finally:
        close_bridge("stdin EOF (parent gone) or transport shutdown")
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass
    # Exit explicitly rather than falling off the end of main(): a lingering
    # non-daemon thread must not keep this process alive after its parent died.
    # That is the orphan class BC-06 exists to kill - see OPEN-13 in
    # _install_signal_handlers.
    os._exit(0)
