"""Parsers for recorded PCK JSON responses, without network or status guessing."""

from __future__ import annotations

from typing import Any

from ..core.errors import ConfigurationError, ParseError
from ..models.review import ReviewCase, ReviewCasePart, ReviewCaseRef


def _object(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ParseError("review response was not an object", code="REVIEW_RESPONSE_INVALID")
    if value.get("ErrorMessage") or value.get("Errors"):
        raise ParseError("review backend reported an error", code="REVIEW_BACKEND_ERROR")
    return value


def _text(row: dict, key: str) -> str:
    value = row.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ParseError("invalid review field type", code="REVIEW_FIELD_INVALID")
    return value.strip()


def parse_login_info(payload: Any) -> dict:
    row = _object(payload)
    if not _text(row, "UserNMC") or not {"LoginDateTime", "LastLoginDateTime"} <= row.keys():
        raise ParseError("review login identity missing", code="REVIEW_LOGIN_INFO_INVALID")
    return dict(row)


def parse_review_options(payload: Any) -> dict[str, list[dict]]:
    row = _object(payload)
    if not {"InsuSectNo", "VSDrID", "VerifyCode"} <= row.keys():
        raise ParseError("review selectors missing", code="REVIEW_OPTIONS_INVALID")
    for items in row.values():
        parse_review_doctors(items)
    return dict(row)


def parse_review_doctors(payload: Any) -> list[dict]:
    if not isinstance(payload, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("Value"), str)
        or not isinstance(item.get("Text"), str)
        for item in payload
    ):
        raise ParseError("review choices malformed", code="REVIEW_OPTIONS_INVALID")
    return [dict(item) for item in payload]


def _grid(payload: Any) -> list[dict]:
    row = _object(payload)
    data, total = row.get("Data"), row.get("Total")
    if not isinstance(data, list) or type(total) is not int or total < 0:
        raise ParseError("review grid missing", code="REVIEW_GRID_INVALID")
    # Recorded Kendo pagination is client-side. Never silently accept truncation.
    if len(data) != total:
        raise ParseError("review grid was incomplete", code="REVIEW_GRID_INCOMPLETE")
    return [_object(item) for item in data]


def parse_review_case(payload: Any, reference: ReviewCaseRef | None = None) -> ReviewCase:
    row = _object(payload)
    try:
        ref = ReviewCaseRef(_text(row, "ApplySeq"))
    except ConfigurationError as exc:
        raise ParseError("review application reference missing", code="REVIEW_REF_INVALID") from exc
    if reference is not None and ref != reference:
        raise ParseError("review case identity mismatch", code="REVIEW_IDENTITY_MISMATCH")
    return ReviewCase(
        ref,
        _text(row, "ApplyDate"),
        _text(row, "PatNo"),
        _text(row, "PatName"),
        _text(row, "VSDrID"),
        _text(row, "InsuSectNo"),
        _text(row, "VerifyCode"),
        _text(row, "ApplyStatus"),
        _text(row, "ApplyFinishFlag"),
        dict(row),
    )


def parse_review_cases(payload: Any) -> list[ReviewCase]:
    result = [parse_review_case(row) for row in _grid(payload)]
    if len({r.reference for r in result}) != len(result):
        raise ParseError("duplicate review case identities", code="REVIEW_DUPLICATE_CASE")
    return result


def parse_review_part(payload: Any, reference: ReviewCaseRef, kind: str) -> ReviewCasePart:
    rows = _grid(payload)
    for row in rows:
        if _text(row, "ApplySeq") != reference.apply_seq:
            raise ParseError("review case part identity mismatch", code="REVIEW_IDENTITY_MISMATCH")
    return ReviewCasePart(reference, kind, tuple(dict(row) for row in rows), len(rows))
