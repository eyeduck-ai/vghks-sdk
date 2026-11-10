"""Rate-limited, retrying transport for all network requests."""

from __future__ import annotations

import json
import random
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import partial
from io import BytesIO
from time import monotonic
from typing import Any
from urllib.parse import urlsplit

import requests

from .browser_headers import BROWSER_LANGUAGE, BROWSER_USER_AGENT, DOCUMENT_ACCEPT
from .capture import RawCaptureSink
from .config import RequestPolicy
from .connections import TLSConnectionManager, https_origin
from .diagnostics import DiagnosticRecorder
from .errors import ParseError, RequestError
from .network_errors import network_error_code
from .response_text import decode_response_text

_BODY_CHUNK_BYTES = 64 * 1024


def content_length_exceeds(response: requests.Response, max_bytes: int) -> bool:
    declared = str(response.headers.get("Content-Length", "")).strip()
    if not declared.isascii() or not declared.isdecimal():
        return False
    # Compare decimal strings so even an overlong header can be rejected.
    declared = declared.lstrip("0")
    limit = str(max_bytes)
    return (len(declared), declared) > (len(limit), limit)


def _omit_oversize_body(response: requests.Response) -> None:
    response.close()
    response._content = b""
    response._content_consumed = True
    response.sdk_body_limit_exceeded = True


def _buffer_limited_body(response: requests.Response, *, max_bytes: int, **_: Any) -> None:
    if getattr(response, "sdk_body_limit_exceeded", False) is True:
        return
    if content_length_exceeds(response, max_bytes):
        _omit_oversize_body(response)
        return
    if getattr(response, "_content_consumed", True):
        if len(response.content or b"") > max_bytes:
            _omit_oversize_body(response)
        return

    try:
        with BytesIO() as body:
            for chunk in response.iter_content(chunk_size=_BODY_CHUNK_BYTES):
                if body.tell() + len(chunk) > max_bytes:
                    _omit_oversize_body(response)
                    return
                body.write(chunk)
            # Cache the one bounded read for Requests, Runtime and the recorders.
            response._content = body.getvalue()
            response._content_consumed = True
    except BaseException as exc:
        response.close()
        if isinstance(exc, requests.RequestException) and exc.request is None:
            exc.request = response.request
        raise


class _AnonymousAuth(requests.auth.AuthBase):
    def __call__(self, request: requests.PreparedRequest) -> requests.PreparedRequest:
        request.headers.pop("Authorization", None)
        return request


