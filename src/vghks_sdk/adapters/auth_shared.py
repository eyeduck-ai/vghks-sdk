"""Shared authentication state types and transport/source validation."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ..core.config import PortalCredentials, SDKSettings
from ..core.errors import AuthenticationError, SDKError
from ..core.operations import OperationSpec
from ..core.transport import SafeSessionTransport
from ..models.auth import PasswordStatus


@dataclass(frozen=True, slots=True)
class AppSession:
    key: str
    hid: str
    landing_url: str
    landing_html: str = field(default="", repr=False)
    authentication_mode: str = ""


@dataclass(frozen=True, slots=True)
class SsoForm:
    action_url: str
    payload: dict[str, str]


class AuthContext:
    """Operations share one facade state and transport; no independent login state."""

    settings: SDKSettings
    credentials: PortalCredentials
    transport: SafeSessionTransport
    _apps: dict[str, AppSession]
    _portal_authenticated: bool
    _portal_landing_url: str
    _generation: int
    _password_status: PasswordStatus
    _webmaas_page: str
    _webmaas_recovery_page: str

    def _request(self, spec: OperationSpec, url: str, **kwargs: object):
        """Execute a bootstrap operation using the same registry policy as Runtime."""

        actual_path = urlsplit(url).path
        if actual_path != spec.path:
            raise AuthenticationError(
                "authentication operation URL did not match its registered endpoint",
                code="OPERATION_PATH_MISMATCH",
                operation=spec.key,
                app=spec.app,
                endpoint_path=actual_path,
            )
        kwargs.setdefault("retry_safe", spec.retry_safe)
        try:
            return self.transport.request(spec.method, url, **kwargs)
        except SDKError as exc:
            exc.with_context(
                operation=spec.key,
                app=spec.app,
                endpoint_path=spec.path,
            )
            raise

    @staticmethod
    def _operation_url(configured_base: str, spec: OperationSpec) -> str:
        parsed = urlsplit(configured_base)
        return f"{parsed.scheme}://{parsed.netloc}{spec.path}"

    @staticmethod
    def _validate_host(url: str, expected_base: str, label: str) -> None:
        parsed = urlsplit(url)
        expected = urlsplit(expected_base)
        if parsed.scheme.lower() != "https" or parsed.netloc.lower() != expected.netloc.lower():
            raise AuthenticationError(
                f"{label} was outside the configured internal host",
                code="AUTH_LANDING_HOST_INVALID",
                operation="auth.landing",
            )

    @staticmethod
    def _validate_base_path(url: str, expected_base: str, label: str) -> None:
        parsed = urlsplit(url)
        expected = urlsplit(expected_base)
        expected_path = expected.path.rstrip("/").lower()
        actual_path = parsed.path.rstrip("/").lower()
        if (
            parsed.scheme.lower() != "https"
            or parsed.netloc.lower() != expected.netloc.lower()
            or (
                expected_path
                and actual_path != expected_path
                and not actual_path.startswith(expected_path + "/")
            )
        ):
            raise AuthenticationError(
                f"{label} was outside the configured HTTPS base",
                code="AUTH_LANDING_BASE_INVALID",
                operation="auth.landing",
            )

    def _configured_app_base(self, app_key: str) -> str:
        return {
            "prq": self.settings.prq_base_url,
            "sectord": self.settings.sectord_base_url,
            "audit": self.settings.audit_base_url,
            "oppl": self.settings.oppl_base_url,
            "oppl_records": self.settings.oppl_base_url,
            "performance": self.settings.mis_base_url,
            "payroll": self.settings.mis_base_url,
            "personnel": self.settings.personnel_base_url,
            "attendance": self.settings.attendance_base_url,
        }[app_key]

    @staticmethod
    def _milliseconds() -> str:
        return str(int(time.time() * 1000))
