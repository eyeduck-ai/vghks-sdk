from __future__ import annotations

import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from urllib.parse import urlsplit

from vghks_sdk import PortalCredentials, SDKSettings
from vghks_sdk.adapters.auth import AppSession, AuthenticationAdapter
from vghks_sdk.core.errors import AuthenticationError
from vghks_sdk.runtime import SDKRuntime


@dataclass
class FakeResponse:
    url: str
    body: str


class ScriptedTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.session = SimpleNamespace(cookies={"JSESSIONID": "fixture"})

    def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        path = urlsplit(url).path
        if path.endswith("/login.do"):
            return FakeResponse(url, "<script>var targetUrl='myPortal.do?thetime=1';</script>")
        if path.endswith("/myPortal.do"):
            return FakeResponse(url, "portal")
        if path.endswith("/ssoFromDn.do"):
            app_dn = str(dict(kwargs.get("params", {})).get("apDn", ""))
            if "011911_06" in app_dn:
                origin = "https://zwmc01p.vghks.gov.tw:4430"
                app_path = "/SectOrdWeb"
                hid = "SECTORD-HID"
            elif "0108_04" in app_dn or "010801_04" in app_dn:
                origin = "https://zwmc01p.vghks.gov.tw:4430"
                app_path = "/OPPLWeb"
                hid = "OPPL-HID"
            elif "010501_01" in app_dn:
                origin = "https://wmc01p.vghks.gov.tw:4439"
                app_path = "/PRQWeb"
                hid = "AUDIT-HID"
            elif "02060101_06" in app_dn:
                origin = "https://wac01p.vghks.gov.tw:4430"
                app_path = "/DDPortal"
                hid = "PERSONNEL-HID"
            else:
                origin = "https://zwmc01p.vghks.gov.tw:4434"
                app_path = "/PRQWeb"
                hid = "1A0"
            body = f"""
            <form action="{origin}{app_path}/WPSAutoLogon">
              <input name="HID" value="{hid}"><input name="ssID" value="synthetic">
              <input name="keyOne" value="1"><input name="keyTwo" value="2">
              <input name="keyThree" value="3"><input name="uid" value="U001">
              <input name="targetURL" value="{origin}{app_path}/landing.do">
            </form>
            """
            return FakeResponse(url, body)
        if path.endswith("/WPSAutoLogon"):
            values = kwargs.get("data") or kwargs.get("params") or {}
            target_url = str(dict(values).get("targetURL", url))
            return FakeResponse(
                target_url,
                '<frameset><frame src="DRQuerySql.jsp"></frameset>'
                if "/DDPortal/" in target_url else "application",
            )
        if path.endswith("/SectOrdWeb/so.do"):
            return FakeResponse(url, "ssID=s&keyOne=1&keyTwo=2&keyThree=3")
        return FakeResponse(url, "ok")

    def text(self, response: FakeResponse) -> str:
        return response.body

    def reset_cookies(self) -> None:
        pass


