"""Privacy-preserving diagnostics for use in the disconnected hospital network.

The recorder deliberately accepts request *field names* and response structure,
never request values or raw response text.  Diagnostic bundles must still be
handled according to hospital policy because counts and endpoint names can be
operationally sensitive.
"""

from __future__ import annotations

import json
import platform
import re
import threading
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

import bs4
import requests
from bs4 import BeautifulSoup

from .errors import ConfigurationError, error_code

_CHARSET_RE = re.compile(r"charset\s*=\s*['\"]?([^;\s'\"]+)", re.IGNORECASE)
_SAFE_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\[\]-]{0,79}$")
_SAFE_CODE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,79}$")
_STATIC_PATH_SEGMENTS = {
    "PRQWeb",
    "SectOrdWeb",
    "OPPLWeb",
    "webmaas",
    "RSV",
    "Page",
    "JSP",
    "ajax",
    "WPSAutoLogon",
}
_STATIC_ENDPOINT_SEGMENTS = {
    "AJAXAction.do",
    "GenerateKeyAction.do",
    "QueryBillingSOAP.do",
    "QueryCaseDetail.do",
    "QueryCaseList.do",
    "QueryOPDPatList.do",
    "QueryPatientRecord.do",
    "QueryRecordList.do",
    "QueryResNumCenter.do",
    "RSV11W001.do",
    "ReportIndex.jsp",
    "TestReport_A.jsp",
    "TestReport_Select.jsp",
    "aptreePath.do",
    "login.do",
    "myPortal.do",
    "so.do",
    "ssoFromDn.do",
    "ssoLogAdd.do",
    "surgAction.do",
    "syserrorexception.jsp",
}
_HTML_SELECTORS = (
    "form",
    "form#RSV11WForm",
    "#data",
    "#data .soap",
    "#data .soap pre",
    "table.eTable",
    "table#row",
    "table#dataTbl",
    "table#pgnTbl",
    "table#pgnTb2",
    "#typeO",
    "#tabs",
    "#tab_ul",
)
_MAX_SHAPE_BYTES = 4_000_000