class SafeSessionTransport:
    """Serialize, delay, retry, and decode all HTTP calls.

    `retry_safe` must be explicitly true for read-only POST endpoints.  Login and
    SSO calls are not automatically retried after receiving an HTTP response.
    """

    def __init__(
        self,
        *,
        policy: RequestPolicy,
        verify: bool | str = True,
        session: requests.Session | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        diagnostics: DiagnosticRecorder | None = None,
        raw_capture: RawCaptureSink | None = None,
        connections: TLSConnectionManager | None = None,
    ) -> None:
        self.policy = policy.validate()
        self.verify = verify
        self.session = session or requests.Session()
        self.sleeper = sleeper
        self.rng = rng or random.SystemRandom()
        self.diagnostics = diagnostics
        self.raw_capture = raw_capture
        self.connections = connections
        self._lock = threading.RLock()
        # requests.Session already has a python-requests UA and */* Accept;
        # setdefault alone would leave those in place. Preserve explicit overrides.
        agent = self.session.headers.get("User-Agent", "")
        if not agent or agent.startswith("python-requests/"):
            self.session.headers["User-Agent"] = BROWSER_USER_AGENT
        if self.session.headers.get("Accept", "*/*") == "*/*":
            self.session.headers["Accept"] = DOCUMENT_ACCEPT
        self.session.headers.setdefault("Accept-Language", BROWSER_LANGUAGE)

    def request(
        self,
        method: str,
        url: str,
        *,
        retry_safe: bool = False,
        allow_retry: bool = True,
        _connection_probe: bool = False,
        _response_byte_limit: int | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        method = method.upper()
        timeout = kwargs.pop(
            "timeout",
            (
                self.policy.connect_timeout_seconds,
                self.policy.read_timeout_seconds,
            ),
        )
        # Some GET endpoints prepare a stateful external application. Their
        # callers can prohibit replay without changing other read operations.
        safe_to_repeat = allow_retry and (method == "GET" or retry_safe)
        attempts = self.policy.max_attempts if safe_to_repeat else 1
        safe_path = urlsplit(url).path or "/"
        request_kwargs = kwargs
        buffer_body = None
        if _response_byte_limit is not None:
            headers = requests.structures.CaseInsensitiveDict(kwargs.get("headers") or {})
            headers.setdefault("Accept-Encoding", "identity")
            kwargs.update(stream=True, headers=headers)
            hooks = kwargs.get("hooks") or {}
            response_hooks = hooks.get("response") or getattr(self.session, "hooks", {}).get("response", ())
            if callable(response_hooks):
                response_hooks = (response_hooks,)
            # Requests may read a redirect body while preparing Response.next
            # even with redirects disabled. Bound the read before it does so.
            buffer_body = partial(_buffer_limited_body, max_bytes=_response_byte_limit)
            request_kwargs = {**kwargs, "hooks": {**hooks, "response": [buffer_body, *response_hooks]}}

        with self._lock:
            if (
                self.connections is not None
                and method != "GET"
                and not retry_safe
                and self.connections.requires_confirmation(url)
            ):
                self._confirm_connection(url, timeout)
            tried_connections: set[tuple[Any, ...]] = set()
            for attempt in range(1, attempts + 1):
                if self.connections is not None:
                    self.connections.apply_all(self.session)
                throttle_delay = self._sleep_request_jitter()
                request_id = 0
                if self.diagnostics is not None:
                    request_id = self.diagnostics.record_http_request(
                        method=method,
                        url=url,
                        attempt=attempt,
                        max_attempts=attempts,
                        throttle_delay_seconds=throttle_delay,
                        tls_verification_enabled=self._verification_enabled(url),
                        kwargs=kwargs,
                    )
                started = monotonic()
                try:
                    response = self.session.request(
                        method,
                        url,
                        timeout=timeout,
                        verify=self.verify,
                        **request_kwargs,
                    )
                    if buffer_body is not None:
                        # Also support injected sessions that do not dispatch hooks.
                        buffer_body(response)
                except requests.RequestException as exc:
                    elapsed = monotonic() - started
                    code = network_error_code(exc)
                    will_retry = attempt < attempts
                    reason = "NETWORK_ERROR"
                    if self.connections is not None and (
                        code.startswith("TLS_") or code == "NETWORK_TLS_FAILED"
                    ):
                        self.connections.mark_failure(url, exc)
                        will_retry = will_retry and self.connections.recover(url, exc, tried_connections)
                        reason = "TLS_CONNECTION_CHANGED"
                    if _connection_probe:
                        exc.sdk_connection_probe = True
                    if self.raw_capture is not None:
                        self.raw_capture.record_network_error(
                            method=method,
                            url=url,
                            kwargs=dict(kwargs),
                            request=getattr(exc, "request", None),
                            error=exc,
                            attempt=attempt,
                            max_attempts=attempts,
                            throttle_delay_seconds=throttle_delay,
                            elapsed_seconds=elapsed,
                            will_retry=will_retry,
                        )
                    if self.diagnostics is not None:
                        self.diagnostics.record_network_error(
                            request_id=request_id,
                            exc=exc,
                            elapsed_seconds=elapsed,
                            will_retry=will_retry,
                        )
                    if not will_retry:
                        raise RequestError(
                            f"network failure for {method} {safe_path}",
                            code=code,
                            endpoint_path=safe_path,
                            attempt=attempt,
                            cause_type=exc.__class__.__name__,
                            phase="CONNECTION_PROBE" if _connection_probe else "REQUEST",
                            retry_safe=safe_to_repeat,
                            retry_recommended=code in {
                                "NETWORK_DNS_FAILED", "NETWORK_CONNECT_TIMEOUT",
                                "NETWORK_READ_TIMEOUT", "NETWORK_TIMEOUT",
                                "NETWORK_CONNECTION_FAILED", "NETWORK_REQUEST_FAILED",
                            },
                        ) from exc
                    delay = self._backoff_delay(attempt, None)
                    if self.raw_capture is not None:
                        self.raw_capture.record_retry(
                            attempt=attempt,
                            next_attempt=attempt + 1,
                            reason=reason,
                            delay_seconds=delay,
                        )
                    if self.diagnostics is not None:
                        self.diagnostics.record_retry(
                            request_id=request_id,
                            reason=reason,
                            next_attempt=attempt + 1,
                            delay_seconds=delay,
                        )
                    if delay > 0:
                        self.sleeper(delay)
                    continue

                elapsed = monotonic() - started
                if self.connections is not None:
                    self.connections.mark_response(response)
                if _connection_probe:
                    response.sdk_connection_probe = True
                will_retry = (
                    not _connection_probe
                    and response.status_code in self.policy.retry_statuses
                    and attempt < attempts
                )
                if self.raw_capture is not None:
                    self.raw_capture.record_response(
                        response=response,
                        attempt=attempt,
                        max_attempts=attempts,
                        throttle_delay_seconds=throttle_delay,
                        elapsed_seconds=elapsed,
                        will_retry=will_retry,
                    )
                if self.diagnostics is not None:
                    self.diagnostics.record_http_response(
                        request_id=request_id,
                        response=response,
                        elapsed_seconds=elapsed,
                    )

                if will_retry:
                    retry_after = response.headers.get("Retry-After")
                    response.close()
                    delay = self._backoff_delay(attempt, retry_after)
                    if self.raw_capture is not None:
                        self.raw_capture.record_retry(
                            attempt=attempt,
                            next_attempt=attempt + 1,
                            reason=f"HTTP_{response.status_code}",
                            delay_seconds=delay,
                        )
                    if self.diagnostics is not None:
                        self.diagnostics.record_retry(
                            request_id=request_id,
                            reason=f"HTTP_{response.status_code}",
                            next_attempt=attempt + 1,
                            delay_seconds=delay,
                        )
                    if delay > 0:
                        self.sleeper(delay)
                    continue

                if response.status_code >= 400 and not _connection_probe:
                    status = response.status_code
                    response.close()
                    raise RequestError(
                        f"HTTP {status} for {method} {safe_path}",
                        status_code=status,
                        code=f"HTTP_{status}",
                        endpoint_path=safe_path,
                        attempt=attempt,
                        retry_safe=safe_to_repeat,
                        retry_recommended=status in self.policy.retry_statuses,
                    )
                return response

        raise RequestError(
            f"request attempts exhausted for {method} {safe_path}",
            code="REQUEST_ATTEMPTS_EXHAUSTED",
            endpoint_path=safe_path,
            attempt=attempts,
            retry_safe=safe_to_repeat,
        )

    def text(self, response: requests.Response) -> str:
        return decode_response_text(
            response.content or b"", response.headers.get("Content-Type", ""),
            encoding_hint=getattr(response, "encoding", None),
        )[0]

    def json(self, response: requests.Response) -> Any:
        try:
            return json.loads(self.text(response))
        except json.JSONDecodeError as exc:
            raise ParseError(
                "response did not contain valid JSON",
                code="RESPONSE_JSON_INVALID",
                endpoint_path=urlsplit(str(getattr(response, "url", ""))).path,
                cause_type=exc.__class__.__name__,
            ) from exc

    def reset_cookies(self) -> None:
        self.session.cookies.clear()

    def _verification_enabled(self, url: str) -> bool:
        if self.verify is False:
            return False
        if isinstance(self.session, requests.Session):
            return getattr(self.session.get_adapter(url), "verify_certificate", True)
        return True

    def _confirm_connection(self, url: str, timeout: Any) -> None:
        """Negotiate anonymously before sending a password or an unsafe POST.

        Use no caller cookies, auth, body, headers, params or redirects. An HTTP
        error at / still proves TLS works; it says nothing about login success.
        """
        with requests.Session() as session:
            session.trust_env = self.session.trust_env
            session.auth = _AnonymousAuth()  # Suppress implicit .netrc credentials too.
            probe = SafeSessionTransport(
                policy=self.policy,
                verify=self.verify,
                session=session,
                sleeper=self.sleeper,
                rng=self.rng,
                diagnostics=self.diagnostics,
                raw_capture=self.raw_capture,
                connections=self.connections,
            )
            response = probe.request(
                "GET", https_origin(url) + "/", timeout=timeout,
                allow_redirects=False, _connection_probe=True,
            )
            response.close()

    def close(self) -> None:
        self.session.close()

    def _sleep_request_jitter(self) -> float:
        delay = self.rng.uniform(self.policy.min_delay_seconds, self.policy.max_delay_seconds)
        if delay > 0:
            self.sleeper(delay)
        return delay

    def _backoff_delay(self, attempt: int, retry_after: str | None) -> float:
        requested = self._parse_retry_after(retry_after)
        if requested is None:
            requested = self.policy.backoff_base_seconds * (2 ** (attempt - 1))
            requested += self.rng.uniform(0.0, 0.5)
        return min(requested, self.policy.max_retry_after_seconds)

    @staticmethod
    def _parse_retry_after(value: str | None) -> float | None:
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            pass
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None
