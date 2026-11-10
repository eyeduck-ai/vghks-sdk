"""Decode HTTP response bodies for SDK transports and their callers."""

from __future__ import annotations

import codecs
import re
import unicodedata
from functools import partial
from itertools import chain
from typing import Literal, TypedDict

_HTTP_CHARSET = re.compile(r"charset\s*=\s*[\"'\s]*([a-zA-Z0-9_-]+)", re.I)
_META_CHARSET = re.compile(rb'<meta\b[^>]*\bcharset\s*=\s*["\x27\s]*([a-zA-Z0-9_-]+)', re.I)
_UTF8_CHARSETS = {"utf-8", "utf8", "utf_8"}
_COMMON_CHARSETS = _UTF8_CHARSETS | {
    "big5", "big-5", "cp950", "ms950", "950", "iso-8859-1", "latin1", "latin-1",
}

# Windows CP950 user-defined rows and the PUA code point at trail byte 0x40.
# Python's existing CP950 mappings take precedence.
_CP950_EUDC_ROWS = (
    (0x81, 0x8D, 0xEEB8),
    (0x8E, 0xA0, 0xE311),
    (0xC7, 0xC8, 0xF70F),
    (0xFA, 0xFE, 0xE000),
)


def _cp950_eudc_error(error: UnicodeDecodeError, *, fallback):
    start = error.start
    if start + 1 >= len(error.object):
        return fallback(error)
    lead, trail = error.object[start:start + 2]
    if 0x40 <= trail <= 0x7E:
        trail_offset = trail - 0x40
    elif 0xA1 <= trail <= 0xFE:
        trail_offset = 63 + trail - 0xA1
    else:
        return fallback(error)
    for first, last, first_codepoint in _CP950_EUDC_ROWS:
        if first <= lead <= last:
            # Consume both bytes so the next character stays aligned.
            return chr(first_codepoint + (lead - first) * 157 + trail_offset), start + 2
    return fallback(error)


codecs.register_error(
    "vghks_sdk_cp950_eudc_strict", partial(_cp950_eudc_error, fallback=codecs.strict_errors),
)
codecs.register_error(
    "vghks_sdk_cp950_eudc_replace", partial(_cp950_eudc_error, fallback=codecs.replace_errors),
)


def decode_cp950(content: bytes, *, errors: Literal["strict", "replace"] = "strict") -> str:
    """Fill gaps in Python's CP950 table with Windows private-use code points."""
    return content.decode("cp950", errors="vghks_sdk_cp950_eudc_" + errors)


def declared_charsets(content: bytes, content_type: str) -> tuple[str | None, str | None]:
    """Read the HTTP charset and the first HTML meta charset in the body prefix."""
    http = _HTTP_CHARSET.search(content_type)
    meta = _META_CHARSET.search(content[:16384])
    return (
        http.group(1).lower() if http else None,
        meta.group(1).decode("ascii").lower() if meta else None,
    )


def decode_response_text(
    content: bytes, content_type: str = "", *, encoding_hint: str | None = None,
) -> tuple[str, str]:
    """Return decoded text and codec using the response's own encoding hints."""
    if content.startswith(codecs.BOM_UTF8):
        return content.decode("utf-8-sig", errors="replace"), "utf-8-sig"
    http, meta = declared_charsets(content, content_type)
    declared = meta or http
    hint = declared or (encoding_hint or "").lower()
    if hint and hint not in _COMMON_CHARSETS:
        try:
            codec = codecs.lookup(hint).name
            return content.decode(codec), codec
        except (LookupError, UnicodeError):
            pass
    try:
        return content.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    if declared in _UTF8_CHARSETS:
        # Use the base codecs to choose an encoding for mislabeled pages.
        try:
            return content.decode("cp950"), "cp950"
        except UnicodeDecodeError:
            utf8 = content.decode("utf-8", errors="replace")
            big5 = content.decode("cp950", errors="replace")
            if utf8.count("\ufffd") <= big5.count("\ufffd"):
                return utf8, "utf-8"
    # Legacy PRQ pages can contain a damaged byte alongside valid text.
    return decode_cp950(content, errors="replace"), "cp950"


class PrivateUseCharacter(TypedDict):
    code_point: str
    windows_cp950: str | None


def private_use_characters(*texts: str) -> list[PrivateUseCharacter]:
    """Describe up to 16 distinct private-use code points from parsed text."""
    points: set[int] = set()
    for char in chain.from_iterable(texts):
        if unicodedata.category(char) == "Co":
            points.add(ord(char))
            if len(points) == 16:
                break
    return [
        {"code_point": f"U+{point:04X}", "windows_cp950": _cp950_equivalent(point)}
        for point in sorted(points)
    ]


def _cp950_equivalent(point: int) -> str | None:
    for first, last, first_codepoint in _CP950_EUDC_ROWS:
        offset = point - first_codepoint
        if 0 <= offset < (last - first + 1) * 157:
            row, column = divmod(offset, 157)
            trail = 0x40 + column if column < 63 else 0xA1 + column - 63
            pair = bytes((first + row, trail))
            if decode_cp950(pair) == chr(point):
                return pair.hex(" ").upper()
    return None
