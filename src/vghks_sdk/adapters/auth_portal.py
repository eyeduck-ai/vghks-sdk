"""Portal login/session checks with the existing bounded password submission flow."""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..core.errors import (
    AuthenticationError,
    AuthExpiredError,
    LoginRejectedError,
    NotAuthenticatedError,
    RequestError,
)
from ..core.operations import operation_spec
from ..models.auth import PasswordStatus
from ..parsing.portal import (
    has_portal_login_rejection,
    is_portal_login_destination,
    parse_login_target,
)
from .auth_shared import AuthContext

_PORTAL_ENTRY = operation_spec("portal.entry")
_PORTAL_LOGIN = operation_spec("portal.login")
_PORTAL_SESSION_CHECK = operation_spec("portal.session_check")
_NAVIGATION_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
}


class PortalLogin(AuthContext):
    def login(self, *, force: bool = False) -> None:
        if self._portal_authenticated and not force:
            return
        try:
            self._login(force=force)
        except (AuthExpiredError, NotAuthenticatedError) as exc:
            # A failed login bootstrap is not expiry of an established session.
            raise AuthenticationError(
                "portal did not establish a session; credential validity is unknown",
                code="PORTAL_LOGIN_NOT_ESTABLISHED",
                operation="portal.login", app="portal",
                endpoint_path=exc.info.endpoint_path,
                cause_type=type(exc).__name__,
            ) from exc
        except RequestError as exc:
            # Repeating this login may resubmit the password, even when the
            # failing HTTP request was a safe landing-page GET.
            exc.with_context(retry_safe=False)
            if exc.status_code not in {401, 403}:
                raise
            # An HTTP denial may be an access policy, not a wrong password.
            # Never route it through Runtime's expired-query recovery.
            raise AuthenticationError(
                "portal login flow was denied; credential validity is unknown",
                code="PORTAL_LOGIN_HTTP_DENIED",
                operation="portal.login", app="portal",
                endpoint_path=exc.info.endpoint_path,
                http_status=exc.status_code,
                cause_type=type(exc).__name__,
            ) from exc

    def _login(self, *, force: bool) -> None:
        self._password_status = PasswordStatus()
        if force:
            self.transport.reset_cookies()
        self._apps.clear()
        self._webmaas_page = ""
        self._webmaas_recovery_page = ""
        self._portal_authenticated = False
        self._portal_landing_url = ""
        entry_url = self._operation_url(self.settings.portal_base_url, _PORTAL_ENTRY)
        entry = self._request(
            _PORTAL_ENTRY,
            entry_url,
            headers={**_NAVIGATION_HEADERS, "Sec-Fetch-Site": "none"},
            allow_redirects=True,
        )
        self._validate_host(entry.url, self.settings.portal_base_url, "portal entry")
        self._observe_password_status(self.transport.text(entry), entry.url)
        payload = self._login_payload(self.transport.text(entry), entry.url)
        now = self._milliseconds()
        login_url = self._operation_url(self.settings.portal_base_url, _PORTAL_LOGIN)
        posted = self._request(
            _PORTAL_LOGIN,
            login_url,
            params={"thetime": now},
            data=payload,
            headers={
                **_NAVIGATION_HEADERS,
                "Origin": self._operation_url(
                    self.settings.portal_base_url, _PORTAL_ENTRY
                ).removesuffix(_PORTAL_ENTRY.path),
                "Referer": entry.url,
            },
            allow_redirects=False,
        )
        response = self._follow_login_redirects(posted, password_post=True)
        text = self.transport.text(response)
        self._observe_password_status(text, response.url, login_stage=True)
        try:
            target = parse_login_target(text)
        except AuthenticationError as exc:
            if (
                response is posted
                or exc.info.code != "PORTAL_LOGIN_TARGET_MISSING"
                or urlsplit(response.url).path.lower() != "/myportal.do"
            ):
                raise
            # Only the known Portal destination may replace the JS navigation;
            # an arbitrary HTTP 200 after a redirect is not login evidence.
            landing = response
        else:
            target_url = urljoin(self.settings.portal_base_url.rstrip("/") + "/", target)
            self._validate_host(target_url, self.settings.portal_base_url, "portal login target")
            self._check_password_change_redirect(target_url)
            if is_portal_login_destination(target_url, response.url, self.settings.portal_base_url):
                raise LoginRejectedError(
                    "portal navigated back to its login entrance",
                    code="PORTAL_LOGIN_REJECTED", operation="portal.login", app="portal",
                )
            self.assert_valid_landing("", target_url)
            if urlsplit(target_url).path.lower() != "/myportal.do":
                raise AuthenticationError(
                    "portal login target was not the authenticated landing",
                    code="PORTAL_LOGIN_TARGET_UNRECOGNIZED", operation="portal.login", app="portal",
                )
            landing = self.transport.request(
                "GET",
                target_url,
                allow_redirects=False,
                headers={**_NAVIGATION_HEADERS, "Referer": response.url},
            )
            landing = self._follow_login_redirects(landing)
        landing_text = self.transport.text(landing)
        self._observe_password_status(landing_text, landing.url)
        if not landing_text.strip():
            raise AuthenticationError(
                "portal login landing was empty; credential validity is unknown",
                code="PORTAL_LOGIN_LANDING_EMPTY", operation="portal.login", app="portal",
            )
        if has_portal_login_rejection(landing_text) or is_portal_login_destination(
            landing.url, response.url, self.settings.portal_base_url,
        ):
            raise LoginRejectedError(
                "portal login returned to the login page; rejection reason is unknown",
                code="PORTAL_LOGIN_REJECTED",
                operation="portal.login", app="portal", endpoint_path="/login.do",
            )
        self.assert_valid_landing(landing_text, landing.url)
        self._validate_host(landing.url, self.settings.portal_base_url, "portal login landing")
        if urlsplit(landing.url).path.lower() != "/myportal.do":
            raise AuthenticationError(
                "portal login ended at an unrecognized page",
                code="PORTAL_LOGIN_TARGET_UNRECOGNIZED", operation="portal.login", app="portal",
            )
        self._portal_authenticated = True
        self._portal_landing_url = landing.url
        self._generation += 1

    def _follow_login_redirects(self, response, *, password_post: bool = False):
        """Follow bounded, same-origin GET redirects without replaying passwords."""
        for _ in range(6):
            status = getattr(response, "status_code", 200)
            if status not in {301, 302, 303, 307, 308}:
                return response
            if password_post and status in {307, 308}:
                raise AuthenticationError(
                    "portal requested a repeated password POST",
                    code="PORTAL_LOGIN_POST_REDIRECT_UNSUPPORTED",
                    operation="portal.login", app="portal", endpoint_path="/login.do",
                )
            location = response.headers.get("Location", "")
            if not location:
                raise AuthenticationError(
                    "portal login redirect omitted its destination",
                    code="PORTAL_LOGIN_REDIRECT_MISSING",
                    operation="portal.login", app="portal",
                )
            target = urljoin(response.url, location)
            self._validate_host(target, self.settings.portal_base_url, "portal login redirect")
            self._check_password_change_redirect(target)
            response = self.transport.request(
                "GET", target, allow_redirects=False,
                headers={**_NAVIGATION_HEADERS, "Referer": response.url},
            )
            password_post = False
        raise AuthenticationError(
            "portal login exceeded the redirect limit",
            code="PORTAL_LOGIN_REDIRECT_LIMIT", operation="portal.login", app="portal",
        )

    def check_portal_session(self) -> str:
        """Probe the existing Portal session without requesting clinical data."""

        self.login()
        portal = self.settings.portal_base_url.rstrip("/")
        response = self._request(
            _PORTAL_SESSION_CHECK,
            self._operation_url(portal, _PORTAL_SESSION_CHECK),
            data={"userName": self.credentials.username},
            allow_redirects=True,
        )
        response_text = self.transport.text(response)
        self.assert_valid_landing(response_text, response.url)
        self._validate_host(response.url, self.settings.portal_base_url, "portal session check")
        if not response_text.strip():
            raise AuthenticationError(
                "portal session check returned an empty response",
                code="PORTAL_SESSION_RESPONSE_EMPTY",
                operation="portal.session_check",
                app="portal",
                endpoint_path="/sessionCheck.do",
            )
        if not self.transport.session.cookies:
            raise AuthenticationError(
                "portal login did not establish a session cookie",
                code="PORTAL_SESSION_COOKIE_MISSING",
                operation="portal.session_check",
                app="portal",
                endpoint_path="/sessionCheck.do",
            )
        return response.url

    def _login_payload(self, html_text: str, entry_url: str) -> dict[str, str]:
        """Preserve current hidden fields, using this Session's login form only."""
        soup = BeautifulSoup(html_text, "html.parser")
        payload: dict[str, str] = {}
        for form in soup.find_all("form"):
            if not form.find("input", attrs={"name": "mpassword"}):
                continue
            action = urljoin(entry_url, str(form.get("action") or "/login.do"))
            self._validate_host(action, self.settings.portal_base_url, "portal login form")
            if urlsplit(action).path != _PORTAL_LOGIN.path:
                raise AuthenticationError(
                    "portal login form endpoint changed",
                    code="PORTAL_LOGIN_FORM_CHANGED",
                    operation="portal.entry",
                    app="portal",
                )
            payload = {
                str(node["name"]): str(node.get("value", ""))
                for node in form.find_all("input", attrs={"type": re.compile("^hidden$", re.I)})
                if node.get("name") and not node.has_attr("disabled")
            }
            break
        payload.update(muid=self.credentials.username, mpassword=self.credentials.password)
        payload.setdefault("ssoId2", "")
        return payload