class DiagnosticRecorder:
    """Write a two-file redacted diagnostic bundle.

    `diagnostics.jsonl` is the chronological trace. `summary.json` contains the
    final command result and optional smoke-test check statuses.
    """

    schema_version = 2

    def __init__(
        self,
        directory: Path,
        *,
        overwrite: bool = False,
        run_id: str | None = None,
    ) -> None:
        self.directory = directory
        self.trace_path = directory / "diagnostics.jsonl"
        self.summary_path = directory / "summary.json"
        if not overwrite and (self.trace_path.exists() or self.summary_path.exists()):
            raise ConfigurationError(
                "diagnostic files already exist; choose a new directory or allow overwrite"
            )
        directory.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id or uuid.uuid4().hex
        self._lock = threading.RLock()
        self._sequence = 0
        self._request_sequence = 0
        self._operation_sequence = 0
        self._local = threading.local()
        self._finalized = False
        self._started = monotonic()
        self._handle = self.trace_path.open("w", encoding="utf-8", newline="\n")
        self._write(
            "run_started",
            {
                "runtime": {
                    "python": _safe_version(platform.python_version()),
                    "requests": _safe_version(requests.__version__),
                    "beautifulsoup4": _safe_version(bs4.__version__),
                }
            },
        )

    def record_http_request(
        self,
        *,
        method: str,
        url: str,
        attempt: int,
        max_attempts: int,
        throttle_delay_seconds: float,
        tls_verification_enabled: bool,
        kwargs: Mapping[str, Any],
    ) -> int:
        with self._lock:
            self._request_sequence += 1
            request_id = self._request_sequence
        query_keys = _query_keys(url)
        query_keys.extend(_mapping_keys(kwargs.get("params")))
        self._write(
            "http_request",
            {
                "request_id": request_id,
                "method": method.upper() if method.upper() in {"GET", "POST"} else "OTHER",
                "path": _safe_path(url),
                "attempt": attempt,
                "max_attempts": max_attempts,
                "throttle_delay_ms": round(max(0.0, throttle_delay_seconds) * 1000, 1),
                "tls_verification_enabled": bool(tls_verification_enabled),
                "operation_id": _safe_int(getattr(self._local, "operation_id", 0)),
                "query_keys": sorted(set(query_keys)),
                "form_keys": sorted(set(_mapping_keys(kwargs.get("data")))),
                "json_keys": sorted(set(_mapping_keys(kwargs.get("json")))),
            },
        )
        return request_id

    def record_http_response(
        self, *, request_id: int, response: Any, elapsed_seconds: float
    ) -> None:
        history = []
        for prior in getattr(response, "history", ()) or ():
            history.append(
                {
                    "status_code": _safe_int(getattr(prior, "status_code", 0)),
                    "path": _safe_path(str(getattr(prior, "url", ""))),
                }
            )
        self._write(
            "http_response",
            {
                "request_id": request_id,
                "status_code": _safe_int(getattr(response, "status_code", 0)),
                "final_path": _safe_path(str(getattr(response, "url", ""))),
                "elapsed_ms": round(max(0.0, elapsed_seconds) * 1000, 1),
                "redirects": history,
                "response": _response_structure(response),
            },
        )

    def record_network_error(
        self,
        *,
        request_id: int,
        exc: BaseException,
        elapsed_seconds: float,
        will_retry: bool,
    ) -> None:
        self._write(
            "http_network_error",
            {
                "request_id": request_id,
                "error_type": _safe_code(exc.__class__.__name__),
                "error_chain": _exception_types(exc),
                "elapsed_ms": round(max(0.0, elapsed_seconds) * 1000, 1),
                "will_retry": bool(will_retry),
            },
        )

    def record_retry(
        self,
        *,
        request_id: int,
        reason: str,
        next_attempt: int,
        delay_seconds: float,
    ) -> None:
        self._write(
            "http_retry",
            {
                "request_id": request_id,
                "reason": _safe_code(reason),
                "next_attempt": next_attempt,
                "delay_ms": round(max(0.0, delay_seconds) * 1000, 1),
            },
        )

    def start_operation(self, *, name: str, app_key: str) -> int:
        with self._lock:
            self._operation_sequence += 1
            operation_id = self._operation_sequence
            self._local.operation_id = operation_id
        self._write(
            "operation_started",
            {
                "operation_id": operation_id,
                "operation": _safe_code(name),
                "application": _safe_code(app_key),
            },
        )
        return operation_id

    def finish_operation(
        self,
        *,
        operation_id: int,
        name: str,
        status: str,
        exc: BaseException | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "operation_id": operation_id,
            "operation": _safe_code(name),
            "status": _safe_code(status),
        }
        if exc is not None:
            payload["error_code"] = _safe_code(error_code(exc))
            payload["error_type"] = _safe_code(exc.__class__.__name__)
        self._write("operation_finished", payload)
        if getattr(self._local, "operation_id", None) == operation_id:
            self._local.operation_id = 0

    def record_reauthentication(self, *, operation_id: int, app_key: str) -> None:
        self._write(
            "reauthentication_started",
            {
                "operation_id": operation_id,
                "application": _safe_code(app_key),
            },
        )

    def record_probe_step(
        self,
        *,
        name: str,
        status: str,
        error: BaseException | None = None,
        error_code_value: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "check": _safe_code(name),
            "status": _safe_code(status),
            "details": _safe_details(details or {}),
        }
        if error is not None:
            payload["error_code"] = _safe_code(error_code(error))
            payload["error_type"] = _safe_code(error.__class__.__name__)
        elif error_code_value:
            payload["error_code"] = _safe_code(error_code_value)
        self._write("diagnostic_check", payload)

    def finalize(
        self,
        *,
        command: str,
        status: str,
        exit_code: int,
        error: BaseException | None = None,
        checks: Iterable[Mapping[str, Any]] = (),
    ) -> None:
        with self._lock:
            if self._finalized:
                return
            self._finalized = True
            error_value = _safe_code(error_code(error)) if error is not None else ""
            self._write(
                "run_finished",
                {
                    "command": _safe_code(command),
                    "status": _safe_code(status),
                    "exit_code": int(exit_code),
                    "error_code": error_value,
                    "elapsed_ms": round((monotonic() - self._started) * 1000, 1),
                },
            )
            self._handle.close()
            summary = {
                "schema_version": self.schema_version,
                "run_id": self.run_id,
                "command": _safe_code(command),
                "status": _safe_code(status),
                "exit_code": int(exit_code),
                "error_code": error_value,
                "checks": [_safe_check(item) for item in checks],
                "contains_raw_request_or_response": False,
            }
            with self.summary_path.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(summary, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")

    def _write(self, event: str, payload: Mapping[str, Any]) -> None:
        with self._lock:
            self._sequence += 1
            row = {
                "schema_version": self.schema_version,
                "run_id": self.run_id,
                "sequence": self._sequence,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "event": _safe_code(event),
                **payload,
            }
            self._handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            self._handle.write("\n")
            self._handle.flush()


def _response_structure(response: Any) -> Mapping[str, Any]:
    raw = bytes(getattr(response, "content", b"") or b"")
    headers = getattr(response, "headers", {}) or {}
    content_type = str(headers.get("Content-Type", ""))
    mime_type = content_type.split(";", 1)[0].strip().lower()
    charset_match = _CHARSET_RE.search(content_type)
    declared_charset = charset_match.group(1).lower() if charset_match else ""
    sample = raw[:_MAX_SHAPE_BYTES]
    text, decoded_as = _decode_sample(sample, declared_charset)
    base: dict[str, Any] = {
        "byte_length": len(raw),
        "shape_truncated": len(raw) > len(sample),
        "mime_type": mime_type,
        "declared_charset": _safe_code(declared_charset) if declared_charset else "",
        "decoded_as": _safe_code(decoded_as),
    }

    if len(raw) <= _MAX_SHAPE_BYTES:
        try:
            value = json.loads(text)
        except (json.JSONDecodeError, UnicodeError):
            pass
        else:
            base["kind"] = "json"
            base["json_shape"] = _json_shape(value)
            return base

    lowered = text[:4096].lower()
    if "html" in mime_type or "<html" in lowered or "<form" in lowered or "<table" in lowered:
        base["kind"] = "html"
        base["html_shape"] = _html_shape(text)
        return base

    form_keys = _form_encoded_keys(text)
    if form_keys:
        base["kind"] = "form_encoded_text"
        base["field_names"] = form_keys
    else:
        base["kind"] = "text"
        base["non_whitespace_length"] = len(re.sub(r"\s+", "", text))
    return base


def _html_shape(text: str) -> Mapping[str, Any]:
    soup = BeautifulSoup(text, "html.parser")
    tag_names = ("html", "form", "input", "table", "tr", "th", "td", "script", "pre", "div")
    forms = []
    for form in soup.find_all("form")[:20]:
        forms.append(
            {
                "id": _safe_field_name(str(form.get("id") or "")),
                "method": str(form.get("method") or "GET").upper()
                if str(form.get("method") or "GET").upper() in {"GET", "POST"}
                else "OTHER",
                "action_path": _safe_path(str(form.get("action") or "")),
                "input_names": sorted(
                    {
                        _safe_field_name(str(node.get("name") or ""))
                        for node in form.find_all(["input", "select", "textarea"])
                        if node.get("name")
                    }
                ),
            }
        )
    tables = []
    for table in soup.find_all("table")[:30]:
        tables.append(
            {
                "id": _safe_field_name(str(table.get("id") or "")),
                "classes": sorted(
                    {_safe_field_name(str(value)) for value in (table.get("class") or [])}
                ),
                "row_count": len(table.find_all("tr")),
                "header_cell_count": len(table.find_all("th")),
                "data_cell_count": len(table.find_all("td")),
            }
        )
    return {
        "tag_counts": {name: len(soup.find_all(name)) for name in tag_names},
        "selector_counts": {selector: len(soup.select(selector)) for selector in _HTML_SELECTORS},
        "forms": forms,
        "tables": tables,
        "script_markers": {
            "new_KSCase": len(re.findall(r"\bnew\s+KSCase\s*\(", text)),
            "QueryCaseDetail": text.count("QueryCaseDetail.do"),
            "targetUrl_assignment": len(re.findall(r"\btargetUrl\s*=", text)),
        },
    }


def _json_shape(value: Any, *, depth: int = 0) -> Mapping[str, Any]:
    if depth >= 3:
        return {"type": _json_type(value)}
    if isinstance(value, Mapping):
        keys = sorted({_safe_field_name(str(key)) for key in value})
        samples: dict[str, Any] = {}
        for key, item in list(value.items())[:20]:
            safe_key = _safe_field_name(str(key))
            samples[safe_key] = _json_shape(item, depth=depth + 1)
        return {
            "type": "object",
            "key_count": len(value),
            "keys": keys[:100],
            "fields": samples,
        }
    if isinstance(value, list):
        types = Counter(_json_type(item) for item in value)
        result: dict[str, Any] = {
            "type": "array",
            "length": len(value),
            "item_types": dict(sorted(types.items())),
        }
        if value:
            result["first_item"] = _json_shape(value[0], depth=depth + 1)
        return result
    return {"type": _json_type(value)}


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, list):
        return "array"
    return "other"


