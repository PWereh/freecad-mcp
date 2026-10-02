"""v0.3.0 - execute a governed script by reference, hash-bound and root-confined."""
import ast
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from freecad_mcp import script_ref, server

SEP = script_ref.DEFAULT_SEPARATOR
BODY = "\nimport sys\nprint('RXC_JSON', PARAMS['obj'])   \r\nprint('done')\n\n"
FILE = "# rxc-probe v1 id=t\n# header comment, never executed\nPARAMS = {'obj': 'header'}\n" + SEP + BODY


def _sha(body: str) -> str:
    return hashlib.sha256(script_ref.normalise(body).encode("utf-8")).hexdigest()


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    r = tmp_path / "probes"
    r.mkdir()
    monkeypatch.setenv("FREECAD_MCP_SCRIPT_ROOTS", str(r))
    monkeypatch.delenv("FREECAD_MCP_SCRIPT_SEPARATOR", raising=False)
    return r


def _write(root: Path, name: str = "p.py", text: str = FILE) -> Path:
    p = root / name
    p.write_bytes(text.encode("utf-8"))
    return p


# ---- the unchanged path -------------------------------------------------------------------------

def test_code_alone_is_returned_unchanged() -> None:
    assert script_ref.resolve("print(1)", None, None, None) == "print(1)"


@pytest.mark.parametrize("args,msg", [
    ((None, None, None, None), "neither"),
    (("print(1)", "C:/x.py", "0" * 64, None), "both"),
    (("print(1)", None, "0" * 64, None), "only valid with script_path"),
    (("print(1)", None, None, {"a": 1}), "only valid with script_path"),
])
def test_exactly_one_of_code_or_script_path(args, msg) -> None:
    with pytest.raises(script_ref.ScriptRefError, match=msg):
        script_ref.resolve(*args)


# ---- the composed payload -----------------------------------------------------------------------

def test_composed_payload_is_params_then_separator_then_the_hashed_body(root: Path) -> None:
    p = _write(root)
    out = script_ref.resolve(None, str(p), _sha(BODY), {"obj": "Plate", "tol": 0.5, "n": 3, "flag": True, "none": None})
    head, sep, body = out.partition(SEP)
    assert sep == SEP and body == BODY                      # exactly the bytes that were hashed
    assert head.startswith("PARAMS = ") and head.endswith("\n") and head.count("\n") == 1
    # the hook parses the header with ast.literal_eval; it must give back the JSON values
    assert ast.literal_eval(head[len("PARAMS = "):].strip()) == {
        "obj": "Plate", "tol": 0.5, "n": 3, "flag": True, "none": None}
    assert "header comment" not in out and "'header'" not in out   # file header never executed
    assert _sha(out.partition(SEP)[2]) == _sha(BODY)                # hook-side hash agrees


def test_windows_paths_survive_the_literal(root: Path) -> None:
    p = _write(root)
    path = r"C:\Claude\Projects\rxCAD\runs\x\doc\checkpoint-0.FCStd"
    out = script_ref.resolve(None, str(p), _sha(BODY), {"checkpoint": path})
    assert ast.literal_eval(out.partition(SEP)[0][len("PARAMS = "):].strip()) == {"checkpoint": path}


# ---- the refusals -------------------------------------------------------------------------------

def test_off_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FREECAD_MCP_SCRIPT_ROOTS", raising=False)
    p = tmp_path / "p.py"
    p.write_text(FILE)
    with pytest.raises(script_ref.ScriptRefError, match="disabled"):
        script_ref.resolve(None, str(p), _sha(BODY), None)


