from dataclasses import dataclass

from .freecad_client import FreeCADConnection


@dataclass
class ServerState:
    only_text_feedback: bool = False
    rpc_host: str = "localhost"
    rpc_port: int = 9875
    freecad_connection: FreeCADConnection | None = None
    freecadcmd: list[str] | None = None  # headless FreeCAD command; None = auto-detect
    # Reported by the addon at connect and stored here so tools and diagnostics
    # read ONE copy. Nothing is pushed to the addon and nothing is compared
    # across the boundary; the client only derives from what it is told.
    budgets: dict | None = None
    bridge_contract: str | None = None
    jobs_dir: str | None = None
