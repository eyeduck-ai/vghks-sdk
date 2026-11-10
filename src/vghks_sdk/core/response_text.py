"""Decode HTTP response bodies for SDK transports and their callers."""

from __future__ import annotations

import codecs
import re
from functools import partial
from typing import Literal

_HTTP_CHARSET = re.compile(r"charset\s*=\s*[\"'\s]*([a-zA-Z0-9_-]+)", re.I)
_META_CHARSET = re.compile(rb'<meta\b[^>]*\bcharset\s*=\s*["\x27\s]*([a-zA-Z0-9_-]+)', re.I)

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
    content: bytes, content_type: str = "", *, encoding: str = "auto",
) -> tuple[str, str]:
    """Return text and codec for an account's auto, big5, or utf-8 setting."""
    if content.startswith(codecs.BOM_UTF8):
        return content.decode("utf-8-sig", errors="replace"), "utf-8-sig"
    if encoding == "big5":
        return decode_cp950(content, errors="replace"), "cp950"
    if encoding != "auto":
        return content.decode("utf-8", errors="replace"), "utf-8"
    try:
        return content.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    http, meta = declared_charsets(content, content_type)
    if (meta or http) in {"utf-8", "utf8"}:
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
