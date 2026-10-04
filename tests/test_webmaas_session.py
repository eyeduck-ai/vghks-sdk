from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from test_webmaas import adapter_with, landing, response

from vghks_sdk.adapters.auth import AppSession, AuthenticationAdapter
from vghks_sdk.core.config import PortalCredentials, SDKSettings
from vghks_sdk.core.diagnostics import DiagnosticRecorder
from vghks_sdk.core.errors import LoginRejectedError, ParseError, RequestError
from vghks_sdk.core.transport import SafeSessionTransport
from vghks_sdk.runtime import SDKRuntime


def readiness_runtime(mode="healthy", page="RSV11W001"):
    settings = SDKSettings(
        portal_base_url="https://synthetic.test",
        sectord_base_url="https://synthetic.test/SectOrdWeb",
        webmaas_base_url="https://synthetic.test/webmaas",
    )
    transport = MagicMock(spec=SafeSessionTransport)
    transport.text.side_effect = lambda value: value.text
    transport.session = MagicMock()
    auth = AuthenticationAdapter(
        settings=settings, credentials=PortalCredentials("SYNTHETIC", "SECRET"),
        transport=transport,
    )
    auth._portal_authenticated = True
    auth._apps["sectord"] = AppSession("sectord", "SYNTHETIC-HID", settings.sectord_base_url)
    path = "/QUY/QUY15W001.do" if page == "QUY15W001" else "/RSV/RSV11W001.do"
    form = "QUY15WForm" if page == "QUY15W001" else "RSV11WForm"
    auth._webmaas_page = page
    auth._apps["webmaas"] = AppSession(
        "webmaas", "SYNTHETIC-HID", settings.webmaas_base_url + path,
        landing(form, "stale-token"),
    )
    reopened = False

    def login(*, force=False):
        if force:
            if mode == "relogin_rejected":
                raise LoginRejectedError("synthetic rejection")
            auth.invalidate_webmaas_session()

    auth.login = MagicMock(side_effect=login)
    auth.check_portal_session = MagicMock(return_value=settings.portal_base_url + "/sessionCheck.do")

    def request(method, url, **kwargs):
        nonlocal reopened
        assert method == "GET", "readiness must not submit a patient or password form"
        if url.endswith("/so.do"):
            result = response("ssID=fresh&keyOne=1&keyTwo=2&keyThree=3")
        elif url.endswith("/WPSAutoLogon"):
            reopened = True
            result = response("<html>synthetic unrecognized page</html>" if mode == "persistent_missing"
                              else landing("RSV11WForm", "fresh-sso-token"))
            if mode == "persistent_expiry":
                result.url = settings.portal_base_url + "/login.do"
                return result
            result.url = kwargs["params"]["targetURL"]
            return result
        elif mode in {"login_page", "persistent_expiry", "relogin_rejected"} and (
            not reopened or mode == "persistent_expiry"
        ):
            result = response("<form><input name='muid'><input name='mpassword'></form>")
            result.url = settings.portal_base_url + "/login.do"
            return result
        elif mode == "http_denied" and not reopened:
            raise RequestError("synthetic forbidden", status_code=403)
        elif mode == "persistent_missing" or (mode == "form_missing" and not reopened):
            result = response("<html>synthetic unrecognized page</html>")
        elif mode == "token_missing" and not reopened:
            result = response(landing(form, ""))
        else:
            result = response(landing(form, "fresh-get-token"))
        result.url = url
        return result

    transport.request.side_effect = request
    return SDKRuntime(settings=settings, transport=transport, auth=auth), auth, transport


