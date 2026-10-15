"""WebMAAS page roles, one-use landing forms and isolated SSO recovery."""

from __future__ import annotations

from dataclasses import replace
from urllib.parse import parse_qsl, urlsplit

from ..core.errors import AuthenticationError, ParseError, SDKError
from ..core.operations import operation_spec
from ..parsing.webmaas import parse_query_form
from .auth_shared import AppSession, AuthContext

_SECTORD_KEY_BRIDGE = operation_spec("sectord.key_bridge")
_WEBMAAS_SSO_LOGON = operation_spec("webmaas.sso_logon")
_WEBMAAS_PAGES = {
    "RSV11W001": ("RSV/RSV11W001.do", "maas_RSV11"),
    # The recorded role is QRY15 even though the URL/page id uses QUY15.
    "QUY15W001": ("QUY/QUY15W001.do", "maas_QRY15"),
}


class WebmaasSession(AuthContext):
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

    def take_webmaas_landing(self, page_id: str) -> str:
        """Use the SSO redirect's query page once, including its fresh form token."""
        if self._webmaas_page != page_id:
            return ""
        session = self._apps.get("webmaas")
        if session is None or not session.landing_html:
            return ""
        self._apps["webmaas"] = replace(session, landing_html="")
        return session.landing_html

    def invalidate_webmaas_session(self) -> None:
        """Discard unverified WebMAAS state without changing Portal credentials."""
        self._apps.pop("webmaas", None)
        self._webmaas_page = ""
        self._webmaas_recovery_page = ""

    def recover_webmaas_session(self) -> AppSession:
        """Renew the interrupted query's recorded SSO role without Portal login."""
        page_id = self._webmaas_recovery_page or "RSV11W001"
        self.invalidate_webmaas_session()
        return self.ensure_webmaas_page(page_id)

    def check_webmaas_session(self) -> AppSession:
        """Verify a query page returned in this check, including its form token."""
        cached = "webmaas" in self._apps
        page_id = self._webmaas_page or "RSV11W001"
        session = self.ensure_webmaas_page(page_id)
        basic = page_id == "QUY15W001"
        spec = operation_spec(
            "webmaas.basic_info_landing" if basic else "webmaas.registration_landing"
        )
        form_id = "QUY15WForm" if basic else "RSV11WForm"
        if not cached and session.landing_html:
            try:
                parse_query_form(session.landing_html, form_id)
                return session
            except ParseError as exc:
                if exc.info.code not in {"WEBMAAS_QUERY_FORM_MISSING", "WEBMAAS_QUERY_TOKEN_MISSING"}:
                    raise
        url = self._operation_url(self.settings.webmaas_base_url, spec)
        response = None
        try:
            response = self._request(spec, url)
            text = self.transport.text(response)
            self.assert_not_expired(text, response.url)
            self._validate_host(response.url, self.settings.webmaas_base_url, "WebMAAS session check")
            self._validate_base_path(response.url, self.settings.webmaas_base_url, "WebMAAS session check")
            parse_query_form(text, form_id)
        except SDKError as exc:
            self.invalidate_webmaas_session()
            exc.with_context(
                operation="webmaas.session_check", app="webmaas", endpoint_path=spec.path,
                http_status=response.status_code if response is not None else None,
            )
            raise
        # This newly verified form can serve the next query once. Retain neither
        # the stale SSO landing nor a token taken from a failed response.
        verified = replace(session, landing_url=response.url, landing_html=text)
        self._apps["webmaas"] = verified
        return verified

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
        try:
            self.assert_valid_landing(landing_text, posted.url)
        except SDKError as exc:
            exc.with_context(operation=_WEBMAAS_SSO_LOGON.key, app="webmaas",
                             http_status=getattr(posted, "status_code", None))
            raise
        self._validate_host(posted.url, self.settings.webmaas_base_url, "webmaas landing")
        self._validate_base_path(posted.url, self.settings.webmaas_base_url, "webmaas landing")
        landing_html = (
            landing_text if urlsplit(posted.url).path == urlsplit(target_url).path else ""
        )
        session = AppSession("webmaas", sectord.hid, posted.url, landing_html)
        self._apps["webmaas"] = session
        self._webmaas_page = page_id
        self._webmaas_recovery_page = ""
        return session