def test_a_path_outside_every_root_is_refused(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere.py"
    outside.write_text(FILE)
    with pytest.raises(script_ref.ScriptRefError, match="outside every configured root"):
        script_ref.resolve(None, str(outside), _sha(BODY), None)


def test_dot_dot_cannot_escape_a_root(root: Path, tmp_path: Path) -> None:
    (tmp_path / "evil.py").write_text(FILE)
    with pytest.raises(script_ref.ScriptRefError, match="outside every configured root"):
        script_ref.resolve(None, str(root / ".." / "evil.py"), _sha(BODY), None)


def test_a_relative_path_is_refused(root: Path) -> None:
    _write(root)
    with pytest.raises(script_ref.ScriptRefError, match="absolute"):
        script_ref.resolve(None, "p.py", _sha(BODY), None)


def test_the_body_is_bound_to_the_hash_in_the_call(root: Path) -> None:
    """Closes the hook-vs-bridge time-of-check gap: a file swapped after the hook approved it no
    longer hashes to the value the call carries, so it is refused here."""
    p = _write(root)
    approved = _sha(BODY)
    p.write_bytes(FILE.replace("print('done')", "import os; os.remove('x')").encode())
    with pytest.raises(script_ref.ScriptRefError, match="hash mismatch"):
        script_ref.resolve(None, str(p), approved, None)


def test_header_edits_do_not_change_the_hash(root: Path) -> None:
    p = _write(root, text="# a different header\n" + SEP + BODY)
    assert script_ref.resolve(None, str(p), _sha(BODY), None).endswith(BODY)


@pytest.mark.parametrize("text", [FILE.replace(SEP, "# no separator"), FILE + SEP + "\n"])
def test_the_separator_must_appear_exactly_once(root: Path, text: str) -> None:
    p = _write(root, text=text)
    with pytest.raises(script_ref.ScriptRefError, match="exactly once"):
        script_ref.resolve(None, str(p), _sha(BODY), None)


@pytest.mark.parametrize("bad", ["ABC", "0" * 63, "G" * 64, None])
def test_the_hash_must_be_64_lowercase_hex(root: Path, bad) -> None:
    p = _write(root)
    with pytest.raises(script_ref.ScriptRefError, match="64 lowercase hex"):
        script_ref.resolve(None, str(p), bad, None)


@pytest.mark.parametrize("params,msg", [
    ([1, 2], "flat JSON object"),
    ({"a": [1]}, "scalar"),
    ({"a": {"b": 1}}, "scalar"),
    ({"not ok": 1}, "identifier"),
    ({"a": float("nan")}, "finite"),
])
def test_params_must_be_flat_finite_scalars(root: Path, params, msg) -> None:
    p = _write(root)
    with pytest.raises(script_ref.ScriptRefError, match=msg):
        script_ref.resolve(None, str(p), _sha(BODY), params)


def test_the_separator_is_configurable(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FREECAD_MCP_SCRIPT_SEPARATOR", "# === BODY ===")
    p = _write(root, text="# h\n# === BODY ===" + BODY)
    assert script_ref.resolve(None, str(p), _sha(BODY), None).endswith("# === BODY ===" + BODY)


# ---- through the tool ---------------------------------------------------------------------------

def test_headless_tool_runs_the_referenced_file(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _write(root)
    monkeypatch.setattr(server.state, "freecadcmd", [sys.executable])
    res = server.execute_code_headless(None, script_path=str(p), script_sha256=_sha(BODY),
                                       params={"obj": "Plate"}, timeout=60)
    text = res[0].text
    assert "RXC_JSON Plate" in text and "done" in text


def test_headless_tool_reports_a_refusal_without_running(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _write(root)
    monkeypatch.setattr(server.state, "freecadcmd", [sys.executable])
    res = server.execute_code_headless(None, script_path=str(p), script_sha256="0" * 64)
    payload = json.loads(res[0].text)
    assert payload["success"] is False and "hash mismatch" in payload["error"]


# ---- interoperability with the rxCAD harness pins -----------------------------------------------

HARNESS = Path(r"C:\Claude\Projects\rxCAD\harness\probes")


@pytest.mark.skipif(not (HARNESS / "index.json").exists(), reason="rxCAD harness not present")
def test_the_bridge_hash_equals_the_harness_pin_for_every_probe() -> None:
    """If this fails, a pinned probe sent by script_path would be refused by one side and not the
    other. The definition is shared with rxCAD build_probe_index.normalise on purpose."""
    index = json.loads((HARNESS / "index.json").read_text(encoding="utf-8"))
    pins = index.get("probes", index)
    checked = 0
    for digest, entry in pins.items():
        f = HARNESS / (entry["probe_id"] + ".py")
        text = f.read_text(encoding="utf-8")
        assert script_ref.body_sha256(text.partition(SEP)[2]) == digest, entry["probe_id"]
        checked += 1
    assert checked >= 10
