"""Rate-limited, retrying transport for all network requests."""

from __future__ import annotations

import json
import random
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from time import monotonic
from typing import Any
from urllib.parse import urlsplit

import requests

from .browser_headers import BROWSER_LANGUAGE, BROWSER_USER_AGENT, DOCUMENT_ACCEPT
from .capture import RawCaptureSink
from .config import RequestPolicy
from .diagnostics import DiagnosticRecorder
from .errors import ParseError, RequestError
from .network_errors import network_error_code

_CHARSET_RE = re.compile(r"charset\s*=\s*['\"]?([^;\s'\"]+)", re.IGNORECASE)


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
    ) -> None:
        self.policy = policy.validate()
        self.verify = verify
        self.session = session or requests.Session()
        self.sleeper = sleeper
        self.rng = rng or random.SystemRandom()
        self.diagnostics = diagnostics
        self.raw_capture = raw_capture
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
        attempts = self.policy.max_attempts if (method == "GET" or retry_safe) else 1
        safe_path = urlsplit(url).path or "/"

        with self._lock:
            for attempt in range(1, attempts + 1):
                throttle_delay = self._sleep_request_jitter()
                request_id = 0
                if self.diagnostics is not None:
                    request_id = self.diagnostics.record_http_request(
                        method=method,
                        url=url,
                        attempt=attempt,
                        max_attempts=attempts,
                        throttle_delay_seconds=throttle_delay,
                        tls_verification_enabled=self.verify is not False,
                        kwargs=kwargs,
                    )
                started = monotonic()
                try:
                    response = self.session.request(
                        method,
                        url,
                        timeout=timeout,
                        verify=self.verify,
                        **kwargs,
                    )
                except requests.RequestException as exc:
                    elapsed = monotonic() - started
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
                            will_retry=attempt < attempts,
                        )
                    if self.diagnostics is not None:
                        self.diagnostics.record_network_error(
                            request_id=request_id,
                            exc=exc,
                            elapsed_seconds=elapsed,
                            will_retry=attempt < attempts,
                        )
                    if attempt >= attempts:
                        raise RequestError(
                            f"network failure for {method} {safe_path}",
                            code=network_error_code(exc),
                            endpoint_path=safe_path,
                            attempt=attempt,
                            cause_type=exc.__class__.__name__,
                        ) from exc
                    delay = self._backoff_delay(attempt, None)
                    if self.raw_capture is not None:
                        self.raw_capture.record_retry(
                            attempt=attempt,
                            next_attempt=attempt + 1,
                            reason="NETWORK_ERROR",
                            delay_seconds=delay,
                        )
                    if self.diagnostics is not None:
                        self.diagnostics.record_retry(
                            request_id=request_id,
                            reason="NETWORK_ERROR",
                            next_attempt=attempt + 1,
                            delay_seconds=delay,
                        )
                    if delay > 0:
                        self.sleeper(delay)
                    continue

                elapsed = monotonic() - started
                will_retry = (
                    response.status_code in self.policy.retry_statuses and attempt < attempts
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

                if response.status_code in self.policy.retry_statuses and attempt < attempts:
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

                if response.status_code >= 400:
                    status = response.status_code
                    response.close()
                    raise RequestError(
                        f"HTTP {status} for {method} {safe_path}",
                        status_code=status,
                        code=f"HTTP_{status}",
                        endpoint_path=safe_path,
                        attempt=attempt,
                    )
                return response

        raise RequestError(
            f"request attempts exhausted for {method} {safe_path}",
            code="REQUEST_ATTEMPTS_EXHAUSTED",
            endpoint_path=safe_path,
            attempt=attempts,
        )

    def text(self, response: requests.Response) -> str:
        content = response.content or b""
        content_type = response.headers.get("Content-Type", "")
        match = _CHARSET_RE.search(content_type)
        declared = match.group(1).strip().lower() if match else ""
        if declared in {"big5", "big-5", "cp950", "ms950"}:
            encodings = ("cp950", "big5", "utf-8")
        elif declared in {"utf-8", "utf8"}:
            encodings = ("utf-8", "cp950")
        elif (response.encoding or "").lower() in {"iso-8859-1", "latin1", "latin-1"}:
            encodings = ("utf-8", "cp950", response.encoding)
        elif response.encoding:
            encodings = (response.encoding, "utf-8", "cp950")
        else:
            encodings = ("utf-8", "cp950")

        tried: set[str] = set()
        for encoding in encodings:
            normalized = encoding.lower()
            if normalized in tried:
                continue
            tried.add(normalized)
            try:
                return content.decode(encoding, errors="strict")
            except (UnicodeDecodeError, LookupError):
                continue
        return content.decode("utf-8", errors="replace")

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
