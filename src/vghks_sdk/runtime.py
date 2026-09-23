"""Shared serialized runtime for authentication, requests, and operation tracing."""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from time import monotonic
from typing import Protocol, TypeVar
from urllib.parse import urlsplit

import requests

from .core.capture import RawCaptureSink
from .core.config import PortalCredentials, SDKSettings
from .core.diagnostics import DiagnosticRecorder
from .core.errors import (
    AuthenticationError,
    AuthExpiredError,
    ErrorInfo,
    ParseError,
    RequestError,
    SDKError,
    error_info,
)
from .core.operations import OperationSpec
from .core.readiness import AuthCheckSpec, make_auth_report, resolve_auth_targets
from .core.transport import SafeSessionTransport
from .models import AuthCheckReport, AuthCheckTarget
from .parsing.portal import is_portal_login_destination

T = TypeVar("T")


class AuthSessionProtocol(Protocol):
    hid: str
    landing_url: str
    landing_html: str


class AuthenticationProtocol(Protocol):
    credentials: PortalCredentials
    portal_landing_url: str
    generation: int

    def login(self, *, force: bool = False) -> None: ...

    def check_portal_session(self) -> str: ...

    def ensure(self, app_key: str) -> AuthSessionProtocol: ...

    def ensure_webmaas_page(self, page_id: str) -> AuthSessionProtocol: ...

    def hid_for(self, app_key: str) -> str: ...

    def assert_not_expired(self, text: str, response_url: str) -> None: ...


