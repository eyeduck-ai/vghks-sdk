"""Portal, SSO, SectOrd, WebMAAS, and OPPL authentication adapter."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from ..core.config import AppProfile, PortalCredentials, SDKSettings
from ..core.errors import AuthenticationError, AuthExpiredError, SDKError
from ..core.operations import OperationSpec, operation_spec
from ..core.transport import SafeSessionTransport
from ..parsing.portal import parse_login_target

_PORTAL_ENTRY = operation_spec("portal.entry")
_PORTAL_LOGIN = operation_spec("portal.login")
_PORTAL_SESSION_CHECK = operation_spec("portal.session_check")
_PORTAL_SSO_LOG_ADD = operation_spec("portal.sso_log_add")
_PORTAL_APP_TREE = operation_spec("portal.app_tree")
_PORTAL_SSO_FROM_DN = operation_spec("portal.sso_from_dn")
_SECTORD_KEY_BRIDGE = operation_spec("sectord.key_bridge")
_WEBMAAS_SSO_LOGON = operation_spec("webmaas.sso_logon")
_WEBMAAS_PAGES = {
    "RSV11W001": ("RSV/RSV11W001.do", "maas_RSV11"),
    # The recorded role is QRY15 even though the URL/page id uses QUY15.
    "QUY15W001": ("QUY/QUY15W001.do", "maas_QRY15"),
}
_PROFILE_SSO_LOGON = {
    "personnel": operation_spec("personnel.sso_logon"),
    "prq": operation_spec("prq.sso_logon"),
    "sectord": operation_spec("sectord.sso_logon"),
    "audit": operation_spec("audit.sso_logon"),
    "oppl": operation_spec("oppl.sso_logon"),
    "oppl_records": operation_spec("oppl_records.sso_logon"),
    "performance": operation_spec("mis.sso_logon"),
    "payroll": operation_spec("mis.sso_logon"),
}

# Navigation headers observed in login.har. Values contain no session state.
_NAVIGATION_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
}


@dataclass(frozen=True, slots=True)
class AppSession:
    key: str
    hid: str
    landing_url: str
    landing_html: str = ""
    authentication_mode: str = ""


@dataclass(frozen=True, slots=True)
class SsoForm:
    action_url: str
    payload: dict[str, str]


class AuthenticationAdapter:
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
        self._generation = 0

    @property
    def portal_authenticated(self) -> bool:
        return self._portal_authenticated

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def portal_landing_url(self) -> str:
        return self._portal_landing_url

    def login(self, *, force: bool = False) -> None:
        if self._portal_authenticated and not force:
            return
        if force:
            self.transport.reset_cookies()
        self._apps.clear()
        self._webmaas_page = ""
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
        payload = self._login_payload(self.transport.text(entry), entry.url)
        now = self._milliseconds()
        login_url = self._operation_url(self.settings.portal_base_url, _PORTAL_LOGIN)
        response = self._request(
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
            allow_redirects=True,
        )
        text = self.transport.text(response)
        target = parse_login_target(text)
        target_url = urljoin(self.settings.portal_base_url.rstrip("/") + "/", target)
        self._validate_host(target_url, self.settings.portal_base_url, "portal login target")
        landing = self.transport.request(
            "GET",
            target_url,
            allow_redirects=True,
            headers={**_NAVIGATION_HEADERS, "Referer": response.url},
        )
        landing_text = self.transport.text(landing)
        self.assert_valid_landing(landing_text, landing.url)
        self._validate_host(landing.url, self.settings.portal_base_url, "portal login landing")
        self._portal_authenticated = True
        self._portal_landing_url = landing.url
        self._generation += 1

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

    def ensure_webmaas_page(self, page_id: str) -> AppSession:
        """Select the recorded WebMAAS SSO role inside the Runtime lock."""
        if page_id not in _WEBMAAS_PAGES:
            raise AuthenticationError(
                "unknown WebMAAS page", code="WEBMAAS_PAGE_UNKNOWN", app="webmaas"
            )
        self.login()
        if self._webmaas_page == page_id and "webmaas" in self._apps:
            return self._apps["webmaas"]
        return self._open_webmaas(page_id)

    def assert_not_expired(self, text: str, response_url: str) -> None:
        path = urlsplit(response_url).path.lower()
        lowered = text.lower()
        if path.endswith("/login.do") or "syserrorexception.jsp" in path:
            raise AuthExpiredError(
                "application session returned an authentication page",
                code="AUTH_SESSION_LOGIN_PAGE",
                operation="auth.session",
                endpoint_path=urlsplit(response_url).path,
            )
        if re.search(r"name\s*=\s*['\"]muid['\"]", lowered) and re.search(
            r"name\s*=\s*['\"]mpassword['\"]", lowered
        ):
            raise AuthExpiredError(
                "application session returned the portal login form",
                code="AUTH_SESSION_LOGIN_FORM",
                operation="auth.session",
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

    def _open_profile(self, profile: AppProfile) -> AppSession:
        portal = self.settings.portal_base_url.rstrip("/")
        log_response = self._request(
            _PORTAL_SSO_LOG_ADD,
            self._operation_url(portal, _PORTAL_SSO_LOG_ADD),
            params={"apOu": profile.app_ou, "apDesc": profile.app_description},
        )
        self.assert_valid_landing(self.transport.text(log_response), log_response.url)
        tree_response = self._request(
            _PORTAL_APP_TREE,
            self._operation_url(portal, _PORTAL_APP_TREE),
            params={"apDn": profile.app_dn, "thetime": self._milliseconds()},
        )
        self.assert_valid_landing(self.transport.text(tree_response), tree_response.url)
        response = self._request(
            _PORTAL_SSO_FROM_DN,
            self._operation_url(portal, _PORTAL_SSO_FROM_DN),
            params={"apDn": profile.app_dn},
        )
        response_text = self.transport.text(response)
        self.assert_valid_landing(response_text, response.url)
        form = self._parse_sso_form(response_text, profile)
        logon_spec = _PROFILE_SSO_LOGON[profile.key]
        posted = self._request(
            logon_spec,
            form.action_url,
            **{"params" if logon_spec.method == "GET" else "data": form.payload},
            allow_redirects=True,
        )
        landing_text = self.transport.text(posted)
        self.assert_valid_landing(landing_text, posted.url)
        self._validate_host(posted.url, profile.expected_base_url, f"{profile.key} landing")
        self._validate_base_path(
            posted.url, self._configured_app_base(profile.key), f"{profile.key} landing"
        )
        if profile.key == "personnel":
            soup = BeautifulSoup(landing_text, "html.parser")
            frames = [urljoin(posted.url, str(node.get("src", ""))) for node in soup.find_all("frame")]
            expected = self.settings.personnel_base_url.rstrip("/") + "/DRQuerySql.jsp"
            if expected not in frames:
                raise AuthenticationError(
                    "personnel landing did not contain the query frame",
                    code="PERSONNEL_LANDING_INVALID", app="personnel",
                )
        session = AppSession(
            key=profile.key,
            hid=form.payload.get("HID", ""),
            landing_url=posted.url,
            landing_html=landing_text,
        )
        if not session.hid:
            raise AuthenticationError(
                "application SSO did not provide HID",
                code="SSO_HID_MISSING",
                operation="portal.sso_from_dn",
                app=profile.key,
                endpoint_path="/ssoFromDn.do",
            )
        self._apps[profile.key] = session
        return session

    def _open_webmaas(self, page_id: str = "RSV11W001") -> AppSession:
        page_path, role = _WEBMAAS_PAGES[page_id]
        self._apps.pop("webmaas", None)
        self._webmaas_page = ""
        sectord = self.ensure("sectord")
        response = self._request(
            _SECTORD_KEY_BRIDGE,
            self._operation_url(self.settings.sectord_base_url, _SECTORD_KEY_BRIDGE),
            params={
                "CardNO": self.credentials.username,
                "_": self._milliseconds(),
                "reqCode": "getRSAInfo",
            },
        )
        response_text = self.transport.text(response)
        self.assert_valid_landing(response_text, response.url)
        bundle = dict(parse_qsl(response_text.strip(), keep_blank_values=True))
        required = {"ssID", "keyOne", "keyTwo", "keyThree"}
        if not required.issubset(bundle) or any(not bundle[key] for key in required):
            raise AuthenticationError(
                "SectOrd bridge did not return the required key bundle",
                code="SECTORD_BRIDGE_KEYS_MISSING",
                operation="sectord.key_bridge",
                app="sectord",
                endpoint_path="/SectOrdWeb/so.do",
            )
        target_url = f"{self.settings.webmaas_base_url.rstrip('/')}/{page_path}"
        params = {
            **{key: bundle[key] for key in required},
            "uid": self.credentials.username,
            "singlePage": "true",
            "externalRoles": role,
            "targetURL": target_url,
        }
        posted = self._request(
            _WEBMAAS_SSO_LOGON,
            self._operation_url(self.settings.webmaas_base_url, _WEBMAAS_SSO_LOGON),
            params=params,
            allow_redirects=True,
        )
        landing_text = self.transport.text(posted)
        self.assert_valid_landing(landing_text, posted.url)
        self._validate_host(posted.url, self.settings.webmaas_base_url, "webmaas landing")
        self._validate_base_path(posted.url, self.settings.webmaas_base_url, "webmaas landing")
        session = AppSession("webmaas", sectord.hid, posted.url)
        self._apps["webmaas"] = session
        self._webmaas_page = page_id
        return session

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

    def _parse_sso_form(self, html_text: str, profile: AppProfile) -> SsoForm:
        soup = BeautifulSoup(html_text, "html.parser")
        form = soup.find("form")
        if form is None:
            raise AuthenticationError(
                "portal SSO response did not contain a form",
                code="PORTAL_SSO_FORM_MISSING",
                operation="portal.sso_from_dn",
                app=profile.key,
                endpoint_path="/ssoFromDn.do",
            )
        payload = {
            str(node.get("name")): str(node.get("value", ""))
            for node in form.find_all("input")
            if node.get("name")
        }
        required = {"HID", "ssID", "keyOne", "keyTwo", "keyThree", "targetURL"}
        if not required.issubset(payload) or any(not payload[key] for key in required):
            raise AuthenticationError(
                "portal SSO form omitted required hidden fields",
                code="PORTAL_SSO_FIELDS_MISSING",
                operation="portal.sso_from_dn",
                app=profile.key,
                endpoint_path="/ssoFromDn.do",
            )
        if not any(payload.get(key, "") for key in ("uid", "stUsrId", "USR_ID")):
            raise AuthenticationError(
                "portal SSO form omitted the user identifier field",
                code="PORTAL_SSO_USER_FIELD_MISSING",
                operation="portal.sso_from_dn",
                app=profile.key,
                endpoint_path="/ssoFromDn.do",
            )

        target_url = unquote(payload["targetURL"])
        # OPPL returns an application-relative target (surgAction.do?...).
        # Resolve only for validation; submit the fresh form value unchanged.
        resolved_target = urljoin(
            self._configured_app_base(profile.key).rstrip("/") + "/", target_url
        )
        self._validate_host(resolved_target, profile.expected_base_url, f"{profile.key} target")
        self._validate_base_path(
            resolved_target, self._configured_app_base(profile.key), f"{profile.key} target"
        )
        payload["targetURL"] = target_url
        action = str(form.get("action") or "")
        wps_host = unquote(payload.get("wpsHost", ""))
        if action.startswith(("http://", "https://")):
            action_url = action
        elif wps_host:
            action_url = urljoin(wps_host.rstrip("/") + "/", action.lstrip("/"))
        else:
            action_url = urljoin(profile.expected_base_url.rstrip("/") + "/", action.lstrip("/"))
        self._validate_host(action_url, profile.expected_base_url, f"{profile.key} SSO action")
        return SsoForm(action_url=action_url, payload=payload)

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
        }[app_key]

    @staticmethod
    def _milliseconds() -> str:
        return str(int(time.time() * 1000))
