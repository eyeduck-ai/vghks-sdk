"""Typed auth data for the public SDK."""

from __future__ import annotations

from dataclasses import dataclass

from ..core.errors import ErrorInfo


@dataclass(frozen=True, slots=True)
class AuthCheckTarget:
    """One non-clinical authentication/landing readiness result.

    The fields are deliberately restricted so serializing this object cannot
    disclose hosts, credentials, cookie names/values, HID values, SSO tokens,
    response bodies, or patient data.
    """

    target: str
    capability: tuple[str, ...]
    landing_path: str
    hid_present: bool
    cookie_count: int
    duration_ms: float
    dependencies: tuple[str, ...]
    status: str
    issue: ErrorInfo | None = None

    @property
    def error_code(self) -> str:
        return self.issue.code if self.issue is not None else ""


@dataclass(frozen=True, slots=True)
class AuthCheckReport:
    """Typed result for a complete authentication readiness sweep."""

    schema_version: int
    sdk_version: str
    generated_at: str
    status: str
    targets: tuple[AuthCheckTarget, ...]
    reauthenticated: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "OK"
