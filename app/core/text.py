"""Small text-parsing helpers shared across the app."""
from __future__ import annotations

import re
from typing import Any


def parse_names(text: str) -> list[str]:
    """Splits a comma-separated list of names (a name itself may contain
    spaces, e.g. "Черныш К.М."), used for the legacy free-text бригада
    fields when migrating them into real `people` rows."""
    return [n.strip() for n in (text or "").split(",") if n.strip()]


# Some source data (Yandex Constructor exports, tables copy-pasted out of a
# web page) carries Cyrillic as literal HTML entities — "&#1052;&#1045;..."
# instead of "МЕ..." — and lands in the database like that unless something
# decodes it on the way in. The same entity text then shows up again in
# every file we export, e.g. the Excel for Yandex Constructor.
#
# Only numeric entities and a short list of semicolon-terminated named ones
# are decoded — deliberately NOT html.unescape(), which also rewrites legacy
# names that need no semicolon ("&region=5" -> "®ion=5", "&para", "&copy"),
# which would mangle an URL or a code pasted into a description.
_ENTITY_RE = re.compile(r"&(?:#(\d{1,7})|#[xX]([0-9a-fA-F]{1,6})|(amp|lt|gt|quot|apos|nbsp));")
_NAMED = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'", "nbsp": " "}


def _decode_one(m: re.Match) -> str:
    dec, hexa, name = m.groups()
    if name:
        return _NAMED[name]
    code = int(dec) if dec else int(hexa, 16)
    if code == 0 or 0xD800 <= code <= 0xDFFF or code > 0x10FFFF:
        return m.group(0)
    return chr(code)


def decode_entities(value: Any) -> str:
    """Returns `value` as text with HTML entities decoded. Repeats until
    nothing changes, since a value that was encoded twice ("&amp;#1052;")
    needs two passes. A no-op on text without entities, so it's safe to
    call on anything user-visible."""
    if value is None:
        return ""
    s = str(value)
    for _ in range(3):
        decoded = _ENTITY_RE.sub(_decode_one, s)
        if decoded == s:
            break
        s = decoded
    return s
