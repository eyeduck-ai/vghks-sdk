"""Narrow interface for optional, deliberately unredacted HTTP capture.

The core transport depends only on this protocol.  The concrete filesystem
implementation lives in :mod:`vghks_sdk.live.capture` so normal SDK usage does
not create capture files or depend on the live-test package.
"""

from __future__ import annotations

from typing import Any, Protocol


class RawCaptureSink(Protocol):
    """Receive one completed transport attempt without redaction."""

    def set_live_step(self, name: str | None) -> None: ...

    def start_operation(self, *, name: str, app_key: str) -> str: ...

    def finish_operation(self, operation_id: str) -> None: ...

    def record_response(
        self,
        *,
        response: Any,
        attempt: int,
        max_attempts: int,
        throttle_delay_seconds: float,
        elapsed_seconds: float,
        will_retry: bool,
    ) -> None: ...

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
    ) -> None: ...

    def record_retry(
        self,
        *,
        attempt: int,
        next_attempt: int,
        reason: str,
        delay_seconds: float,
    ) -> None: ...

    def record_error(self, *, error: BaseException, step: str = "") -> str: ...
