"""Recorded PCK OAuth navigation using the existing Portal account and cookies."""

from __future__ import annotations

from urllib.parse import parse_qs, unquote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from ..core.errors import AuthenticationError, ParseError
from ..parsing.review import parse_login_info


class ReviewOAuth:
    """Bounded manual redirects; never replay HAR codes/cookies or send them over HTTP."""

    def __init__(self, auth) -> None:
        self.auth = auth
        self.transport = auth.transport
        self.portal = auth.settings.portal_base_url.rstrip("/")
        self.base = auth.settings.review_base_url.rstrip("/")
        self.callback_path = "/Pck/HISLogin/SSOLoginCallBack"

    @staticmethod
    def _fail(code: str) -> None:
        raise AuthenticationError(
            "review OAuth flow did not match the supported login", code=code, app="review"
        )

    def _callback(self, value: str) -> bool:
        p, b = urlsplit(value), urlsplit(self.base)
        return (
            p.netloc.lower() == b.netloc.lower()
            and p.path == self.callback_path
            and p.scheme in {"http", "https"}
            and not p.username
            and not p.fragment
        )

    def _target(self, value: str) -> str:
        p = urlsplit(value)
        if p.username or p.fragment:
            self._fail("REVIEW_OAUTH_TARGET_INVALID")
        if self._callback(value):
            # The recorded browser used HSTS to upgrade its HTTP callback.
            # Keep redirect_uri untouched for the server's code exchange.
            return urlunsplit(p._replace(scheme="https"))
        if p.scheme != "https":
            self._fail("REVIEW_OAUTH_TARGET_INVALID")
        allowed = (
            p.netloc.lower() == urlsplit(self.portal).netloc.lower()
            and p.path in {"/oauth2Server.do", "/oauth2ServerLogin.do"}
        ) or (
            p.netloc.lower() == urlsplit(self.base).netloc.lower()
            and p.path
            in {"/Pck/HISLogin", "/Pck/HISLogin/SSOLogin", "/Pck/angular/", "/Pck/angular/bulletin"}
        )
        if not allowed:
            self._fail("REVIEW_OAUTH_TARGET_INVALID")
        return value

    def _authorize_parameters(self, url: str) -> dict[str, str]:
        raw = parse_qs(urlsplit(url).query, keep_blank_values=True)
        if any(len(v) != 1 for v in raw.values()):
            self._fail("REVIEW_OAUTH_PARAMETERS_INVALID")
        values = {k: v[0] for k, v in raw.items()}
        if (
            values.get("response_type") != "code"
            or values.get("client_id") != "pckoauth"
            or not self._callback(values.get("redirect_uri", ""))
            or not values.get("state")
        ):
            self._fail("REVIEW_OAUTH_PARAMETERS_INVALID")
        return values

    def _form(self, response, authorization: dict[str, str]) -> tuple[str, dict[str, str]]:
        soup = BeautifulSoup(self.transport.text(response), "html.parser")
        forms = [
            form
            for form in soup.find_all("form")
            if form.find("input", attrs={"name": "mpassword"})
        ]
        if len(forms) != 1:
            self._fail("REVIEW_OAUTH_FORM_UNRECORDED")
        form = forms[0]
        action = self._target(urljoin(response.url, str(form.get("action", ""))))
        if (
            urlsplit(action).path != "/oauth2ServerLogin.do"
            or urlsplit(action).netloc.lower() != urlsplit(self.portal).netloc.lower()
        ):
            self._fail("REVIEW_OAUTH_FORM_TARGET_INVALID")
        fields = {}
        for node in form.find_all(["input", "button"], attrs={"name": True}):
            if node.has_attr("disabled"):
                continue
            name, kind = str(node["name"]), str(node.get("type", "")).lower()
            if name in fields:
                self._fail("REVIEW_OAUTH_FORM_INVALID")
            if (
                node.name == "input"
                and kind not in {"hidden", "submit", "button"}
                and name not in {"muid", "mpassword"}
            ):
                self._fail("REVIEW_OAUTH_EXTRA_INPUT_REQUIRED")
            fields[name] = str(
                node.get("value", node.get_text(strip=True) if node.name == "button" else "")
            )
        for key in ("response_type", "client_id", "redirect_uri", "state"):
            if unquote(fields.get(key, "")) != unquote(authorization[key]):
                self._fail("REVIEW_OAUTH_FORM_CONTEXT_MISMATCH")
        if unquote(fields.get("oauthServer", "")).rstrip("/") != self.portal or not fields.get(
            "code"
        ):
            self._fail("REVIEW_OAUTH_FORM_NONCE_MISSING")
        fields.update(muid=self.auth.credentials.username, mpassword=self.auth.credentials.password)
        fields.setdefault("submit", "登入")
        return action, fields

    def login(self) -> tuple[str, str]:
        # /HISLogin's redirect is captured even though the first login HTML
        # bodies are absent. All hidden login fields must come from a fresh page.
        url = self.base + "/HISLogin"
        mode = "portal_session"
        authorization: dict[str, str] = {}
        submitted = False
        redeemed = False
        method, payload = "GET", None
        for _ in range(12):
            url = self._target(url)
            path = urlsplit(url).path
            if path == "/oauth2Server.do":
                authorization = self._authorize_parameters(url)
            if path == self.callback_path:
                values = parse_qs(urlsplit(url).query, keep_blank_values=True)
                if (
                    not authorization
                    or redeemed
                    or any(len(v) != 1 for v in values.values())
                    or not values.get("code", [""])[0]
                ):
                    self._fail("REVIEW_OAUTH_CALLBACK_INVALID")
                if values.get("state") and values["state"][0] != authorization["state"]:
                    self._fail("REVIEW_OAUTH_STATE_MISMATCH")
                if (
                    values.get("oauthServer", [""])[0].rstrip("/") != self.portal
                    or values.get("redirect_uri", [""])[0] != authorization["redirect_uri"]
                ):
                    self._fail("REVIEW_OAUTH_CALLBACK_CONTEXT_MISMATCH")
                redeemed = True
            response = self.transport.request(
                method,
                url,
                allow_redirects=False,
                **(
                    {
                        "data": payload,
                        "headers": {
                            "Origin": self.portal,
                            "Referer": self.portal + "/oauth2Server.do",
                        },
                    }
                    if method == "POST"
                    else {}
                ),
            )
            if method == "POST" and response.status_code in {307, 308}:
                # The recording uses 302. Do not repeat a password submission
                # against a new target or silently change its HTTP method.
                self._fail("REVIEW_OAUTH_POST_REDIRECT_UNSUPPORTED")
            method, payload = "GET", None
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                if not location:
                    self._fail("REVIEW_OAUTH_REDIRECT_MISSING")
                url = urljoin(response.url, location)
                continue
            if path in {"/oauth2Server.do", "/oauth2ServerLogin.do"}:
                if submitted or not authorization:
                    self._fail("REVIEW_OAUTH_LOGIN_REJECTED")
                url, payload = self._form(response, authorization)
                submitted, method, mode = True, "POST", "portal_credentials"
                continue
            if path in {"/Pck/HISLogin", "/Pck/HISLogin/SSOLogin"}:
                try:
                    target = self.transport.json(response)
                except ParseError:
                    target = None
                if isinstance(target, str) and target.startswith("https://"):
                    url = target
                    continue
                if path == "/Pck/HISLogin":
                    url = self.base + "/HISLogin/SSOLogin"
                    continue
            # Positive identity check is required; a 200 login shell is not success.
            info = self.transport.request(
                "GET",
                self.base + "/Menu/GetLoginInfo",
                allow_redirects=False,
                headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
            )
            if info.status_code != 200:
                self._fail("REVIEW_LOGIN_NOT_ESTABLISHED")
            parse_login_info(self.transport.json(info))
            if not any(
                cookie.name == "HIS_IPD" and cookie.value
                for cookie in self.transport.session.cookies
            ):
                self._fail("REVIEW_LOGIN_COOKIE_MISSING")
            return info.url, mode if authorization else "existing_review_session"
        self._fail("REVIEW_OAUTH_REDIRECT_LIMIT")
