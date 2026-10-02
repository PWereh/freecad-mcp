"""v0.3.0 - run a governed script by REFERENCE, so its text never passes through a model.

Measured 2026-10-02 in rxCAD HP-1: an agent asked to send a ~5.5 KB hash-pinned probe "verbatim"
produced three different bodies in three minutes, each denied by the harness hook because none
hashed to the pin. A long body retyped through model output drifts. The remedy is structural: the
call names a FILE and the hash its body must have, and the bridge reads the file itself.

Binding the content to the call. A PreToolUse hook runs in a separate process BEFORE the call
reaches this server, so a file checked by the hook could be swapped before it is read here. The
call therefore carries ``script_sha256`` and this module refuses unless the bytes IT reads hash to
that value. Read once, hash those bytes, execute those same bytes - never re-read.

Off by default. Nothing is readable until ``FREECAD_MCP_SCRIPT_ROOTS`` names at least one root,
and a path is accepted only if its real path (symlinks resolved) lies under one of them.

The hash definition MUST stay byte-identical to the harness's (rxCAD build_probe_index.normalise
and the hook): body = the text after the separator line, starting with the newline that ends it;
normalise = CRLF/CR -> LF, rstrip each line, strip trailing newlines; sha256 of the UTF-8 bytes.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Any

DEFAULT_SEPARATOR = "# ---- BODY (hashed) ----"
MAX_SCRIPT_BYTES = 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ScriptRefError(ValueError):
    """A script reference was refused. The message says which rule refused it."""


def normalise(body: str) -> str:
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    body = "\n".join(line.rstrip() for line in body.split("\n"))
    return body.rstrip("\n")


def body_sha256(body: str) -> str:
    return hashlib.sha256(normalise(body).encode("utf-8")).hexdigest()


def configured_roots() -> list[str]:
    raw = os.environ.get("FREECAD_MCP_SCRIPT_ROOTS", "")
    return [os.path.realpath(r) for r in raw.split(os.pathsep) if r.strip()]


def separator() -> str:
    return os.environ.get("FREECAD_MCP_SCRIPT_SEPARATOR") or DEFAULT_SEPARATOR


def _under(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(root)]) == os.path.normcase(root)
    except ValueError:          # different drives on Windows
        return False


def _params_literal(params: Any) -> str:
    """A Python literal the hook can parse with ast.literal_eval and that evaluates to the same
    values the hook validated from the JSON call. repr, not json: json's true/false/null are not
    Python literals."""
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ScriptRefError("params must be a flat JSON object")
    for key, value in params.items():
        if not isinstance(key, str) or not _KEY.match(key):
            raise ScriptRefError(f"params key {key!r} is not an identifier")
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise ScriptRefError(f"params[{key!r}] must be a scalar, got {type(value).__name__}")
        if isinstance(value, float) and not math.isfinite(value):
            raise ScriptRefError(f"params[{key!r}] must be finite")
    return repr(dict(params))


def resolve(
    code: str | None,
    script_path: str | None,
    script_sha256: str | None,
    params: dict[str, Any] | None,
) -> str:
    """Return the exact code to execute, or raise ScriptRefError.

    ``code`` alone is the unchanged pre-v0.3.0 path and is returned as given.
    """
    if script_path is None:
        if code is None:
            raise ScriptRefError("give exactly one of code or script_path (got neither)")
        if script_sha256 is not None or params is not None:
            raise ScriptRefError("script_sha256 and params are only valid with script_path")
        return code
    if code is not None:
        raise ScriptRefError("give exactly one of code or script_path (got both)")

    roots = configured_roots()
    if not roots:
        raise ScriptRefError(
            "script_path is disabled: FREECAD_MCP_SCRIPT_ROOTS names no root directory")
    if not script_sha256 or not _SHA256.match(script_sha256):
        raise ScriptRefError("script_sha256 must be 64 lowercase hex characters")
    if not os.path.isabs(script_path):
        raise ScriptRefError("script_path must be absolute")
    real = os.path.realpath(script_path)
    if not any(_under(real, root) for root in roots):
        raise ScriptRefError(f"script_path is outside every configured root: {real}")
    if not os.path.isfile(real):
        raise ScriptRefError(f"script_path is not a file: {real}")

    with open(real, "rb") as fh:              # read ONCE; everything below uses these bytes
        raw = fh.read(MAX_SCRIPT_BYTES + 1)
    if len(raw) > MAX_SCRIPT_BYTES:
        raise ScriptRefError(f"script is larger than {MAX_SCRIPT_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ScriptRefError(f"script is not UTF-8: {exc}") from None

    sep = separator()
    if text.count(sep) != 1:
        raise ScriptRefError(f"script must contain the separator line exactly once: {sep!r}")
    body = text.partition(sep)[2]
    actual = body_sha256(body)
    if actual != script_sha256:
        raise ScriptRefError(
            f"script body hash mismatch: file has {actual}, call asked for {script_sha256}")

    # The file's own header (comments, any PARAMS) is never executed: PARAMS comes only from the
    # call, and the body is exactly the bytes that were hashed.
    return "PARAMS = " + _params_literal(params) + "\n" + sep + body
