"""v0.3.1 - which process this bridge SERVES, reported to the addon at hello().

The addon supersedes older registrations from the same client (see bridges.hello), so a reconnect
in one MCP client never counts twice. Measured 2026-10-02: /mcp reconnect started a second bridge
under the same claude.exe (pid 2500) without closing the first, and both stayed registered.

The parent of this interpreter is NOT the client on Windows. Measured chain for one live bridge:

    python.exe        (uv base interpreter - this process)
    python.exe        .venv\\Scripts\\python.exe        venv redirector
    freecad-mcp.exe   .venv\\Scripts\\freecad-mcp.exe   console-script launcher
    claude.exe        the MCP client - the process the bridges share

Both intermediates are different for every bridge, so they are skipped. The rule is by FULL PATH,
not by name: skip every ancestor whose executable lives in THIS venv's Scripts directory. A name
rule would either miss the redirector or wrongly skip a client that is itself a python.exe.
Best effort: on any failure the answer is None, and the addon then behaves as before.
"""

from __future__ import annotations

import os
import sys


def _scripts_dir() -> str:
    return os.path.normcase(os.path.realpath(os.path.join(sys.prefix, "Scripts")))


def _windows_parent_table() -> dict[int, int]:
    """pid -> parent pid, from one Toolhelp32 snapshot."""
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260),
        ]

    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)    # TH32CS_SNAPPROCESS
    if snap in (None, wintypes.HANDLE(-1).value):
        return {}
    table: dict[int, int] = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            table[entry.th32ProcessID] = entry.th32ParentProcessID
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return table


def _windows_image_path(pid: int) -> str | None:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = wintypes.HANDLE
    handle = k32.OpenProcess(0x1000, False, pid)           # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if not k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return None
        return buf.value
    finally:
        k32.CloseHandle(handle)


def _first_non_venv_ancestor(start: int, parents: dict[int, int], image_path, scripts: str) -> int:
    """Walk up from ``start`` past every process whose image lives in ``scripts``."""
    pid, seen = start, set()
    while pid and pid not in seen:
        seen.add(pid)
        path = image_path(pid)
        if path is None or os.path.normcase(os.path.dirname(os.path.realpath(path))) != scripts:
            return pid
        pid = parents.get(pid, 0)
    return pid


def client_pid() -> int | None:
    """The pid of the MCP client this bridge serves, or None when it cannot be determined."""
    try:
        parent = os.getppid()
        if sys.platform != "win32":
            return parent or None
        found = _first_non_venv_ancestor(parent, _windows_parent_table(),
                                         _windows_image_path, _scripts_dir())
        return found or None
    except Exception:
        return None
