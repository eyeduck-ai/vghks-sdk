"""Authentication facade and shared session challenge classification."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ..core.config import PortalCredentials, SDKSettings
from ..core.errors import (
    ApplicationSessionExpiredError,
    AuthenticationError,
    AuthExpiredError,
    NotAuthenticatedError,
    PasswordChangeRequiredError,
)
from ..core.transport import SafeSessionTransport
from ..models.auth import PasswordStatus
from ..parsing.portal import (
    has_portal_login_form,
    has_portal_login_redirect,
    is_password_change_destination,
    parse_password_status,
)
from ..parsing.webmaas import is_webmaas_session_timeout
from .auth_portal import PortalLogin
from .auth_shared import AppSession as AppSession
from .auth_shared import SsoForm as SsoForm
from .auth_sso import ApplicationSso
from .auth_webmaas import WebmaasSession


class AuthenticationAdapter(PortalLogin, ApplicationSso, WebmaasSession):
    """Compatible facade for one Portal/SSO state, Session and transport."""

    def __init__(
        self,
        *,
        settings: SDKSettings,
        credentials: PortalCredentials,
        transport: SafeSessionTransport,
    ) -> None:
        self.settings = settings
        self.credentials = credentials.validate()
        self.transport = transport
        self._portal_authenticated = False
        self._portal_landing_url = ""
        self._apps: dict[str, AppSession] = {}
        self._webmaas_page = ""
        self._webmaas_recovery_page = ""
        self._generation = 0
        self._password_status = PasswordStatus()

    @property
    def portal_authenticated(self) -> bool:
        return self._portal_authenticated

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def password_status(self) -> PasswordStatus:
        return self._password_status

    def _observe_password_status(self, text: str, url: str, *, login_stage: bool = False) -> None:
        status = parse_password_status(text, login_stage=login_stage)
        if status.status != "NO_NOTICE":
            self._password_status = status
        if status.status == "CHANGE_REQUIRED":
            raise PasswordChangeRequiredError(
                "password change is required by the returned page",
                operation="portal.login" if login_stage else "auth.session", app="portal",
                endpoint_path=urlsplit(url).path,
            )

    def _check_password_change_redirect(self, target: str) -> None:
        if is_password_change_destination(target, self.settings.portal_base_url,
                                          self.settings.portal_base_url):
            self._password_status = PasswordStatus("CHANGE_REQUIRED", evidence="LOGIN_REDIRECT")
            raise PasswordChangeRequiredError(
                "portal login requires password change", operation="portal.login",
                app="portal", endpoint_path="/login.do",
            )

    @property
    def portal_landing_url(self) -> str:
        return self._portal_landing_url

    def ensure(self, app_key: str) -> AppSession:
        if app_key in self._apps:
            return self._apps[app_key]
        self.login()
        if app_key in self.settings.profiles:
            # Both capabilities share the OPPL session cookie. Opening either
            # invalidates the cached HID/landing for the other SSO entry.
            if app_key in {"oppl", "oppl_records"}:
                self._apps.pop("oppl_records" if app_key == "oppl" else "oppl", None)
            return self._open_profile(self.settings.profiles[app_key])
        if app_key == "webmaas":
            return self._open_webmaas()
        if app_key == "review":
            from .review_auth import ReviewOAuth

            landing, mode = ReviewOAuth(self).login()
            session = AppSession("review", "", landing, authentication_mode=mode)
            self._apps["review"] = session
            return session
        raise AuthenticationError(
            "unknown application profile",
            code="AUTH_APP_PROFILE_UNKNOWN",
            operation="auth.ensure",
            app="portal",
        )

    def hid_for(self, app_key: str) -> str:
        session = self.ensure(app_key)
        if not session.hid:
            raise AuthenticationError(
                "application did not provide HID",
                code="AUTH_APP_HID_MISSING",
                operation="auth.hid_for",
                app=app_key,
            )
        return session.hid

    def assert_not_expired(self, text: str, response_url: str) -> None:
        if text:
            self._observe_password_status(text, response_url)
        if text and is_webmaas_session_timeout(text, response_url, self.settings.webmaas_base_url):
            page_id = self._webmaas_page
            self.invalidate_webmaas_session()
            self._webmaas_recovery_page = page_id
            error_type = ApplicationSessionExpiredError if self._portal_authenticated else NotAuthenticatedError
            raise error_type(
                "WebMAAS returned its explicit session timeout page",
                code="WEBMAAS_SESSION_TIMEOUT" if self._portal_authenticated else "AUTH_NOT_AUTHENTICATED",
                app="webmaas", endpoint_path=urlsplit(response_url).path,
            )
        error_type = AuthExpiredError if self._portal_authenticated else NotAuthenticatedError
        path = urlsplit(response_url).path.lower()
        if path.endswith("/login.do") or "syserrorexception.jsp" in path:
            raise error_type(
                "application session returned an authentication page",
                code="AUTH_SESSION_LOGIN_PAGE" if self._portal_authenticated else "AUTH_NOT_AUTHENTICATED",
                operation="auth.session",
                endpoint_path=urlsplit(response_url).path,
            )
        if not text:
            return
        if has_portal_login_form(text):
            raise error_type(
                "application session returned the portal login form",
                code="AUTH_SESSION_LOGIN_FORM" if self._portal_authenticated else "AUTH_NOT_AUTHENTICATED",
                operation="auth.session",
                endpoint_path=urlsplit(response_url).path,
            )
        if has_portal_login_redirect(text, response_url, self.settings.portal_base_url):
            raise error_type(
                "application returned automatic navigation to the portal entrance",
                code="AUTH_SESSION_REDIRECT" if self._portal_authenticated else "AUTH_NOT_AUTHENTICATED", operation="auth.session",
                endpoint_path=urlsplit(response_url).path,
            )

    def assert_valid_landing(self, text: str, response_url: str) -> None:
        """Reject login redirects and recognizable 2xx application error pages."""

        self.assert_not_expired(text, response_url)
        path = urlsplit(response_url).path.lower()
        if re.search(r"(?:^|/)(?:error|exception)(?:[./]|$)", path):
            raise AuthenticationError(
                "application landing returned an error page",
                code="AUTH_LANDING_ERROR_PAGE",
                operation="auth.landing",
            )
        if "<html" not in text[:2048].lower():
            return
        title = BeautifulSoup(text, "html.parser").find("title")
        title_text = title.get_text(" ", strip=True).casefold() if title else ""
        if any(
            marker in title_text
            for marker in ("system error", "application error", "系統錯誤", "發生錯誤")
        ):
            raise AuthenticationError(
                "application landing returned an error page",
                code="AUTH_LANDING_ERROR_PAGE",
                operation="auth.landing",
            )