def _decode_sample(raw: bytes, declared: str) -> tuple[str, str]:
    encodings: list[str] = []
    if declared in {"big5", "big-5", "cp950", "ms950"}:
        encodings.extend(("cp950", "big5"))
    elif declared in {"utf-8", "utf8"}:
        encodings.append("utf-8")
    encodings.extend(("utf-8", "cp950", "big5"))
    seen: set[str] = set()
    for encoding in encodings:
        if encoding in seen:
            continue
        seen.add(encoding)
        try:
            return raw.decode(encoding, errors="strict"), encoding
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8-replace"


def _form_encoded_keys(text: str) -> list[str]:
    if "=" not in text or len(text) > 100_000:
        return []
    try:
        pairs = parse_qsl(text.strip(), keep_blank_values=True, strict_parsing=False)
    except ValueError:
        return []
    if not pairs:
        return []
    return sorted({_safe_field_name(key) for key, _ in pairs})


def _query_keys(url: str) -> list[str]:
    try:
        return [_safe_field_name(key) for key, _ in parse_qsl(urlsplit(url).query)]
    except ValueError:
        return []


def _mapping_keys(value: Any) -> list[str]:
    if isinstance(value, Mapping):
        return [_safe_field_name(str(key)) for key in value]
    if isinstance(value, (list, tuple)):
        output = []
        for item in value:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                output.append(_safe_field_name(str(item[0])))
        return output
    return []


