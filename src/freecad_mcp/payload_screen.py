"""BC-03: screen a payload for XML-1.0-illegal characters BEFORE it becomes XML.

``xmlrpc.client`` escapes markup but does not strip control characters, so a
raw 0x0B in a comment reaches the addon's parser and comes back as
``ExpatError: not well-formed (invalid token): line 441, column 67`` - a
position in the XML, corresponding to nothing in the caller's code. Measured on
16 September 2026: 0x0B 0x0C 0x85 put into a comment by shell escape collapse.

This screen runs on the CLIENT, before marshalling, and names the byte, the
offset, the line and the column IN THE CODE. Nothing is transmitted when it
returns a dict.

No addon-side screen is built: a payload that reached the addon has already
parsed, so the screen there would be dead code.
"""

# < 0x20 except TAB/LF/CR, plus DEL, the C1 block, and the Unicode line and
# paragraph separators.
ILLEGAL = (
    ({c for c in range(0x20)} - {0x09, 0x0A, 0x0D})
    | {0x7F}
    | set(range(0x80, 0xA0))
    | {0x2028, 0x2029}
)


def screen(code: str) -> dict | None:
    """Return None when *code* is safe to marshal, else a structured error.

    Walks the RAW string. It must never split the payload into lines first:
    str.split on line boundaries consumes exactly the characters being looked
    for (0x0B, 0x0C and 0x85 are all line boundaries to Python), so the one
    routine that must see them would be the one routine that cannot.
    """
    if not isinstance(code, str):
        return None
    for i, ch in enumerate(code):
        point = ord(ch)
        if point in ILLEGAL:
            line = code.count("\n", 0, i) + 1
            col = i - (code.rfind("\n", 0, i) + 1) + 1
            return {
                "success": False,
                "code": "ILLEGAL_PAYLOAD_CHARACTER",
                "error": (
                    f"illegal character 0x{point:02x} at line {line} col {col} "
                    f"(offset {i})"
                ),
                "byte": f"0x{point:02x}",
                "offset": i,
                "line": line,
                "col": col,
            }
    return None
