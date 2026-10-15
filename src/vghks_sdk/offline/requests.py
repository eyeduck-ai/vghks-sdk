"""Pure captured-request parsing and immutable operation signature lookup."""

from __future__ import annotations

import base64
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlsplit

from ..contracts.har import HarEntry, HarSignature
from ..core.operations import OPERATIONS, OperationSpec

if TYPE_CHECKING:
    from .bundle import BundleReader

RequestParts = tuple[str, str, dict[str, str], dict[str, str]]


def request_parts(request: Mapping[str, Any]) -> RequestParts:
    """Return fresh query/form dictionaries; never retain request values."""
    url = urlsplit(str(request.get("url", "")))
    query = dict(parse_qsl(url.query, keep_blank_values=True))
    unprepared = request.get("unprepared_kwargs") or {}
    params = unprepared.get("params", {})
    if isinstance(params, dict):
        query.update({str(key): str(value) for key, value in params.items()})
    body = request.get("body") or {}
    form: dict[str, str] = {}
    if isinstance(body.get("value"), dict):
        form = {str(key): str(value) for key, value in body["value"].items()}
    else:
        text = body.get("text")
        if text is None and body.get("base64"):
            text = base64.b64decode(body["base64"], validate=True).decode("utf-8")
        if isinstance(text, str):
            form = dict(parse_qsl(text, keep_blank_values=True))
    return str(request.get("method", "")).upper(), url.path, query, form


def request_key(request: dict) -> tuple | None:
    """Retain only parser inputs, without unrelated headers/cookies/capture data."""
    unprepared, body = request.get("unprepared_kwargs") or {}, request.get("body") or {}
    if not isinstance(unprepared, dict) or not isinstance(body, dict):
        return None
    params = unprepared.get("params", {})
    extra = tuple((str(key), str(value)) for key, value in params.items()) if isinstance(params, dict) else ()
    fields = body.get("value")
    if isinstance(fields, dict):
        form = ("fields", tuple((str(key), str(value)) for key, value in fields.items()))
    else:
        text = body.get("text")
        if text is None and body.get("base64"):
            if not isinstance(body["base64"], str):
                return None
            form = ("base64", body["base64"])
        else:
            form = ("text", text) if isinstance(text, str) else ("empty",)
    return str(request.get("method", "")).upper(), str(request.get("url", "")), extra, form


def _signature_index(operations: Iterable[OperationSpec]):
    groups = defaultdict(list)
    for spec in operations:
        groups[spec.method, spec.path].append((spec, HarSignature.from_operation(spec)))
    return MappingProxyType({
        endpoint: tuple(sorted(candidates, key=lambda item: (
            len(item[0].query_keys) + len(item[0].form_keys) + len(item[0].operation_values)
        ), reverse=True))
        for endpoint, candidates in groups.items()
    })


_SIGNATURES = _signature_index(OPERATIONS)


def identify_parts(method: str, path: str, query: Mapping[str, str], form: Mapping[str, str]) -> str:
    # A displaytag next-page GET returns rows, not the initial token form.
    if (method == "GET" and path == "/webmaas/RSV/RSV11W001.do" and query.get("patno")
            and any(re.fullmatch(r"d-\d+-p", key) and value.isdecimal()
                    for key, value in query.items())):
        return "webmaas.registration_query"
    entry = HarEntry(method, path, frozenset(query), frozenset(form), {**query, **form}, 0, None, "")
    return next((spec.key for spec, signature in _SIGNATURES.get((method, path), ())
                 if signature.matches(entry)), "")


def identify_operation(request: Mapping[str, Any]) -> str:
    return identify_parts(*request_parts(request))


def captured_request_parts(reader: BundleReader, request: Mapping[str, Any]) -> RequestParts:
    """Borrow read-only metadata from the bounded, single-analysis ZIP view."""
    parse = getattr(reader, "_request_parts", request_parts)
    return parse(request)
