"""Safe, structured exception types used across SDK and diagnostic outputs."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from urllib.parse import urlsplit

_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
_SAFE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,79}$")


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    """Whitelisted diagnostic metadata; never contains request or patient values."""

    code: str
    category: str
    operation: str = ""
    app: str = ""
    endpoint_path: str = ""
    http_status: int | None = None
    attempt: int | None = None
    cause_type: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _normalize_code(self.code, "SDK_ERROR"))
        object.__setattr__(self, "category", _normalize_code(self.category, "INTERNAL"))
        object.__setattr__(self, "operation", _normalize_name(self.operation))
        object.__setattr__(self, "app", _normalize_name(self.app))
        object.__setattr__(self, "endpoint_path", _safe_path(self.endpoint_path))
        object.__setattr__(self, "cause_type", _normalize_name(self.cause_type))
        if self.http_status is not None:
            object.__setattr__(self, "http_status", max(0, int(self.http_status)))
        if self.attempt is not None:
            object.__setattr__(self, "attempt", max(1, int(self.attempt)))


class SDKError(Exception):
    """Base class for expected SDK failures."""

    code = "SDK_ERROR"
    category = "INTERNAL"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        category: str | None = None,
        operation: str = "",
        app: str = "",
        endpoint_path: str = "",
        http_status: int | None = None,
        attempt: int | None = None,
        cause_type: str = "",
    ) -> None:
        super().__init__(message)
        self.info = ErrorInfo(
            code=code or self.code,
            category=category or self.category,
            operation=operation,
            app=app,
            endpoint_path=endpoint_path,
            http_status=http_status,
            attempt=attempt,
            cause_type=cause_type,
        )

    def with_context(
        self,
        *,
        operation: str = "",
        app: str = "",
        endpoint_path: str = "",
        attempt: int | None = None,
    ) -> SDKError:
        """Fill missing safe context without replacing the original exception."""

        self.info = replace(
            self.info,
            operation=self.info.operation or operation,
            app=self.info.app or app,
            endpoint_path=self.info.endpoint_path or endpoint_path,
            attempt=self.info.attempt or attempt,
        )
        return self


class ConfigurationError(SDKError):
    code = "CONFIGURATION_ERROR"
    category = "CONFIGURATION"


class AuthenticationError(SDKError):
    code = "AUTHENTICATION_ERROR"
    category = "AUTHENTICATION"


class AuthExpiredError(AuthenticationError):
    code = "AUTH_EXPIRED"


class RequestError(SDKError):
    code = "REQUEST_ERROR"
    category = "NETWORK"

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        endpoint_path: str = "",
        attempt: int | None = None,
        cause_type: str = "",
    ) -> None:
        super().__init__(
            message,
            code=code,
            category="HTTP" if status_code is not None else "NETWORK",
            endpoint_path=endpoint_path,
            http_status=status_code,
            attempt=attempt,
            cause_type=cause_type,
        )
        self.status_code = status_code


class ParseError(SDKError):
    code = "PARSE_ERROR"
    category = "PARSE"


class NotFoundError(SDKError):
    code = "NOT_FOUND"
    category = "NOT_FOUND"


def error_code(exc: BaseException) -> str:
    """Return a stable, non-sensitive error code for local batch output."""

    return error_info(exc).code


def error_info(exc: BaseException | None) -> ErrorInfo:
    """Return stable, safe metadata for expected and unexpected failures."""

    if exc is None:
        return ErrorInfo("SDK_ERROR", "INTERNAL")
    info = getattr(exc, "info", None)
    if isinstance(info, ErrorInfo):
        return info
    return ErrorInfo(
        code="UNEXPECTED_ERROR",
        category="INTERNAL",
        cause_type=exc.__class__.__name__,
    )


def _normalize_code(value: str, default: str) -> str:
    candidate = str(value or "").strip().upper()
    return candidate if _SAFE_CODE.fullmatch(candidate) else default


def _normalize_name(value: str) -> str:
    candidate = str(value or "").strip()
    return candidate if _SAFE_NAME.fullmatch(candidate) else ""


def _safe_path(value: str) -> str:
    path = urlsplit(str(value or "")).path
    if not path.startswith("/") or len(path) > 240:
        return ""
    return path
