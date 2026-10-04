"""Typed auth data for the public SDK."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.errors import ErrorInfo


@dataclass(frozen=True, slots=True)
class PasswordStatus:
    """Safe observed policy notice; NO_NOTICE does not assert password validity."""

    status: str = "NO_NOTICE"
    remaining_days: int | None = None
    evidence: str = ""

    def __post_init__(self) -> None:
        if self.status not in {"NO_NOTICE", "EXPIRING", "CHANGE_REQUIRED"}:
            raise ValueError("unknown password notice status")
        if self.evidence not in {
            "",
            "VISIBLE_TEXT",
            "SCRIPT_LITERAL",
            "LOGIN_CHANGE_FORM",
            "LOGIN_REDIRECT",
        }:
            raise ValueError("unknown password notice evidence")
        if self.remaining_days is not None and (
            type(self.remaining_days) is not int or not 0 <= self.remaining_days <= 36500
        ):
            raise ValueError("invalid remaining password days")

    @property
    def code(self) -> str:
        return {
            "NO_NOTICE": "",
            "EXPIRING": "PORTAL_PASSWORD_EXPIRING",
            "CHANGE_REQUIRED": "PORTAL_PASSWORD_CHANGE_REQUIRED",
        }[self.status]


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
    password_status: PasswordStatus = field(default_factory=PasswordStatus)

    @property
    def ok(self) -> bool:
        return self.status == "OK"