class AuthTests(unittest.TestCase):
    def test_login_loads_entry_in_same_session_and_preserves_hidden_fields(self) -> None:
        class EntryTransport(ScriptedTransport):
            def request(self, method, url, **kwargs):
                response = super().request(method, url, **kwargs)
                if url.endswith("/index.do"):
                    self.session.cookies["ENTRY"] = "fresh-cookie"
                    response.body = """<form action="login.do" method="post">
                        <input name="mpassword" type="password">
                        <input name="csrf" type="hidden" value="fresh-token">
                        <input name="ssoId2" type="hidden" value="fresh-sso">
                        <input name="ignored" type="hidden" value="x" disabled>
                        </form>"""
                if (
                    url.endswith("/login.do")
                    and self.session.cookies.get("ENTRY") != "fresh-cookie"
                ):
                    raise AssertionError("entry session was lost")
                return response

        transport = EntryTransport()
        auth = AuthenticationAdapter(
            settings=SDKSettings(),
            credentials=PortalCredentials("U001", "synthetic-password"),
            transport=transport,
        )
        auth.login()
        self.assertEqual(
            [urlsplit(call[1]).path for call in transport.calls],
            ["/index.do", "/login.do", "/myPortal.do"],
        )
        post = transport.calls[1][2]
        self.assertEqual(
            post["data"],
            {
                "muid": "U001",
                "mpassword": "synthetic-password",
                "ssoId2": "fresh-sso",
                "csrf": "fresh-token",
            },
        )
        self.assertEqual(post["headers"]["Origin"], "https://portal.vghks.gov.tw")
        self.assertEqual(post["headers"]["Referer"], "https://portal.vghks.gov.tw/index.do")
        self.assertIn("text/html", post["headers"]["Accept"])
        self.assertEqual(post["headers"]["Sec-Fetch-Mode"], "navigate")
        auth.login()
        self.assertEqual(len(transport.calls), 3)

    def test_blank_login_response_does_not_guess_landing_or_retry_credentials(self) -> None:
        class BlankLogin(ScriptedTransport):
            def request(self, method, url, **kwargs):
                response = super().request(method, url, **kwargs)
                if url.endswith("/login.do"):
                    response.body = "\n"
                return response

        transport = BlankLogin()
        auth = AuthenticationAdapter(
            settings=SDKSettings(),
            credentials=PortalCredentials("U001", "synthetic-password"),
            transport=transport,
        )
        with self.assertRaises(AuthenticationError) as failure:
            auth.login()
        self.assertEqual(failure.exception.info.code, "PORTAL_LOGIN_RESPONSE_EMPTY")
        self.assertEqual([call[0] for call in transport.calls], ["GET", "POST"])
        self.assertFalse(auth.portal_authenticated)

    def test_changed_login_form_does_not_submit_credentials(self) -> None:
        class ChangedEntry(ScriptedTransport):
            def request(self, method, url, **kwargs):
                response = super().request(method, url, **kwargs)
                response.body = '<form action="https://elsewhere.invalid/login.do"><input name="mpassword"></form>'
                return response

        transport = ChangedEntry()
        auth = AuthenticationAdapter(
            settings=SDKSettings(),
            credentials=PortalCredentials("U001", "synthetic-password"),
            transport=transport,
        )
        with self.assertRaises(AuthenticationError):
            auth.login()
        self.assertEqual([call[0] for call in transport.calls], ["GET"])

    def test_login_and_prq_sso_copy_dynamic_hidden_fields(self) -> None:
        transport = ScriptedTransport()
        auth = AuthenticationAdapter(
            settings=SDKSettings(),
            credentials=PortalCredentials("U001", "synthetic-password"),
            transport=transport,  # type: ignore[arg-type]
        )
        app = auth.ensure("prq")
        self.assertEqual(app.hid, "1A0")
        sso_posts = [
            call
            for call in transport.calls
            if call[0] == "POST" and call[1].endswith("WPSAutoLogon")
        ]
        self.assertEqual(len(sso_posts), 1)
        payload = sso_posts[0][2]["data"]
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["ssID"], "synthetic")
        self.assertEqual(auth.generation, 1)

    def test_portal_readiness_posts_session_check_and_requires_cookie(self) -> None:
        transport = ScriptedTransport()
        auth = AuthenticationAdapter(
            settings=SDKSettings(),
            credentials=PortalCredentials("U001", "synthetic-password"),
            transport=transport,  # type: ignore[arg-type]
        )
        auth.login()
        session_url = auth.check_portal_session()
        self.assertTrue(session_url.endswith("/sessionCheck.do"))
        probes = [call for call in transport.calls if call[1].endswith("/sessionCheck.do")]
        self.assertEqual(len(probes), 1)
        self.assertEqual(probes[0][0], "POST")
        self.assertEqual(probes[0][2]["data"], {"userName": "U001"})

        transport.session.cookies.clear()
        with self.assertRaises(AuthenticationError):
            auth.check_portal_session()

    def test_oppl_readiness_uses_its_own_fresh_sso_and_hid(self) -> None:
        transport = ScriptedTransport()
        auth = AuthenticationAdapter(
            settings=SDKSettings(),
            credentials=PortalCredentials("U001", "synthetic-password"),
            transport=transport,  # type: ignore[arg-type]
        )
        auth._portal_authenticated = True
        auth._apps["sectord"] = AppSession(
            "sectord", "SECTORD-HID", "https://zwmc01p.vghks.gov.tw:4430/SectOrdWeb/"
        )
        result = auth.ensure("oppl")
        self.assertEqual(result.hid, "OPPL-HID")
        probes = [call for call in transport.calls if call[1].endswith("/OPPLWeb/WPSAutoLogon")]
        self.assertEqual(len(probes), 1)
        self.assertEqual(probes[0][0], "POST")
        self.assertEqual(probes[0][2]["data"]["HID"], "OPPL-HID")
        self.assertEqual(probes[0][2]["data"]["uid"], "U001")
        self.assertNotIn("params", probes[0][2])
        self.assertFalse(
            any(call[1].endswith("/OPPLWeb/surgAction.do") for call in transport.calls)
        )

    def test_oppl_readiness_rejects_empty_or_html_doctor_reply(self) -> None:
        for body in ("", "<html><body>session unavailable</body></html>"):
            with self.subTest(body=body):
                transport = ScriptedTransport()
                auth = AuthenticationAdapter(
                    settings=SDKSettings(),
                    credentials=PortalCredentials("U001", "synthetic-password"),
                    transport=transport,
                )
                auth._portal_authenticated = True
                auth._apps["sectord"] = AppSession("sectord", "TEST-HID", "")
                from unittest.mock import patch

                with (
                    patch.object(transport, "text", return_value=body),
                    self.assertRaises(AuthenticationError),
                ):
                    auth.ensure("oppl")
                self.assertNotIn("oppl", auth._apps)

    def test_full_registry_reaches_every_sso_and_landing_with_hid(self) -> None:
        from test_review import OAuthFixture

        oauth = OAuthFixture()

        class FullTransport(ScriptedTransport):
            def request(self, method, url, **kwargs):
                if urlsplit(url).path.startswith(("/Pck/", "/oauth2Server")):
                    self.calls.append((method, url, kwargs))
                    return oauth.dispatch(method, url, **kwargs)
                return super().request(method, url, **kwargs)

            def text(self, response):
                return (
                    response.body
                    if isinstance(response, FakeResponse)
                    else oauth.transport.text(response)
                )

            def json(self, response):
                return oauth.transport.json(response)

        transport = FullTransport()
        transport.session = oauth.session
        transport.session.cookies.set("JSESSIONID", "synthetic-only")
        settings = SDKSettings()
        credentials = PortalCredentials("U001", "synthetic-password")
        auth = AuthenticationAdapter(
            settings=settings,
            credentials=credentials,
            transport=transport,  # type: ignore[arg-type]
        )
        context = SDKRuntime(
            settings=settings,
            transport=transport,  # type: ignore[arg-type]
            auth=auth,
        )
        report = context.auth_check()
        self.assertTrue(report.ok)
        self.assertEqual(
            [row.target for row in report.targets],
            ["portal", "prq", "sectord", "webmaas", "oppl", "audit", "oppl_records", "review", "personnel"],
        )
        self.assertFalse(report.targets[0].hid_present)
        self.assertTrue(all(row.hid_present for row in report.targets if row.target not in {"portal", "review"}))
        self.assertFalse(next(row for row in report.targets if row.target == "review").hid_present)
        paths = {row.target: row.landing_path for row in report.targets}
        self.assertTrue(paths["prq"].startswith("/PRQWeb/"))
        self.assertTrue(paths["sectord"].startswith("/SectOrdWeb/"))
        self.assertTrue(paths["webmaas"].startswith("/webmaas/"))
        self.assertTrue(paths["oppl"].startswith("/OPPLWeb/"))
        self.assertTrue(paths["audit"].startswith("/PRQWeb/"))


if __name__ == "__main__":
    unittest.main()
