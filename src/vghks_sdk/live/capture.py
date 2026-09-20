"""Full-fidelity HTTP capture used exclusively by the ``live-test`` command.

This module intentionally performs no redaction.  Its output contains enough
request state to expose or replay authenticated sessions and must therefore be
handled according to the hospital's policy.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import shutil
import stat
import threading
import traceback
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.errors import ConfigurationError, error_info

_OWNER_MARKER = ".vghks-live-test"
_OWNED_FILES = (
    _OWNER_MARKER,
    "capture_manifest.jsonl",
    "errors.jsonl",
    "run_summary.json",
)
_OWNED_DIRECTORIES = ("requests", "responses", "parsed", "diagnostics")


class RawCaptureRecorder:
    """Persist exact prepared requests and response bytes without redaction."""

    def __init__(
        self,
        output_dir: Path,
        *,
        overwrite: bool = False,
        prepare_root: bool = True,
        run_id: str | None = None,
    ) -> None:
        self.output_dir = output_dir.resolve()
        self._lock = threading.RLock()
        self._sequence = 0
        self._operation_sequence = 0
        self._error_sequence = 0
        self._request_group_sequence = 0
        self._local = threading.local()
        self.run_id = run_id or uuid.uuid4().hex
        if prepare_root:
            self._prepare_root(overwrite=overwrite)
        elif not (self.output_dir / _OWNER_MARKER).is_file():
            raise ConfigurationError("raw capture requires a live-test owned output directory")
        self.requests_dir = self.output_dir / "requests"
        self.responses_dir = self.output_dir / "responses"
        self.requests_dir.mkdir(mode=0o700, exist_ok=True)
        self.responses_dir.mkdir(mode=0o700, exist_ok=True)
        _restrict_permissions(self.requests_dir, directory=True)
        _restrict_permissions(self.responses_dir, directory=True)
        self.manifest_path = self.output_dir / "capture_manifest.jsonl"
        self._manifest = self.manifest_path.open("x", encoding="utf-8", newline="\n")
        _restrict_permissions(self.manifest_path)
        self.errors_path = self.output_dir / "errors.jsonl"
        self._errors = self.errors_path.open("x", encoding="utf-8", newline="\n")
        _restrict_permissions(self.errors_path)

    @property
    def capture_count(self) -> int:
        return self._sequence

    def set_live_step(self, name: str | None) -> None:
        self._local.live_test_step = str(name or "")

    def start_operation(self, *, name: str, app_key: str) -> str:
        with self._lock:
            self._operation_sequence += 1
            operation_id = f"op-{self._operation_sequence:06d}"
        stack = list(getattr(self._local, "operation_stack", ()))
        stack.append((operation_id, str(name), str(app_key)))
        self._local.operation_stack = stack
        return operation_id

    def finish_operation(self, operation_id: str) -> None:
        stack = list(getattr(self._local, "operation_stack", ()))
        finished = next((item for item in reversed(stack) if item[0] == operation_id), None)
        if stack and stack[-1][0] == operation_id:
            stack.pop()
        else:
            stack = [item for item in stack if item[0] != operation_id]
        self._local.operation_stack = stack
        if finished is not None:
            self._local.last_finished_operation = finished

    def record_response(
        self,
        *,
        response: Any,
        attempt: int,
        max_attempts: int,
        throttle_delay_seconds: float,
        elapsed_seconds: float,
        will_retry: bool,
    ) -> None:
        request_group_id, retry_of = self._attempt_context(attempt)
        chain = [*(getattr(response, "history", ()) or ()), response]
        for chain_index, item in enumerate(chain):
            capture_id = self._record_exchange(
                request=getattr(item, "request", None),
                response=item,
                attempt=attempt,
                max_attempts=max_attempts,
                throttle_delay_seconds=throttle_delay_seconds,
                elapsed_seconds=elapsed_seconds,
                will_retry=will_retry if chain_index == len(chain) - 1 else False,
                redirect_index=chain_index,
                redirect_count=max(0, len(chain) - 1),
                request_group_id=request_group_id,
                retry_of_capture_id=retry_of,
            )
            if chain_index == len(chain) - 1:
                self._local.last_capture_id = capture_id

    def record_network_error(
        self,
        *,
        method: str,
        url: str,
        kwargs: dict[str, Any],
        request: Any | None,
        error: BaseException,
        attempt: int,
        max_attempts: int,
        throttle_delay_seconds: float,
        elapsed_seconds: float,
        will_retry: bool,
    ) -> None:
        with self._lock:
            request_group_id, retry_of = self._attempt_context(attempt)
            capture_id = self._next_id()
            if request is not None:
                request_data = _prepared_request_data(request)
            else:
                request_data = {
                    "method": method,
                    "url": url,
                    "headers": _plain_mapping(kwargs.get("headers", {})),
                    "cookies": _unredacted_value(kwargs.get("cookies", {})),
                    "body": _request_input_body(kwargs),
                    "unprepared_kwargs": _unredacted_value(kwargs),
                }
            request_file = self._write_request(capture_id, request_data)
            self._write_manifest(
                {
                    **self._context_fields(),
                    "schema_version": 3,
                    "capture_id": capture_id,
                    "captured_at": _utc_now(),
                    "kind": "NETWORK_ERROR",
                    "connection_probe": bool(getattr(error, "sdk_connection_probe", False)),
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "throttle_delay_seconds": throttle_delay_seconds,
                    "elapsed_seconds": elapsed_seconds,
                    "will_retry": will_retry,
                    "request_group_id": request_group_id,
                    "retry_of_capture_id": retry_of,
                    "request": request_data,
                    "request_file": request_file,
                    "response": None,
                    "response_file": None,
                    "error": {
                        "type": error.__class__.__name__,
                        "message": str(error),
                        "repr": repr(error),
                    },
                }
            )
            self._local.last_capture_id = capture_id

    def close(self) -> None:
        with self._lock:
            if not self._manifest.closed:
                self._manifest.flush()
                self._manifest.close()
            if not self._errors.closed:
                self._errors.flush()
                self._errors.close()

    def record_retry(
        self,
        *,
        attempt: int,
        next_attempt: int,
        reason: str,
        delay_seconds: float,
    ) -> None:
        with self._lock:
            self._write_manifest(
                {
                    **self._context_fields(),
                    "schema_version": 3,
                    "capture_id": None,
                    "captured_at": _utc_now(),
                    "kind": "RETRY",
                    "attempt": attempt,
                    "next_attempt": next_attempt,
                    "reason": reason,
                    "delay_seconds": delay_seconds,
                    "request_group_id": getattr(self._local, "request_group_id", ""),
                    "retry_of_capture_id": getattr(self._local, "last_capture_id", None),
                }
            )

    def record_error(self, *, error: BaseException, step: str = "") -> str:
        """Cross-link an SDK/profile failure to its most recent HTTP exchange."""

        with self._lock:
            self._error_sequence += 1
            error_id = f"err-{self._error_sequence:06d}"
            info = error_info(error)
            context = self._context_fields()
            effective_step = str(step or context["live_test_step"])
            frames = [
                {
                    "file": os.path.basename(frame.filename),
                    "line": frame.lineno,
                    "function": frame.name,
                }
                for frame in traceback.extract_tb(error.__traceback__)
            ]
            chain: list[str] = []
            current: BaseException | None = error
            while current is not None and len(chain) < 8:
                chain.append(current.__class__.__name__)
                current = current.__cause__ or current.__context__
            row = {
                "schema_version": 1,
                "run_id": self.run_id,
                "error_id": error_id,
                "captured_at": _utc_now(),
                "step": effective_step,
                "sdk_operation_id": context["sdk_operation_id"],
                "sdk_operation": info.operation or context["sdk_operation"],
                "app_key": info.app or context["app_key"],
                "linked_capture_id": getattr(self._local, "last_capture_id", None),
                "issue": {
                    "code": info.code,
                    "category": info.category,
                    "operation": info.operation,
                    "app": info.app,
                    "endpoint_path": info.endpoint_path,
                    "http_status": info.http_status,
                    "attempt": info.attempt,
                    "cause_type": info.cause_type,
                },
                "exception_chain": chain,
                "stack_frames": frames,
                "locals_included": False,
            }
            self._errors.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            self._errors.write("\n")
            self._errors.flush()
            self._write_manifest(
                {
                    **context,
                    "schema_version": 3,
                    "capture_id": None,
                    "captured_at": _utc_now(),
                    "kind": "SDK_ERROR_LINK",
                    "error_id": error_id,
                    "linked_capture_id": row["linked_capture_id"],
                }
            )
            return error_id

    def __enter__(self) -> RawCaptureRecorder:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _record_exchange(
        self,
        *,
        request: Any,
        response: Any,
        attempt: int,
        max_attempts: int,
        throttle_delay_seconds: float,
        elapsed_seconds: float,
        will_retry: bool,
        redirect_index: int,
        redirect_count: int,
        request_group_id: str,
        retry_of_capture_id: str | None,
    ) -> str:
        with self._lock:
            capture_id = self._next_id()
            request_data = _prepared_request_data(request)
            request_file = self._write_request(capture_id, request_data)
            response_body = bytes(getattr(response, "content", b"") or b"")
            response_path = self.responses_dir / (f"{capture_id}{_response_extension(response)}")
            response_path.write_bytes(response_body)
            _restrict_permissions(response_path)
            response_data = {
                "status_code": int(getattr(response, "status_code", 0)),
                "tls": getattr(response, "tls_details", {}),
                "reason": str(getattr(response, "reason", "")),
                "url": str(getattr(response, "url", "")),
                "headers": _response_header_items(response),
                "cookies": _cookie_jar_values(getattr(response, "cookies", None)),
                "encoding": getattr(response, "encoding", None),
                "body_length": len(response_body),
            }
            self._write_manifest(
                {
                    **self._context_fields(),
                    "schema_version": 3,
                    "capture_id": capture_id,
                    "captured_at": _utc_now(),
                    "kind": "HTTP_EXCHANGE",
                    "connection_probe": bool(getattr(response, "sdk_connection_probe", False)),
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "throttle_delay_seconds": throttle_delay_seconds,
                    "elapsed_seconds": elapsed_seconds,
                    "will_retry": will_retry,
                    "redirect_index": redirect_index,
                    "redirect_count": redirect_count,
                    "request_group_id": request_group_id,
                    "retry_of_capture_id": retry_of_capture_id,
                    "request": request_data,
                    "request_file": request_file,
                    "response": response_data,
                    "response_file": response_path.relative_to(self.output_dir).as_posix(),
                    "error": None,
                }
            )
            return capture_id

    def _next_id(self) -> str:
        self._sequence += 1
        return f"{self._sequence:06d}"

    def _write_request(self, capture_id: str, data: Mapping[str, Any]) -> str:
        path = self.requests_dir / f"{capture_id}.json"
        _write_json(path, data)
        return path.relative_to(self.output_dir).as_posix()

    def _write_manifest(self, data: Mapping[str, Any]) -> None:
        self._manifest.write(json.dumps(data, ensure_ascii=False, sort_keys=True, default=str))
        self._manifest.write("\n")
        self._manifest.flush()

    def _context_fields(self) -> dict[str, Any]:
        stack = list(getattr(self._local, "operation_stack", ()))
        if stack:
            operation_id, operation_name, app_key = stack[-1]
        else:
            operation_id, operation_name, app_key = getattr(
                self._local,
                "last_finished_operation",
                ("", "", ""),
            )
        return {
            "run_id": self.run_id,
            "live_test_step": str(getattr(self._local, "live_test_step", "")),
            "sdk_operation_id": operation_id,
            "sdk_operation": operation_name,
            "app_key": app_key,
        }

    def _attempt_context(self, attempt: int) -> tuple[str, str | None]:
        if attempt <= 1 or not getattr(self._local, "request_group_id", ""):
            with self._lock:
                self._request_group_sequence += 1
                group = f"req-{self._request_group_sequence:06d}"
            self._local.request_group_id = group
            self._local.last_capture_id = None
        return (
            str(self._local.request_group_id),
            getattr(self._local, "last_capture_id", None) if attempt > 1 else None,
        )

    def _prepare_root(self, *, overwrite: bool) -> None:
        if self.output_dir.exists() and not self.output_dir.is_dir():
            raise ConfigurationError("live-test output path is not a directory")
        if self.output_dir.exists() and any(self.output_dir.iterdir()):
            if not overwrite:
                raise ConfigurationError(
                    "live-test output directory is not empty; use --overwrite to replace it"
                )
            marker = self.output_dir / _OWNER_MARKER
            if not marker.is_file():
                raise ConfigurationError(
                    "refusing to overwrite a directory not created by live-test"
                )
            for name in _OWNED_DIRECTORIES:
                target = self.output_dir / name
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
            for name in _OWNED_FILES:
                target = self.output_dir / name
                if target.is_file() or target.is_symlink():
                    target.unlink()
                elif target.exists():
                    raise ConfigurationError(f"live-test owned path is not a file: {name}")
        self.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        _restrict_permissions(self.output_dir, directory=True)
        marker = self.output_dir / _OWNER_MARKER
        marker.write_text("vghks-sdk live-test capture v2\n", encoding="utf-8")
        _restrict_permissions(marker)


def _prepared_request_data(request: Any) -> dict[str, Any]:
    if request is None:
        return {
            "method": "",
            "url": "",
            "headers": [],
            "cookies": [],
            "body": {"kind": "none", "text": None, "base64": None},
        }
    headers = getattr(request, "headers", {})
    return {
        "method": str(getattr(request, "method", "")),
        "url": str(getattr(request, "url", "")),
        "headers": _header_items(headers),
        "cookies": _parse_cookie_header(_get_header(headers, "Cookie")),
        "cookie_header": _get_header(headers, "Cookie"),
        "body": _body_value(getattr(request, "body", None)),
    }


def _request_input_body(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    if "json" in kwargs:
        return {"kind": "json-input", "value": _unredacted_value(kwargs["json"])}
    if "data" in kwargs:
        return {"kind": "data-input", "value": _unredacted_value(kwargs["data"])}
    return {"kind": "none", "text": None, "base64": None}


def _body_value(value: Any) -> dict[str, Any]:
    if value is None:
        return {"kind": "none", "text": None, "base64": None}
    if isinstance(value, str):
        return {"kind": "text", "text": value, "base64": None}
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            text = None
        return {
            "kind": "bytes",
            "text": text,
            "base64": base64.b64encode(raw).decode("ascii"),
        }
    return {"kind": "object", "value": _unredacted_value(value)}


def _header_items(headers: Any) -> list[list[str]]:
    if hasattr(headers, "items"):
        return [[str(key), str(value)] for key, value in headers.items()]
    return []


def _response_header_items(response: Any) -> list[list[str]]:
    raw_headers = getattr(getattr(response, "raw", None), "headers", None)
    iterator = getattr(raw_headers, "iteritems", None)
    if callable(iterator):
        try:
            return [[str(key), str(value)] for key, value in iterator()]
        except (TypeError, ValueError):
            pass
    return _header_items(getattr(response, "headers", {}))


def _plain_mapping(value: Any) -> dict[str, str]:
    if hasattr(value, "items"):
        return {str(key): str(item) for key, item in value.items()}
    return {}


def _get_header(headers: Any, name: str) -> str:
    if hasattr(headers, "get"):
        return str(headers.get(name, "") or "")
    return ""


def _parse_cookie_header(value: str) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for part in value.split(";"):
        name, separator, item = part.strip().partition("=")
        if separator:
            output.append({"name": name, "value": item})
    return output


def _cookie_jar_values(jar: Any) -> list[dict[str, Any]]:
    if jar is None:
        return []
    output: list[dict[str, Any]] = []
    try:
        cookies: Iterable[Any] = list(jar)
    except TypeError:
        return output
    for cookie in cookies:
        output.append(
            {
                "name": str(getattr(cookie, "name", "")),
                "value": str(getattr(cookie, "value", "")),
                "domain": str(getattr(cookie, "domain", "")),
                "path": str(getattr(cookie, "path", "")),
                "secure": bool(getattr(cookie, "secure", False)),
                "expires": getattr(cookie, "expires", None),
            }
        )
    return output


def _unredacted_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {
            "base64": base64.b64encode(bytes(value)).decode("ascii"),
            "length": len(value),
        }
    if isinstance(value, Mapping):
        return {str(key): _unredacted_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_unredacted_value(item) for item in value]
    return repr(value)


def _response_extension(response: Any) -> str:
    content_type = _get_header(getattr(response, "headers", {}), "Content-Type")
    mime = content_type.split(";", 1)[0].strip().lower()
    if mime in {"application/json", "text/json"} or mime.endswith("+json"):
        return ".json"
    if mime in {"text/html", "application/xhtml+xml"}:
        return ".html"
    if mime.startswith("text/") or mime in {
        "application/javascript",
        "application/x-javascript",
        "application/xml",
    }:
        return ".txt"
    return ".bin"


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    _restrict_permissions(path)


def _restrict_permissions(path: Path, *, directory: bool = False) -> None:
    mode = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR if directory else stat.S_IRUSR | stat.S_IWUSR
    # Windows ACLs and some network filesystems do not fully implement chmod.
    with contextlib.suppress(OSError):
        os.chmod(path, mode)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