def _safe_path(url: str) -> str:
    path = unquote(urlsplit(url).path or "/")
    output = []
    for segment in path.split("/"):
        if not segment:
            continue
        if segment in _STATIC_PATH_SEGMENTS or segment in _STATIC_ENDPOINT_SEGMENTS:
            output.append(segment)
        else:
            output.append("<redacted-segment>")
    return "/" + "/".join(output) if output else "/"


def _safe_field_name(value: str) -> str:
    looks_like_identifier_value = bool(
        re.search(r"\d{4,}", value) or re.fullmatch(r"[A-Za-z]{1,3}\d{3,}", value)
    )
    return (
        value
        if _SAFE_FIELD_RE.fullmatch(value) and not looks_like_identifier_value
        else "<redacted-field>"
    )


def _safe_code(value: str) -> str:
    return value if _SAFE_CODE_RE.fullmatch(value) else "REDACTED"


def _safe_version(value: str) -> str:
    return value if re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,39}", value) else "REDACTED"


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


def _exception_types(exc: BaseException) -> list[str]:
    output: list[str] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(output) < 6:
        seen.add(id(current))
        output.append(_safe_code(current.__class__.__name__))
        current = current.__cause__ or current.__context__
    return output


def _safe_details(details: Mapping[str, Any]) -> Mapping[str, Any]:
    output: dict[str, Any] = {}
    for key, value in details.items():
        safe_key = _safe_field_name(str(key))
        if isinstance(value, bool) or value is None or isinstance(value, int):
            output[safe_key] = value
        elif isinstance(value, float):
            output[safe_key] = round(value, 3)
        elif isinstance(value, str):
            output[safe_key] = _safe_code(value)
        elif isinstance(value, Mapping):
            output[safe_key] = _safe_details(value)
        elif isinstance(value, (list, tuple)):
            output[safe_key] = [
                _safe_code(item) if isinstance(item, str) else item
                for item in value
                if isinstance(item, (str, int, float, bool)) or item is None
            ]
        else:
            output[safe_key] = "REDACTED"
    return output


def _safe_check(check: Mapping[str, Any]) -> Mapping[str, Any]:
    return {
        "check": _safe_code(str(check.get("check", "unknown"))),
        "status": _safe_code(str(check.get("status", "UNKNOWN"))),
        "error_code": _safe_code(str(check.get("error_code", "")))
        if check.get("error_code")
        else "",
        "details": _safe_details(check.get("details", {}))
        if isinstance(check.get("details", {}), Mapping)
        else {},
    }
