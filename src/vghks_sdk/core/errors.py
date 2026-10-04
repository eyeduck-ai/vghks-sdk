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
    phase: str = ""
    retry_safe: bool | None = None
    retry_recommended: bool = False
    cause: ErrorInfo | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _normalize_code(self.code, "SDK_ERROR"))
        object.__setattr__(self, "category", _normalize_code(self.category, "INTERNAL"))
        object.__setattr__(self, "operation", _normalize_name(self.operation))
        object.__setattr__(self, "app", _normalize_name(self.app))
        object.__setattr__(self, "endpoint_path", _safe_path(self.endpoint_path))
        object.__setattr__(self, "cause_type", _normalize_name(self.cause_type))
        object.__setattr__(self, "phase", _normalize_code(self.phase, ""))
        if type(self.retry_safe) is not bool:
            object.__setattr__(self, "retry_safe", None)
        object.__setattr__(
            self, "retry_recommended", self.retry_safe is True and self.retry_recommended is True
        )
        if not isinstance(self.cause, ErrorInfo):
            object.__setattr__(self, "cause", None)
        if self.http_status is not None:
            object.__setattr__(self, "http_status", max(0, int(self.http_status)))
        if self.attempt is not None:
            object.__setattr__(self, "attempt", max(1, int(self.attempt)))

    @property
    def root_cause(self) -> ErrorInfo:
        """Deepest structured cause; messages and response values are excluded."""

        current = self
        seen = {id(current)}
        while current.cause is not None and id(current.cause) not in seen:
            current = current.cause
            seen.add(id(current))
        return current


class SDKError(Exception):
    """Base class for expected SDK failures."""

    code = "SDK_ERROR"
    category = "INTERNAL"
    phase = ""
    retry_safe: bool | None = None

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
        phase: str = "",
        retry_safe: bool | None = None,
        retry_recommended: bool = False,
        cause: ErrorInfo | None = None,
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
            phase=phase or self.phase,
            retry_safe=self.retry_safe if retry_safe is None else retry_safe,
            retry_recommended=retry_recommended,
            cause=cause,
        )

    def with_context(
        self,
        *,
        operation: str = "",
        app: str = "",
        endpoint_path: str = "",
        attempt: int | None = None,
        phase: str = "",
        retry_safe: bool | None = None,
    ) -> SDKError:
        """Fill missing safe context without replacing the original exception."""

        self.info = replace(
            self.info,
            operation=self.info.operation or operation,
            app=self.info.app or app,
            endpoint_path=self.info.endpoint_path or endpoint_path,
            attempt=self.info.attempt or attempt,
            phase=self.info.phase or phase,
            # Unsafe context always wins, including a write within a read query.
            retry_safe=(
                False if retry_safe is False else
                self.info.retry_safe if self.info.retry_safe is not None else retry_safe
            ),
        )
        return self


class ConfigurationError(SDKError):
    code = "CONFIGURATION_ERROR"
    category = "CONFIGURATION"
    phase = "VALIDATION"


class AuthenticationError(SDKError):
    code = "AUTHENTICATION_ERROR"
    category = "AUTHENTICATION"
    phase = "AUTHENTICATION"
    retry_safe = False


class AuthExpiredError(AuthenticationError):
    code = "AUTH_EXPIRED"


class NotAuthenticatedError(AuthenticationError):
    """An authentication challenge occurred before this SDK established a login."""

    code = "AUTH_NOT_AUTHENTICATED"


class PasswordChangeRequiredError(AuthenticationError):
    """The response explicitly requires the user to change their password."""

    code = "PORTAL_PASSWORD_CHANGE_REQUIRED"


class LoginRejectedError(AuthenticationError):
    """Login was not accepted; the response need not identify why.

    Unlike session expiry, this must never trigger automatic reauthentication.
    """

    code = "AUTH_LOGIN_REJECTED"


class AuthorizationError(SDKError):
    """A clinical access decision is required or was not accepted."""

    code = "AUTHORIZATION_ERROR"
    category = "AUTHORIZATION"
    phase = "AUTHORIZATION"
    retry_safe = False


class AccessReviewRequiredError(AuthorizationError):
    """The recorded clinical-care reason is unavailable on this PRQ review page."""

    code = "PRQ_ACCESS_REVIEW_REQUIRED"


class RequestError(SDKError):
    code = "REQUEST_ERROR"
    category = "NETWORK"

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        category: str | None = None,
        endpoint_path: str = "",
        attempt: int | None = None,
        cause_type: str = "",
        phase: str = "REQUEST",
        retry_safe: bool | None = None,
        retry_recommended: bool = False,
    ) -> None:
        super().__init__(
            message,
            code=code,
            category=category or ("HTTP" if status_code is not None else "NETWORK"),
            endpoint_path=endpoint_path,
            http_status=status_code,
            attempt=attempt,
            cause_type=cause_type,
            phase=phase,
            retry_safe=retry_safe,
            retry_recommended=retry_recommended,
        )
        self.status_code = status_code


class ParseError(SDKError):
    code = "PARSE_ERROR"
    category = "PARSE"
    phase = "PARSE"


class NotFoundError(SDKError):
    code = "NOT_FOUND"
    category = "NOT_FOUND"
    phase = "RESPONSE"


def error_code(exc: BaseException) -> str:
    """Return a stable, non-sensitive error code for local batch output."""

    return error_info(exc).code


def error_info(exc: BaseException | None) -> ErrorInfo:
    """Return stable, safe metadata for expected and unexpected failures."""

    return _exception_info(exc, set(), 0)


def _exception_info(exc: BaseException | None, seen: set[int], depth: int) -> ErrorInfo:
    if exc is None:
        return ErrorInfo("SDK_ERROR", "INTERNAL")
    seen.add(id(exc))
    info = getattr(exc, "info", None)
    if not isinstance(info, ErrorInfo):
        info = ErrorInfo(
            code="UNEXPECTED_ERROR", category="INTERNAL", cause_type=exc.__class__.__name__
        )
    linked = exc.__cause__
    # An implicit context can be the expired query being handled while login
    # is rejected; it is not the cause of that rejection. Follow explicit links.
    if (
        info.cause is None and linked is not None and id(linked) not in seen and depth < 7
        and isinstance(getattr(linked, "info", None), ErrorInfo)
    ):
        info = replace(info, cause=_exception_info(linked, seen, depth + 1))
    return info


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
