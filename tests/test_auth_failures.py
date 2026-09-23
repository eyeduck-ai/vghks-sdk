"""Synthetic login failures through the real Runtime and Requests transport."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock
from urllib.parse import urlsplit

import requests
from test_auth import ScriptedTransport

from vghks_sdk import (
    AuthenticationError,
    AuthExpiredError,
    LoginRejectedError,
    PortalCredentials,
    RequestPolicy,
    SDKSettings,
    VghksSDK,
)
from vghks_sdk.core.operations import operation_spec
from vghks_sdk.core.transport import SafeSessionTransport
from vghks_sdk.models import to_jsonable
from vghks_sdk.offline.replay import replay_response
from vghks_sdk.parsing.portal import has_portal_login_form, parse_login_target

LOGIN_FORM = '<form><input name="muid"><input type="password" name="mpassword"></form>'
SECRET = "synthetic-password-not-in-diagnostics"
DENIED_PAGE = "<h3>登入失敗</h3><p>帳號或密碼錯誤</p><button onclick=\"location.href='logout.do'\">重新登入</button>"


def reply(body: str = "", status: int = 200, location: str = "") -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result._content = body.encode("utf-8")
    result._content_consumed = True
    result.headers["Content-Type"] = "text/html; charset=utf-8"
    if location:
        result.headers["Location"] = location
    return result


class PortalFixture:
    def __init__(self, overrides=None):
        self.overrides = overrides or {}
        self.script = ScriptedTransport()
        self.session = requests.Session()
        self.session.request = MagicMock(side_effect=self.dispatch)
        policy = RequestPolicy(
            min_delay_seconds=0,
            max_delay_seconds=0,
            backoff_base_seconds=0,
        )
        self.transport = SafeSessionTransport(policy=policy, session=self.session)
        self.sdk = VghksSDK(
            settings=SDKSettings(request_policy=policy),
            credentials=PortalCredentials("U001", SECRET),
            transport=self.transport,
        )

    def dispatch(self, method, url, **kwargs):
        path = urlsplit(url).path
        overrides = self.overrides.get(path, [])
        if overrides:
            result = overrides.pop(0)
            if isinstance(result, BaseException):
                raise result
            result.url = url
        else:
            scripted = self.script.request(method, url, **kwargs)
            result = reply(scripted.body)
            result.url = scripted.url
        if path == "/login.do":
            self.session.cookies.set("JSESSIONID", "fresh-synthetic-cookie")
        return result

    @property
    def password_posts(self):
        return [
            call
            for call in self.session.request.call_args_list
            if call.args[0] == "POST" and urlsplit(call.args[1]).path == "/login.do"
        ]

    def query(self):
        runtime = self.sdk._runtime
        spec = operation_spec("prq.visit_cases")
        return runtime.execute(
            spec,
            lambda: runtime.request_text(spec, runtime.settings.prq_base_url + "/QueryCaseList.do"),
            operation_name="prq.visit_cases",
        )


class LoginResponseTests(unittest.TestCase):
    def test_rejection_is_distinct_from_expiry_and_takes_precedence_over_scripts(self):
        for form in (LOGIN_FORM, "<INPUT NAME=muid><INPUT NAME=mpassword>"):
            with self.subTest(form=form), self.assertRaises(LoginRejectedError) as caught:
                parse_login_target(form + '<script>targetUrl="myPortal.do";</script>')
            self.assertNotIsInstance(caught.exception, AuthExpiredError)
            self.assertIsInstance(caught.exception, AuthenticationError)
            self.assertEqual(caught.exception.info.code, "PORTAL_LOGIN_REJECTED")

    def test_javascript_examples_are_not_live_login_inputs(self):
        text = f'<script>var example={LOGIN_FORM!r}; targetUrl="myPortal.do";</script>'
        self.assertFalse(has_portal_login_form(text))
        self.assertEqual(parse_login_target(text), "myPortal.do")

    def test_empty_and_unknown_responses_do_not_claim_invalid_credentials(self):
        for body, code in (
            ("", "PORTAL_LOGIN_RESPONSE_EMPTY"),
            ("unknown", "PORTAL_LOGIN_TARGET_MISSING"),
        ):
            with self.subTest(code=code), self.assertRaises(AuthenticationError) as caught:
                parse_login_target(body)
            self.assertNotIsInstance(caught.exception, (LoginRejectedError, AuthExpiredError))
            self.assertEqual(caught.exception.info.code, code)

    def test_password_is_absent_from_credential_repr(self):
        self.assertNotIn(SECRET, repr(PortalCredentials("U001", SECRET)))

    def test_offline_login_replay_keeps_the_rejection_code(self):
        result = replay_response("portal.login", LOGIN_FORM.encode(), {})
        self.assertEqual(result["error_code"], "PORTAL_LOGIN_REJECTED")


class LoginRecoveryTests(unittest.TestCase):
    def test_error_page_button_is_rejection_without_logout_request(self):
        for lazy in (False, True):
            fixture = PortalFixture({"/login.do": [reply(DENIED_PAGE)]})
            with self.subTest(lazy=lazy), self.assertRaises(LoginRejectedError):
                fixture.query() if lazy else fixture.sdk.auth.login()
            self.assertEqual(len(fixture.password_posts), 1)
            self.assertEqual(fixture.session.request.call_count, 2)
            self.assertFalse(fixture.sdk._runtime.auth.portal_authenticated)
        self.assertEqual(
            replay_response("portal.login", DENIED_PAGE.encode(), {})["error_code"],
            "PORTAL_LOGIN_REJECTED",
        )

    def test_navigation_ignores_handlers_comments_strings_callbacks_and_json_scripts(self):
        for body in (
            "<button onclick=\"location.href='logout.do'\">Retry</button>",
            '<script>// targetUrl="logout.do";\n/* location.href="index.do"; */</script>',
            "<script>var sample=\"targetUrl='logout.do';\";</script>",
            '<script>function callback(){location.href="logout.do";}</script>',
            '<script>if(false){targetUrl="logout.do";}</script>',
            '<script type="application/json">{"targetUrl":"logout.do"}</script>',
        ):
            with self.subTest(body=body), self.assertRaises(AuthenticationError) as caught:
                parse_login_target(body)
            self.assertEqual(caught.exception.info.code, "PORTAL_LOGIN_TARGET_MISSING")
            self.assertEqual(
                parse_login_target(body + '<script>targetUrl="myPortal.do";</script>'),
                "myPortal.do",
            )

    def test_script_error_prose_is_not_login_rejection(self):
        self.assertEqual(
            parse_login_target(
                '<script>var message="登入失敗 帳號或密碼錯誤";targetUrl="myPortal.do";</script>'
            ),
            "myPortal.do",
        )

    def test_unknown_target_and_login_entrance_never_authenticate(self):
        for target, code in (
            ("logout.do", "PORTAL_LOGIN_REJECTED"),
            ("unknown.do", "PORTAL_LOGIN_TARGET_UNRECOGNIZED"),
        ):
            fixture = PortalFixture(
                {"/login.do": [reply(f'<script>targetUrl="{target}";</script>')]}
            )
            with self.subTest(target=target), self.assertRaises(AuthenticationError) as caught:
                fixture.sdk.auth.login()
            self.assertEqual(caught.exception.info.code, code)
            self.assertEqual(fixture.session.request.call_count, 2)
            self.assertFalse(fixture.sdk._runtime.auth.portal_authenticated)

    def test_json_expiry_redirect_reauthenticates_without_visiting_http(self):
        fixture = PortalFixture(
            {
                "/PRQWeb/QueryUploadMR.do": [
                    reply("", 302, "http://intranet.vghks.gov.tw/"),
                    reply('[{"maintp":"OPD","mainnm":"Synthetic"}]'),
                ]
            }
        )
        self.assertEqual(len(fixture.sdk.records.get_upload_types()), 1)
        self.assertEqual(len(fixture.password_posts), 2)
        self.assertTrue(
            all(
                call.args[1].startswith("https://")
                for call in fixture.session.request.call_args_list
            )
        )
        queries = [
            c
            for c in fixture.session.request.call_args_list
            if c.args[1].endswith("QueryUploadMR.do")
        ]
        self.assertEqual(len(queries), 2)
        self.assertTrue(all(c.kwargs["allow_redirects"] is False for c in queries))

    def test_unknown_json_redirect_does_not_trigger_relogin(self):
        for target in ("", "http://foreign.invalid/", "/PRQWeb/unrecorded.do"):
            fixture = PortalFixture({"/PRQWeb/QueryUploadMR.do": [reply("", 302, target)]})
            with self.subTest(target=target), self.assertRaises(Exception) as caught:
                fixture.sdk.records.get_upload_types()
            self.assertEqual(caught.exception.info.code, "QUERY_REDIRECT_UNRECOGNIZED")
            self.assertEqual(len(fixture.password_posts), 1)

    def test_second_login_redirect_stops_after_one_relogin(self):
        fixture = PortalFixture(
            {
                "/PRQWeb/QueryUploadMR.do": [
                    reply("", 302, "http://intranet.vghks.gov.tw/") for _ in range(2)
                ]
            }
        )
        with self.assertRaises(AuthenticationError) as caught:
            fixture.sdk.records.get_upload_types()
        self.assertEqual(caught.exception.info.code, "AUTH_RELOGIN_FAILED")
        self.assertEqual(len(fixture.password_posts), 2)

    def test_automatic_entrance_script_is_expiry_and_offline_keeps_the_code(self):
        fixture = PortalFixture()
        url = "http://intranet.vghks.gov.tw/"
        body = '<script>//location.href="unrecorded.do";\nlocation.href="index.do";</script>'
        with self.assertRaises(AuthExpiredError) as caught:
            fixture.sdk._runtime.auth.assert_not_expired(body, url)
        self.assertEqual(caught.exception.info.code, "AUTH_SESSION_REDIRECT")
        self.assertEqual(
            replay_response("prq.upload_types", body.encode(), {}, response_url=url)["error_code"],
            "AUTH_SESSION_REDIRECT",
        )
        fixture.sdk._runtime.auth.assert_not_expired(
            "<button onclick=\"location.href='index.do'\">Login</button>", url
        )

    def test_rejected_direct_login_and_lazy_query_submit_once(self):
        for action in (lambda f: f.sdk.auth.login(), lambda f: f.query()):
            fixture = PortalFixture({"/login.do": [reply(LOGIN_FORM)]})
            with self.subTest(action=action), self.assertRaises(LoginRejectedError) as caught:
                action(fixture)
            self.assertEqual(caught.exception.info.code, "PORTAL_LOGIN_REJECTED")
            self.assertEqual(len(fixture.password_posts), 1)
            self.assertFalse(fixture.sdk._runtime.auth.portal_authenticated)
            self.assertEqual(fixture.sdk._runtime.auth.generation, 0)
            self.assertEqual(fixture.session.request.call_count, 2)
            self.assertNotIn(SECRET, str(caught.exception))
            self.assertNotIn(SECRET, str(to_jsonable(caught.exception.info)))

    def test_auth_check_blocks_dependents_without_a_second_password_post(self):
        fixture = PortalFixture({"/login.do": [reply(LOGIN_FORM)]})
        report = fixture.sdk.auth.check()
        self.assertFalse(report.ok)
        self.assertFalse(report.reauthenticated)
        self.assertEqual(report.targets[0].issue.code, "PORTAL_LOGIN_REJECTED")
        self.assertTrue(all(row.status == "BLOCKED" for row in report.targets[1:]))
        self.assertEqual(len(fixture.password_posts), 1)
        self.assertEqual(fixture.session.request.call_count, 2)

    def test_denied_login_http_is_not_retried_as_expiry(self):
        for path in ("/index.do", "/login.do", "/myPortal.do"):
            for status in (401, 403):
                with self.subTest(path=path, status=status):
                    fixture = PortalFixture({path: [reply("denied", status)]})
                    with self.assertRaises(AuthenticationError) as caught:
                        fixture.query()
                    self.assertNotIsInstance(
                        caught.exception, (LoginRejectedError, AuthExpiredError)
                    )
                    self.assertEqual(caught.exception.info.code, "PORTAL_LOGIN_HTTP_DENIED")
                    self.assertEqual(caught.exception.info.http_status, status)
                    self.assertEqual(len(fixture.password_posts), int(path != "/index.do"))

    def test_blank_login_during_query_is_not_retried(self):
        fixture = PortalFixture({"/login.do": [reply("")]})
        with self.assertRaises(AuthenticationError) as caught:
            fixture.query()
        self.assertEqual(caught.exception.info.code, "PORTAL_LOGIN_RESPONSE_EMPTY")
        self.assertEqual(len(fixture.password_posts), 1)

    def test_login_landing_back_at_form_is_rejected(self):
        fixture = PortalFixture({"/myPortal.do": [reply(LOGIN_FORM)]})
        with self.assertRaises(LoginRejectedError):
            fixture.query()
        self.assertEqual(len(fixture.password_posts), 1)

    def test_empty_landing_and_unknown_redirect_are_not_login_success(self):
        for overrides, code in (
            ({"/myPortal.do": [reply("")]}, "PORTAL_LOGIN_LANDING_EMPTY"),
            ({"/login.do": [reply("", 302, "/unknown.do")]}, "PORTAL_LOGIN_TARGET_MISSING"),
        ):
            with self.subTest(code=code):
                fixture = PortalFixture(overrides)
                with self.assertRaises(AuthenticationError) as caught:
                    fixture.query()
                self.assertEqual(caught.exception.info.code, code)
                self.assertFalse(fixture.sdk._runtime.auth.portal_authenticated)
                self.assertEqual(len(fixture.password_posts), 1)

    def test_login_system_error_does_not_trigger_password_resubmission(self):
        fixture = PortalFixture(
            {"/login.do": [reply('<script>targetUrl="sysErrorException.jsp";</script>')]}
        )
        with self.assertRaises(AuthenticationError) as caught:
            fixture.query()
        self.assertNotIsInstance(caught.exception, (LoginRejectedError, AuthExpiredError))
        self.assertEqual(caught.exception.info.code, "PORTAL_LOGIN_NOT_ESTABLISHED")
        self.assertEqual(len(fixture.password_posts), 1)

    def test_query_expiry_still_relogs_and_rebuilds_sso_once(self):
        for expired in (reply(LOGIN_FORM), reply("", 401), reply("", 403)):
            with self.subTest(status=expired.status_code):
                fixture = PortalFixture({"/PRQWeb/QueryCaseList.do": [expired, reply("result")]})
                self.assertEqual(fixture.query(), "result")
                self.assertEqual(len(fixture.password_posts), 2)
                sso = [
                    c
                    for c in fixture.session.request.call_args_list
                    if c.args[1].endswith("/WPSAutoLogon")
                ]
                self.assertEqual(len(sso), 2)
                self.assertEqual(fixture.sdk._runtime.auth.generation, 2)

    def test_rejection_during_relogin_preserves_the_original_code(self):
        fixture = PortalFixture()
        fixture.sdk.auth.login()
        fixture.overrides = {
            "/PRQWeb/QueryCaseList.do": [reply(LOGIN_FORM)],
            "/login.do": [reply(LOGIN_FORM)],
        }
        with self.assertRaises(LoginRejectedError) as caught:
            fixture.query()
        self.assertEqual(caught.exception.info.code, "PORTAL_LOGIN_REJECTED")
        self.assertEqual(caught.exception.info.app, "portal")
        self.assertEqual(len(fixture.password_posts), 2)
        self.assertFalse(fixture.sdk._runtime.auth.portal_authenticated)
        self.assertFalse(fixture.sdk._runtime.auth._apps)

    def test_unknown_relogin_failure_keeps_its_specific_code(self):
        fixture = PortalFixture()
        fixture.sdk.auth.login()
        fixture.overrides = {
            "/PRQWeb/QueryCaseList.do": [reply(LOGIN_FORM)],
            "/login.do": [reply("")],
        }
        with self.assertRaises(AuthenticationError) as caught:
            fixture.query()
        self.assertEqual(caught.exception.info.code, "PORTAL_LOGIN_RESPONSE_EMPTY")
        self.assertNotIsInstance(caught.exception, LoginRejectedError)
        self.assertEqual(len(fixture.password_posts), 2)

    def test_second_expiry_stops_without_another_relogin(self):
        fixture = PortalFixture(
            {"/PRQWeb/QueryCaseList.do": [reply(LOGIN_FORM), reply(LOGIN_FORM)]}
        )
        with self.assertRaises(AuthenticationError) as caught:
            fixture.query()
        self.assertEqual(caught.exception.info.code, "AUTH_RELOGIN_FAILED")
        self.assertEqual(len(fixture.password_posts), 2)

    def test_auth_check_recovery_reports_rejection_without_a_third_login(self):
        fixture = PortalFixture()
        fixture.sdk.auth.login()
        fixture.overrides = {
            "/sessionCheck.do": [reply(LOGIN_FORM)],
            "/login.do": [reply(LOGIN_FORM)],
        }
        report = fixture.sdk.auth.check()
        self.assertTrue(report.reauthenticated)
        self.assertEqual(report.targets[0].issue.code, "PORTAL_LOGIN_REJECTED")
        self.assertTrue(all(row.status == "BLOCKED" for row in report.targets[1:]))
        self.assertEqual(len(fixture.password_posts), 2)

    def test_password_redirects_never_repost_credentials(self):
        for status in (307, 308):
            with self.subTest(status=status):
                fixture = PortalFixture({"/login.do": [reply("", status, "/myPortal.do")]})
                with self.assertRaises(AuthenticationError) as caught:
                    fixture.query()
                self.assertEqual(
                    caught.exception.info.code, "PORTAL_LOGIN_POST_REDIRECT_UNSUPPORTED"
                )
                self.assertEqual(fixture.session.request.call_count, 2)
                self.assertFalse(fixture.password_posts[0].kwargs["allow_redirects"])

    def test_http_redirect_login_can_succeed_or_return_a_rejected_form(self):
        for status in (301, 302, 303):
            for rejected in (True, False):
                with self.subTest(status=status, rejected=rejected):
                    fixture = PortalFixture(
                        {
                            "/login.do": [reply("", status, "/myPortal.do")],
                            "/myPortal.do": [reply(LOGIN_FORM if rejected else "portal")],
                        }
                    )
                    if rejected:
                        with self.assertRaises(LoginRejectedError):
                            fixture.sdk.auth.login()
                    else:
                        fixture.sdk.auth.login()
                        self.assertTrue(fixture.sdk._runtime.auth.portal_authenticated)
                    self.assertEqual(len(fixture.password_posts), 1)
                    self.assertEqual(
                        [c.args[0] for c in fixture.session.request.call_args_list],
                        ["GET", "POST", "GET"],
                    )

    def test_foreign_redirect_is_rejected_before_contacting_it(self):
        fixture = PortalFixture({"/login.do": [reply("", 302, "https://foreign.invalid/login.do")]})
        with self.assertRaises(AuthenticationError):
            fixture.sdk.auth.login()
        self.assertEqual(fixture.session.request.call_count, 2)

    def test_redirect_cycles_and_missing_locations_are_bounded(self):
        for overrides, code in (
            ({"/login.do": [reply("", 302)]}, "PORTAL_LOGIN_REDIRECT_MISSING"),
            (
                {
                    "/login.do": [reply("", 302, "/myPortal.do")],
                    "/myPortal.do": [reply("", 302, "/myPortal.do") for _ in range(6)],
                },
                "PORTAL_LOGIN_REDIRECT_LIMIT",
            ),
        ):
            with self.subTest(code=code):
                fixture = PortalFixture(overrides)
                with self.assertRaises(AuthenticationError) as caught:
                    fixture.query()
                self.assertEqual(caught.exception.info.code, code)
                self.assertEqual(len(fixture.password_posts), 1)
                self.assertLessEqual(fixture.session.request.call_count, 8)


if __name__ == "__main__":
    unittest.main()