class WebMaasSessionTests(unittest.TestCase):
    def test_cached_sso_html_does_not_replace_a_live_check(self):
        for page in ("RSV11W001", "QUY15W001"):
            with self.subTest(page=page):
                runtime, auth, transport = readiness_runtime(page=page)
                report = runtime.auth_check(("webmaas",))
                self.assertTrue(report.ok)
                self.assertEqual(transport.request.call_count, 1)
                self.assertIn("fresh-get-token", auth.take_webmaas_landing(page))
                self.assertEqual(auth.take_webmaas_landing(page), "")

    def test_missing_form_or_token_is_not_expiry_and_next_check_renews_only_sso(self):
        for mode, code in (
            ("form_missing", "WEBMAAS_QUERY_FORM_MISSING"),
            ("token_missing", "WEBMAAS_QUERY_TOKEN_MISSING"),
        ):
            with self.subTest(mode=mode):
                runtime, auth, transport = readiness_runtime(mode)
                first = runtime.auth_check(("webmaas",))
                self.assertFalse(first.ok)
                self.assertFalse(first.reauthenticated)
                issue = first.targets[-1].issue
                self.assertEqual((issue.code, issue.category, issue.http_status), (code, "PARSE", 200))
                self.assertEqual(issue.operation, "webmaas.session_check")
                self.assertEqual(issue.endpoint_path, "/webmaas/RSV/RSV11W001.do")
                self.assertNotIn("webmaas", auth._apps)
                self.assertIn("sectord", auth._apps)
                self.assertTrue(runtime.auth_check(("webmaas",)).ok)
                self.assertEqual(transport.request.call_count, 3)
                self.assertFalse(any(call.kwargs.get("force") for call in auth.login.call_args_list))

    def test_persistent_unknown_page_does_not_become_login_success(self):
        runtime, auth, _ = readiness_runtime("persistent_missing")
        for _ in range(2):
            report = runtime.auth_check(("webmaas",))
            self.assertEqual(report.targets[-1].issue.code, "WEBMAAS_QUERY_FORM_MISSING")
            self.assertNotIn("webmaas", auth._apps)
        self.assertFalse(any(call.kwargs.get("force") for call in auth.login.call_args_list))

    def test_explicit_login_page_and_http_denial_recover_once(self):
        for mode in ("login_page", "http_denied"):
            with self.subTest(mode=mode):
                runtime, auth, _ = readiness_runtime(mode)
                report = runtime.auth_check(("webmaas",))
                self.assertTrue(report.ok)
                self.assertTrue(report.reauthenticated)
                self.assertEqual(sum(call.kwargs.get("force", False) for call in auth.login.call_args_list), 1)

    def test_relogin_rejection_preserves_its_code_and_blocks_webmaas(self):
        runtime, auth, _ = readiness_runtime("relogin_rejected")
        report = runtime.auth_check(("webmaas",))
        self.assertFalse(report.ok)
        self.assertEqual(report.targets[0].issue.code, "AUTH_LOGIN_REJECTED")
        self.assertEqual(report.targets[-1].status, "BLOCKED")
        self.assertEqual(sum(call.kwargs.get("force", False) for call in auth.login.call_args_list), 1)

    def test_second_expiry_stops_without_a_second_forced_login(self):
        runtime, auth, _ = readiness_runtime("persistent_expiry")
        report = runtime.auth_check(("webmaas",))
        self.assertFalse(report.ok)
        self.assertEqual(report.targets[-1].issue.code, "AUTH_SESSION_LOGIN_PAGE")
        self.assertEqual(sum(call.kwargs.get("force", False) for call in auth.login.call_args_list), 1)

    def test_query_form_failure_keeps_http_context_and_invalidates_cache(self):
        adapter, session, auth = adapter_with(response("<html>unknown response</html>"))
        with self.assertRaises(ParseError) as caught:
            adapter.get_basic_info("0000000")
        issue = caught.exception.info
        self.assertEqual((issue.code, issue.http_status), ("WEBMAAS_QUERY_FORM_MISSING", 200))
        self.assertEqual(issue.endpoint_path, "/webmaas/QUY/QUY15W001.do")
        auth.invalidate_webmaas_session.assert_called_once()
        self.assertEqual(session.request.call_count, 1)
        auth.login.assert_not_called()

    def test_diagnostics_keep_redirect_structure_and_token_presence_without_values(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = DiagnosticRecorder(Path(directory))
            prior = response("", status=302)
            prior.url = "https://synthetic.test/webmaas/QUY/QUY15W001.do"
            prior.headers["Location"] = "/login.do?secret=SENSITIVE-QUERY"
            final = response(landing("QUY15WForm", "SENSITIVE-TOKEN"))
            final.history = [prior]
            recorder.record_http_response(request_id=1, response=final, elapsed_seconds=0)
            recorder.finalize(command="synthetic", status="OK", exit_code=0)
            text = recorder.trace_path.read_text(encoding="utf-8")
            self.assertNotIn("SENSITIVE", text)
            row = json.loads(text.splitlines()[1])
            self.assertEqual(row["redirects"][0]["location_path"], "/login.do")
            shape = row["response"]["html_shape"]
            self.assertEqual(shape["selector_counts"]["form#QUY15WForm"], 1)
            self.assertTrue(shape["forms"][0]["query_token_present"])


if __name__ == "__main__":
    unittest.main()