class SDKRuntime:
    """Own the one session, lock, authentication state, and diagnostic lifecycle."""

    def __init__(
        self,
        *,
        settings: SDKSettings,
        transport: SafeSessionTransport,
        auth: AuthenticationProtocol,
        diagnostics: DiagnosticRecorder | None = None,
        raw_capture: RawCaptureSink | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        if diagnostics is not None:
            self.transport.diagnostics = diagnostics
        if raw_capture is not None:
            self.transport.raw_capture = raw_capture
        self.diagnostics = diagnostics or getattr(self.transport, "diagnostics", None)
        self.raw_capture = raw_capture or getattr(self.transport, "raw_capture", None)
        self.auth = auth
        self.operation_lock = threading.RLock()

    def login(self) -> None:
        def operation() -> None:
            with self.operation_lock:
                self.auth.login()

        self.trace_direct("login", "portal", operation)

    def auth_check(self, only: Sequence[str] | None = None) -> AuthCheckReport:
        specs = resolve_auth_targets(only)

        def operation() -> AuthCheckReport:
            with self.operation_lock:
                for attempt in range(2):
                    targets, authentication_expired = self._run_auth_check_sweep(
                        specs,
                        force_login=attempt == 1,
                    )
                    if authentication_expired and attempt == 0:
                        continue
                    return make_auth_report(targets, reauthenticated=attempt == 1)
                raise AuthenticationError(
                    "authentication readiness sweep did not complete",
                    code="AUTH_READINESS_INCOMPLETE",
                    operation="auth_check",
                    app="portal",
                )

        return self.trace_direct(
            "auth_check",
            "portal",
            operation,
            reauthentication_check=lambda report: report.reauthenticated,
        )

    def execute(
        self,
        spec: OperationSpec,
        operation: Callable[[], T],
        *,
        operation_name: str,
    ) -> T:
        return self._execute(
            spec.app,
            operation,
            operation_name=operation_name,
            allow_reauthentication=not spec.mutates,
        )

    def request_text(self, spec: OperationSpec, url: str, **kwargs: object) -> str:
        response = self.request_response(spec, url, **kwargs)
        return self.transport.text(response)

    def request_json(self, spec: OperationSpec, url: str, **kwargs: object) -> object:
        response = self.request_response(spec, url, **kwargs)
        return self.transport.json(response)

    def request_binary(
        self,
        spec: OperationSpec,
        url: str,
        *,
        max_bytes: int,
        **kwargs: object,
    ) -> tuple[bytes, str]:
        response = self.request_response(spec, url, **kwargs)
        content_length = str(response.headers.get("Content-Length", "")).strip()
        if content_length.isdecimal() and int(content_length) > max_bytes:
            response.close()
            raise RequestError(
                "binary response exceeded the per-file size limit",
                code="BINARY_ASSET_TOO_LARGE",
                endpoint_path=spec.path,
            )
        content = response.content or b""
        media_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0].strip()
        response.close()
        if len(content) > max_bytes:
            raise RequestError(
                "binary response exceeded the per-file size limit",
                code="BINARY_ASSET_TOO_LARGE",
                endpoint_path=spec.path,
            )
        return content, media_type

    def request_response(
        self,
        spec: OperationSpec,
        url: str,
        **kwargs: object,
    ) -> requests.Response:
        actual_path = urlsplit(url).path
        if actual_path != spec.path:
            raise RequestError(
                "operation URL did not match its registered endpoint",
                code="OPERATION_PATH_MISMATCH",
                endpoint_path=actual_path,
            )
        json_redirect_guard = (
            not spec.mutates and spec.response_kind == "json" and "allow_redirects" not in kwargs
        )
        if spec.mutates:
            # A response lost after a clinical write is not proof of failure.
            # Never resend it through redirects, transport retry, or re-login.
            kwargs["retry_safe"] = False
            kwargs["allow_redirects"] = False
        else:
            kwargs.setdefault("retry_safe", spec.retry_safe)
            if json_redirect_guard:
                # A JSON query may redirect to the legacy HTTP login entrance
                # when cookies expire. Detect it before Requests follows it.
                kwargs.setdefault("allow_redirects", False)
        try:
            response = self.transport.request(spec.method, url, **kwargs)
        except RequestError as exc:
            if exc.status_code in {401, 403}:
                raise AuthExpiredError(
                    "application returned an authentication status",
                    operation=spec.key,
                    app=spec.app,
                    endpoint_path=spec.path,
                ) from exc
            exc.with_context(
                operation=spec.key,
                app=spec.app,
                endpoint_path=spec.path,
            )
            raise
        if (not spec.mutates and 300 <= getattr(response, "status_code", 200) < 400
                and not kwargs.get("allow_redirects", True)):
            location = response.headers.get("Location", "")
            if location and is_portal_login_destination(location, response.url, self.settings.portal_base_url):
                raise AuthExpiredError(
                    "query redirected to the portal login entrance",
                    code="AUTH_SESSION_REDIRECT", operation=spec.key, app=spec.app,
                    endpoint_path=spec.path,
                )
            if json_redirect_guard:
                raise ParseError(
                    "JSON query returned an unrecognized redirect",
                    code="QUERY_REDIRECT_UNRECOGNIZED", operation=spec.key, app=spec.app,
                    endpoint_path=spec.path,
                )
        self._raise_if_expired_response(response)
        return response

    def close(self) -> None:
        self.transport.close()

    def _run_auth_check_sweep(
        self,
        specs: Sequence[AuthCheckSpec],
        *,
        force_login: bool,
    ) -> tuple[list[AuthCheckTarget], bool]:
        targets: list[AuthCheckTarget] = []
        statuses: dict[str, str] = {}
        portal_spec = specs[0]
        started = monotonic()
        try:
            self.auth.login(force=force_login)
            session_url = self.auth.check_portal_session()
            target = self._auth_target_result(
                portal_spec,
                status="OK",
                landing_url=session_url or self.auth.portal_landing_url,
                hid_present=False,
                started=started,
            )
        except Exception as exc:
            if self._is_authentication_expiry(exc) and not force_login:
                return [], True
            target = self._auth_target_result(
                portal_spec,
                status="ERROR",
                started=started,
                exc=exc,
            )
            targets.append(target)
            statuses[portal_spec.key] = target.status
            for spec in specs[1:]:
                blocked = self._auth_target_result(
                    spec,
                    status="BLOCKED",
                    error="DEPENDENCY_FAILED",
                )
                targets.append(blocked)
                statuses[spec.key] = blocked.status
            return targets, False

        targets.append(target)
        statuses[portal_spec.key] = target.status
        for index, spec in enumerate(specs[1:], start=1):
            if any(statuses.get(item) != "OK" for item in spec.dependencies):
                blocked = self._auth_target_result(
                    spec,
                    status="BLOCKED",
                    error="DEPENDENCY_FAILED",
                )
                targets.append(blocked)
                statuses[spec.key] = blocked.status
                continue
            started = monotonic()
            try:
                app_session = self.auth.ensure(spec.key)
                result = self._auth_target_result(
                    spec,
                    status="OK",
                    landing_url=app_session.landing_url,
                    hid_present=bool(app_session.hid),
                    started=started,
                )
            except Exception as exc:
                if self._is_authentication_expiry(exc):
                    if not force_login:
                        return [], True
                    result = self._auth_target_result(
                        spec,
                        status="ERROR",
                        started=started,
                        exc=exc,
                    )
                    targets.append(result)
                    statuses[spec.key] = result.status
                    for remaining in specs[index + 1 :]:
                        blocked = self._auth_target_result(
                            remaining,
                            status="BLOCKED",
                            error="AUTHENTICATION_STOPPED",
                        )
                        targets.append(blocked)
                        statuses[remaining.key] = blocked.status
                    return targets, False
                result = self._auth_target_result(
                    spec,
                    status="ERROR",
                    started=started,
                    exc=exc,
                )
            targets.append(result)
            statuses[spec.key] = result.status
        return targets, False

    def _auth_target_result(
        self,
        spec: AuthCheckSpec,
        *,
        status: str,
        landing_url: str = "",
        hid_present: bool = False,
        started: float | None = None,
        error: str = "",
        exc: BaseException | None = None,
    ) -> AuthCheckTarget:
        duration_ms = round((monotonic() - started) * 1000.0, 3) if started is not None else 0.0
        return AuthCheckTarget(
            target=spec.key,
            capability=spec.capability,
            landing_path=urlsplit(landing_url).path if landing_url else "",
            hid_present=hid_present,
            cookie_count=len(self.transport.session.cookies),
            duration_ms=duration_ms,
            dependencies=spec.dependencies,
            status=status,
            issue=(
                error_info(exc)
                if exc is not None
                else ErrorInfo(error, "AUTHENTICATION")
                if error
                else None
            ),
        )

    @staticmethod
    def _is_authentication_expiry(exc: BaseException) -> bool:
        return isinstance(exc, AuthExpiredError) or (
            isinstance(exc, RequestError) and exc.status_code in {401, 403}
        )

    def _raise_if_expired_response(self, response: object) -> None:
        text = self.transport.text(response)  # type: ignore[arg-type]
        url = str(getattr(response, "url", ""))
        self.auth.assert_not_expired(text, url)

    def _execute(
        self,
        app_key: str,
        operation: Callable[[], T],
        *,
        operation_name: str,
        allow_reauthentication: bool = True,
    ) -> T:
        diagnostics = self.diagnostics
        operation_id = (
            diagnostics.start_operation(name=operation_name, app_key=app_key)
            if diagnostics is not None
            else 0
        )
        raw_capture = self.raw_capture
        raw_operation_id = (
            raw_capture.start_operation(name=operation_name, app_key=app_key)
            if raw_capture is not None
            else ""
        )
        try:
            with self.operation_lock:
                for attempt in range(2):
                    try:
                        self.auth.ensure(app_key)
                        result = operation()
                        self._finish_operation(
                            operation_id,
                            raw_operation_id,
                            operation_name,
                            status="OK",
                        )
                        return result
                    except (AuthExpiredError, RequestError) as exc:
                        if not allow_reauthentication or not self._is_authentication_expiry(exc):
                            raise
                        if attempt == 1:
                            raise AuthenticationError(
                                "application remained expired after one re-login",
                                code="AUTH_RELOGIN_FAILED",
                                operation=operation_name,
                                app=app_key,
                            ) from exc
                        if diagnostics is not None:
                            diagnostics.record_reauthentication(
                                operation_id=operation_id,
                                app_key=app_key,
                            )
                        try:
                            self.auth.login(force=True)
                        except AuthenticationError:
                            # Preserve rejection/unknown-login codes for the application;
                            # they are not another expired query and must not be replayed.
                            raise
                        except Exception as login_exc:
                            raise AuthenticationError(
                                "re-login failed while recovering an expired session",
                                code="AUTH_RELOGIN_FAILED",
                                operation=operation_name,
                                app=app_key,
                                cause_type=login_exc.__class__.__name__,
                            ) from login_exc
                raise AuthenticationError(
                    "application operation did not complete",
                    code="AUTH_OPERATION_INCOMPLETE",
                    operation=operation_name,
                    app=app_key,
                )
        except Exception as exc:
            if isinstance(exc, SDKError):
                exc.with_context(operation=operation_name, app=app_key)
            self._finish_operation(
                operation_id,
                raw_operation_id,
                operation_name,
                status="ERROR",
                exc=exc,
            )
            raise

    def trace_direct(
        self,
        name: str,
        app_key: str,
        operation: Callable[[], T],
        *,
        reauthentication_check: Callable[[T], bool] | None = None,
    ) -> T:
        diagnostics = self.diagnostics
        operation_id = (
            diagnostics.start_operation(name=name, app_key=app_key)
            if diagnostics is not None
            else 0
        )
        raw_capture = self.raw_capture
        raw_operation_id = (
            raw_capture.start_operation(name=name, app_key=app_key)
            if raw_capture is not None
            else ""
        )
        try:
            result = operation()
        except Exception as exc:
            if isinstance(exc, SDKError):
                exc.with_context(operation=name, app=app_key)
            self._finish_operation(
                operation_id,
                raw_operation_id,
                name,
                status="ERROR",
                exc=exc,
            )
            raise
        if (
            diagnostics is not None
            and reauthentication_check is not None
            and reauthentication_check(result)
        ):
            diagnostics.record_reauthentication(
                operation_id=operation_id,
                app_key=app_key,
            )
        self._finish_operation(
            operation_id,
            raw_operation_id,
            name,
            status="OK",
        )
        return result

    def _finish_operation(
        self,
        operation_id: int,
        raw_operation_id: str,
        name: str,
        *,
        status: str,
        exc: BaseException | None = None,
    ) -> None:
        if self.diagnostics is not None:
            self.diagnostics.finish_operation(
                operation_id=operation_id,
                name=name,
                status=status,
                exc=exc,
            )
        if self.raw_capture is not None:
            self.raw_capture.finish_operation(raw_operation_id)
