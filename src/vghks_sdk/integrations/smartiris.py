"""Explicit Windows handoff to the already-running DownloadApp loopback bridge.

HTTP 200 only means that a command received an HTTP response. It does not prove
that SmartIris loaded an image. No command is retried, and no executable starts.
"""

from __future__ import annotations

import ctypes
import errno
import math
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from hashlib import sha256
from urllib.parse import urlsplit

import requests

from ..core.errors import ConfigurationError
from ..models import PacsLaunchContext
from .smartiris_availability import (
    DEFAULT_DOWNLOAD_APP_PATH,
    SmartIrisAvailability,
    _validated_path,
    check_smartiris_availability,
)

_LOCKS: dict[int, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_MAX_RESPONSE_BYTES = 64 * 1024
_OPERATION_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class SmartIrisStepResult:
    """Safe metadata only; request parameters and response bodies are excluded."""

    name: str
    status: str
    http_status: int | None = None
    response_size: int = 0
    response_sha256: str = ""
    error_code: str = ""


@dataclass(frozen=True, slots=True)
class SmartIrisLaunchResult:
    """HANDOFF_SENT is transport completion, never confirmation of loaded images."""

    status: str
    steps: tuple[SmartIrisStepResult, ...] = ()
    error_code: str = ""
    retry_safe: bool = False
    viewer_loaded: bool | None = None


@dataclass(frozen=True, slots=True)
class SmartIrisExchange:
    """Sensitive raw evidence for an explicit local-only capture callback."""

    endpoint_path: str
    request_url: str = field(repr=False)
    http_status: int | None
    response_headers: dict[str, str] = field(repr=False)
    body: bytes = field(repr=False)
    error_code: str = ""
    complete: bool = False
    method: str = "GET"


class _HandoffFailure(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@contextmanager
def _windows_mutex(port: int, timeout: float) -> Iterator[None]:
    """Coordinate cooperating SDK clients across Windows processes/sessions."""

    if os.name != "nt":
        raise _HandoffFailure("SMARTIRIS_WINDOWS_REQUIRED")
    from ctypes import wintypes

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    api.CreateMutexW.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    api.WaitForSingleObject.restype = wintypes.DWORD
    api.ReleaseMutex.argtypes = (wintypes.HANDLE,)
    api.ReleaseMutex.restype = wintypes.BOOL
    api.CloseHandle.argtypes = (wintypes.HANDLE,)
    api.CloseHandle.restype = wintypes.BOOL
    handle = api.CreateMutexW(None, False, f"Global\\VghksSDK.SmartIris.{port}")
    if not handle:
        raise _HandoffFailure("SMARTIRIS_LOCK_UNAVAILABLE")
    acquired = False
    try:
        outcome = api.WaitForSingleObject(handle, math.ceil(timeout * 1000))
        acquired = outcome in (0, 0x80)
        if outcome == 0x80:
            # A previous owner died mid-sequence. Do not assume its local state.
            raise _HandoffFailure("SMARTIRIS_LOCK_ABANDONED")
        if outcome == 0x102:
            raise _HandoffFailure("SMARTIRIS_LOCK_TIMEOUT")
        if outcome != 0:
            raise _HandoffFailure("SMARTIRIS_LOCK_UNAVAILABLE")
        yield
    finally:
        if acquired:
            api.ReleaseMutex(handle)
        api.CloseHandle(handle)


def _bounded_seconds(value: object, name: str, maximum: float, *, zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"invalid SmartIris {name}", code="SMARTIRIS_CONFIG_INVALID")
    number = float(value)
    if not math.isfinite(number) or number > maximum or number < 0 or (not zero and number == 0):
        raise ConfigurationError(f"invalid SmartIris {name}", code="SMARTIRIS_CONFIG_INVALID")
    return number


def _bridge_address(value: str) -> tuple[str, int]:
    try:
        parsed = urlsplit(value)
        port = parsed.port
        valid = (
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost"}
            and parsed.netloc in {f"127.0.0.1:{port}", f"localhost:{port}"}
            and port is not None
            and 1 <= port <= 65535
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
            and not any(character.isspace() for character in value)
        )
    except (TypeError, ValueError, AttributeError):
        valid = False
        port = None
    if not valid or port is None:
        raise ConfigurationError("invalid SmartIris bridge address", code="SMARTIRIS_URL_INVALID")
    # Avoid DNS and coordinate localhost/127.0.0.1 callers using one mutex name.
    return f"http://127.0.0.1:{port}", port


def _refused_connection(exc: BaseException) -> bool:
    pending = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, OSError) and (
            current.errno in {errno.ECONNREFUSED, 61, 10061}
            or getattr(current, "winerror", None) == 10061
        ):
            return True
        for linked in (current.__cause__, current.__context__,
                       getattr(current, "reason", None), *current.args):
            if isinstance(linked, BaseException):
                pending.append(linked)
    return False


class SmartIrisClient:
    """Send one explicit, serialized four-command handoff on Windows.

    The default port is DownloadApp's recorded 15516. An explicit alternate
    loopback port is intended for localhost test servers. ``session`` injection
    must be a dedicated, unauthenticated Session, never the Portal Session.
    The client reconfigures and owns that Session. Its previous transport
    adapters are detached immediately and closed when the client is closed.
    Construction performs no network or Windows API calls. ``capture`` receives
    raw sensitive data and must only save it locally. KeyboardInterrupt stops
    immediately and propagates; ``last_result`` retains the partial outcome.
    The mutex coordinates participating SDK clients, not unrelated vendor pages.
    """

    def __init__(
        self,
        *,
        bridge_url: str = "http://127.0.0.1:15516",
        timeout: tuple[float, float] = (3.0, 10.0),
        lock_timeout: float = 10.0,
        capture: Callable[[SmartIrisExchange], None] | None = None,
        session: requests.Session | None = None,
        download_app_path: str = DEFAULT_DOWNLOAD_APP_PATH,
    ) -> None:
        self._base, self._port = _bridge_address(bridge_url)
        self._download_app_path = _validated_path(download_app_path)
        if not isinstance(timeout, tuple) or len(timeout) != 2:
            raise ConfigurationError("invalid SmartIris timeout", code="SMARTIRIS_CONFIG_INVALID")
        self._timeout = tuple(_bounded_seconds(v, "timeout", 30.0) for v in timeout)
        self._lock_timeout = _bounded_seconds(lock_timeout, "lock timeout", 60.0, zero=True)
        if capture is not None and not callable(capture):
            raise ConfigurationError("invalid SmartIris capture", code="SMARTIRIS_CONFIG_INVALID")
        self._capture = capture
        self._session = session if session is not None else requests.Session()
        if not isinstance(self._session, requests.Session) or (
            self._session.auth or self._session.cookies
            or any(key.casefold() in {"authorization", "cookie", "proxy-authorization"}
                   for key in self._session.headers)
        ):
            raise ConfigurationError(
                "SmartIris requires a dedicated unauthenticated Session",
                code="SMARTIRIS_SESSION_NOT_ISOLATED",
            )
        self._session.trust_env = False
        self._session.headers.clear()
        self._session.headers["User-Agent"] = "vghks-sdk-smartiris"
        self._session.proxies.clear()
        self._session.params.clear()
        self._session.hooks = {"response": []}
        # Requests selects the longest prefix: replacing http:// alone leaves
        # a host/path-specific adapter able to apply its own retry policy.
        # Keep each displaced adapter for close(), without constructor I/O.
        self._displaced_adapters = tuple(
            {id(adapter): adapter for adapter in self._session.adapters.values()}.values()
        )
        self._session.adapters.clear()
        self._session.mount("http://", requests.adapters.HTTPAdapter(max_retries=0))
        self._session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
        self._client_lock = threading.RLock()
        with _LOCKS_GUARD:
            self._bridge_lock = _LOCKS.setdefault(self._port, threading.Lock())
        self._closed = False
        self._last_timestamp = 0
        self.last_result: SmartIrisLaunchResult | None = None

    def check_availability(self) -> SmartIrisAvailability:
        """Read Windows file/process/listener evidence; never contact the bridge."""
        return check_smartiris_availability(download_app_path=self._download_app_path, port=self._port)

    def __enter__(self) -> SmartIrisClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        with self._client_lock:
            if not self._closed:
                self._closed = True
                with ExitStack() as cleanup:
                    for adapter in self._displaced_adapters:
                        cleanup.callback(adapter.close)
                    self._displaced_adapters = ()
                    self._session.close()

    def open_study(self, context: PacsLaunchContext) -> SmartIrisLaunchResult:
        if type(context) is not PacsLaunchContext:
            raise ConfigurationError("PACS launch context required", code="SMARTIRIS_CONTEXT_INVALID")
        context = replace(context)  # Revalidate even a modified frozen instance.
        started = time.monotonic()
        client_acquired = False
        handoff_started = False
        try:
            client_acquired = self._client_lock.acquire(timeout=self._lock_timeout)
            if not client_acquired:
                return self._save_result((), "FAILED", "SMARTIRIS_LOCK_TIMEOUT")
            if self._closed:
                raise ConfigurationError("SmartIris client is closed", code="SMARTIRIS_CLOSED")
            remaining = max(0.0, self._lock_timeout - (time.monotonic() - started))
            if not self._bridge_lock.acquire(timeout=remaining):
                return self._save_result((), "FAILED", "SMARTIRIS_LOCK_TIMEOUT")
            try:
                remaining = max(0.0, self._lock_timeout - (time.monotonic() - started))
                with _windows_mutex(self._port, remaining):
                    handoff_started = True
                    return self._open_locked(context)
            except _HandoffFailure as exc:
                return self._save_result((), "FAILED", exc.code)
            finally:
                self._bridge_lock.release()
        except KeyboardInterrupt:
            if not handoff_started:
                self._save_result((), "FAILED", "SMARTIRIS_INTERRUPTED")
            raise
        finally:
            if client_acquired:
                self._client_lock.release()

    def _save_result(
        self, steps: tuple[SmartIrisStepResult, ...], status: str, code: str = ""
    ) -> SmartIrisLaunchResult:
        self.last_result = SmartIrisLaunchResult(status, steps, code)
        return self.last_result

    def _open_locked(self, context: PacsLaunchContext) -> SmartIrisLaunchResult:
        commands = (
            ("login", {"id": context.login_id, "pwd": ""}),
            ("wonderurl", {"url": context.wonder_url + "/"}),
            ("setanonymousmode", {"mode": "0"}),
            ("cmove", {"cmoveurl": context.cmove_url, "url": context.wado_url,
                       "dicomserver": context.dicom_server, "level": context.level,
                       "uid": context.study_uid, "command": "1"}),
        )
        steps: list[SmartIrisStepResult] = []
        deadline = time.monotonic() + _OPERATION_SECONDS
        try:
            for name, parameters in commands:
                self._last_timestamp = max(self._last_timestamp + 1, int(time.time() * 1000))
                parameters["timestamp"] = str(self._last_timestamp)
                step, interrupted = self._send(name, parameters, deadline)
                steps.append(step)
                if step.status != "HTTP_RESPONSE_RECEIVED":
                    status = ("FAILED" if step.status == "NOT_SENT" and len(steps) == 1
                              else "OUTCOME_UNKNOWN")
                    result = self._save_result(tuple(steps), status, step.error_code)
                    if interrupted:
                        raise KeyboardInterrupt
                    return result
        except KeyboardInterrupt:
            # Also retain progress for an interrupt between completed commands.
            self._save_result(tuple(steps), "OUTCOME_UNKNOWN", "SMARTIRIS_INTERRUPTED")
            raise
        return self._save_result(tuple(steps), "HANDOFF_SENT")

    def _send(
        self, name: str, parameters: dict[str, str], deadline: float
    ) -> tuple[SmartIrisStepResult, bool]:
        endpoint = "/" + name
        url = self._base + endpoint
        prepared = requests.Request("GET", url, params=parameters).prepare()
        request_url = prepared.url or url
        body = bytearray()
        response: requests.Response | None = None
        status_code = None
        headers: dict[str, str] = {}
        code = ""
        step_status = "OUTCOME_UNKNOWN"
        complete = False
        interrupted = False
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _HandoffFailure("SMARTIRIS_TIME_BUDGET_EXCEEDED")
            self._session.cookies.clear()
            response = self._session.request(
                "GET", url, params=parameters, allow_redirects=False, stream=True,
                timeout=tuple(min(value, remaining) for value in self._timeout),
                proxies={}, headers={"Authorization": None, "Cookie": None},
            )
            status_code = response.status_code
            headers = dict(response.headers)
            length = str(response.headers.get("Content-Length", ""))
            if length.isdecimal() and int(length) > _MAX_RESPONSE_BYTES:
                raise _HandoffFailure("SMARTIRIS_RESPONSE_TOO_LARGE")
            # A one-byte iterator lets the wall-clock budget cover slow streams,
            # in addition to Requests' bounded connect/read inactivity timeouts.
            for part in response.iter_content(chunk_size=1):
                if time.monotonic() > deadline:
                    raise _HandoffFailure("SMARTIRIS_TIME_BUDGET_EXCEEDED")
                if len(body) + len(part) > _MAX_RESPONSE_BYTES:
                    raise _HandoffFailure("SMARTIRIS_RESPONSE_TOO_LARGE")
                body.extend(part)
            complete = True
            if 300 <= status_code < 400:
                code = "SMARTIRIS_REDIRECT_REJECTED"
            elif status_code != 200:
                code = "SMARTIRIS_HTTP_ERROR"
            else:
                step_status = "HTTP_RESPONSE_RECEIVED"
        except KeyboardInterrupt:
            code, interrupted = "SMARTIRIS_INTERRUPTED", True
        except requests.ConnectTimeout:
            step_status, code = "NOT_SENT", "SMARTIRIS_CONNECT_TIMEOUT"
        except requests.RequestException as exc:
            if _refused_connection(exc):
                step_status, code = "NOT_SENT", "SMARTIRIS_CONNECTION_REFUSED"
            elif isinstance(exc, requests.Timeout):
                code = "SMARTIRIS_TIMEOUT"
            else:
                code = "SMARTIRIS_TRANSPORT_ERROR"
        except _HandoffFailure as exc:
            code = exc.code
        finally:
            if response is not None:
                response.close()
            self._session.cookies.clear()
        if self._capture is not None:
            try:
                self._capture(SmartIrisExchange(
                    endpoint, request_url, status_code, headers, bytes(body), code, complete,
                ))
            except KeyboardInterrupt:
                step_status, code, interrupted = "OUTCOME_UNKNOWN", "SMARTIRIS_INTERRUPTED", True
            except Exception:
                step_status, code = "OUTCOME_UNKNOWN", "SMARTIRIS_CAPTURE_FAILED"
        return SmartIrisStepResult(
            name, step_status, status_code, len(body),
            sha256(body).hexdigest() if complete or body else "", code,
        ), interrupted
