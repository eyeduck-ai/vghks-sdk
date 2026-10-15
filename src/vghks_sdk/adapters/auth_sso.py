"""Application SSO and attendance landing checks sharing the facade's state."""

from __future__ import annotations

from dataclasses import replace
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from ..core.config import AppProfile
from ..core.errors import AuthenticationError, AuthExpiredError, ParseError
from ..core.operations import operation_spec
from ..parsing.attendance import parse_attendance_state, parse_attendance_wait_target
from ..parsing.portal import is_portal_login_destination
from .auth_shared import AppSession, AuthContext, SsoForm

_PORTAL_SSO_LOG_ADD = operation_spec("portal.sso_log_add")
_PORTAL_APP_TREE = operation_spec("portal.app_tree")
_PORTAL_SSO_FROM_DN = operation_spec("portal.sso_from_dn")
_PROFILE_SSO_LOGON = {
    "attendance": operation_spec("attendance.sso_logon"),
    "personnel": operation_spec("personnel.sso_logon"),
    "prq": operation_spec("prq.sso_logon"),
    "sectord": operation_spec("sectord.sso_logon"),
    "audit": operation_spec("audit.sso_logon"),
    "oppl": operation_spec("oppl.sso_logon"),
    "oppl_records": operation_spec("oppl_records.sso_logon"),
    "performance": operation_spec("mis.sso_logon"),
    "payroll": operation_spec("mis.sso_logon"),
}


class ApplicationSso(AuthContext):
    def take_attendance_landing(self) -> str:
        """Consume the already verified status page once within the Runtime lock."""
        session = self._apps.get("attendance")
        if session is None or not session.landing_html:
            return ""
        self._apps["attendance"] = replace(session, landing_html="")
        return session.landing_html

    def check_attendance_session(self) -> AppSession:
        if "attendance" not in self._apps:
            return self.ensure("attendance")
        spec = operation_spec("attendance.status")
        response = self._request(
            spec, self._operation_url(self.settings.attendance_base_url, spec),
            params=dict(spec.operation_values), allow_redirects=False,
        )
        text = self.transport.text(response)
        self.assert_valid_landing(text, response.url)
        if response.status_code != 200:
            location = response.headers.get("Location", "")
            if is_portal_login_destination(location, response.url, self.settings.portal_base_url):
                raise AuthExpiredError("attendance redirected to Portal", app="attendance")
            raise ParseError("attendance status returned an unrecognized redirect", code="ATTENDANCE_REDIRECT_UNRECOGNIZED")
        parse_attendance_state(text, expected_employee_id=self.credentials.username)
        session = replace(self._apps["attendance"], landing_url=response.url, landing_html=text)
        self._apps["attendance"] = session
        return session

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
        if profile.key == "attendance":
            expected = self.settings.attendance_base_url.rstrip("/") + "/oFSchedule.do?reqCode=getPCClockInLog"
            target = urljoin(self.settings.attendance_base_url.rstrip("/") + "/", form.payload["targetURL"])
            if target != expected or form.payload.get("USR_ID", "").casefold() != self.credentials.username.strip().casefold():
                raise AuthenticationError("attendance SSO target or account did not match", code="ATTENDANCE_SSO_INVALID", app="attendance")
        logon_spec = _PROFILE_SSO_LOGON[profile.key]
        posted = self._request(
            logon_spec,
            form.action_url,
            **{"params" if logon_spec.method == "GET" else "data": form.payload},
            allow_redirects=profile.key != "attendance",
        )
        if profile.key == "attendance":
            posted = self._finish_attendance_sso(posted)
        landing_text = self.transport.text(posted)
        self.assert_valid_landing(landing_text, posted.url)
        self._validate_host(posted.url, profile.expected_base_url, f"{profile.key} landing")
        self._validate_base_path(
            posted.url, self._configured_app_base(profile.key), f"{profile.key} landing"
        )
        if profile.key == "personnel":
            soup = BeautifulSoup(landing_text, "html.parser")
            frames = [urljoin(posted.url, str(node.get("src", "")))
                      for node in soup.find_all(("frame", "iframe"))]
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

    def _finish_attendance_sso(self, response):
        """Bounded GET transitions only; never replay the SSO POST on 307/308."""
        base = self.settings.attendance_base_url
        if response.status_code == 302:
            destination = urljoin(response.url, response.headers.get("Location", ""))
            self._validate_host(destination, base, "attendance SSO redirect")
            actual = urlsplit(destination)
            expected_target = "/PSPDPortal/oFSchedule.do?reqCode=getPCClockInLog"
            if (actual.path != "/PSPDPortal/access_wait.jsp" or actual.fragment
                    or parse_qsl(actual.query, keep_blank_values=True) != [("targetURL", expected_target)]):
                raise AuthenticationError("attendance SSO redirect was unrecognized", code="ATTENDANCE_SSO_REDIRECT_INVALID", app="attendance")
            response = self._request(operation_spec("attendance.sso_wait"), destination, allow_redirects=False)
        if response.status_code != 200:
            raise AuthenticationError("attendance SSO did not reach a read page", code="ATTENDANCE_SSO_REDIRECT_INVALID", app="attendance")
        text = self.transport.text(response)
        self.assert_valid_landing(text, response.url)
        if urlsplit(response.url).path == "/PSPDPortal/access_wait.jsp":
            destination = parse_attendance_wait_target(text, response_url=response.url, base_url=base)
            response = self._request(operation_spec("attendance.status"), destination, allow_redirects=False)
            text = self.transport.text(response)
            self.assert_valid_landing(text, response.url)
        actual = urlsplit(response.url)
        self._validate_host(response.url, base, "attendance landing")
        if (response.status_code != 200 or actual.path != "/PSPDPortal/oFSchedule.do"
                or parse_qsl(actual.query, keep_blank_values=True) != [("reqCode", "getPCClockInLog")]):
            raise AuthenticationError("attendance SSO landing was unrecognized", code="ATTENDANCE_LANDING_INVALID", app="attendance")
        parse_attendance_state(text, expected_employee_id=self.credentials.username)
        return response

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
