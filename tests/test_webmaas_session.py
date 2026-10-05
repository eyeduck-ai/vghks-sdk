from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

from test_webmaas import adapter_with, landing, response

from vghks_sdk.adapters.auth import AppSession, AuthenticationAdapter
from vghks_sdk.core.config import PortalCredentials, SDKSettings
from vghks_sdk.core.diagnostics import DiagnosticRecorder
from vghks_sdk.core.errors import (
    ApplicationSessionExpiredError,
    AuthenticationError,
    LoginRejectedError,
    NotAuthenticatedError,
    ParseError,
    RequestError,
)
from vghks_sdk.core.operations import operation_spec
from vghks_sdk.core.transport import SafeSessionTransport
from vghks_sdk.offline.analyze import _phase, _retest_config, _session_test_summary
from vghks_sdk.offline.bundle import BundleReader
from vghks_sdk.offline.replay import replay_response
from vghks_sdk.parsing.webmaas import is_webmaas_session_timeout
from vghks_sdk.runtime import SDKRuntime

TIMEOUT_HTML = '<html><p>Page time out, Please <a>重新登入</a></p></html>'


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
            if mode == "sso_network":
                raise RequestError("synthetic DNS failure", code="NETWORK_DNS_FAILED",
                                   retry_safe=True, retry_recommended=True)
            result = response("ssID=fresh&keyOne=1&keyTwo=2&keyThree=3")
        elif url.endswith("/WPSAutoLogon"):
            reopened = True
            if mode == "persistent_timeout":
                result = response(TIMEOUT_HTML)
                result.url = settings.webmaas_base_url + "/comm/pageTimeOut.do"
                return result
            sso_form = "QUY15WForm" if "/QUY/" in kwargs["params"]["targetURL"] else "RSV11WForm"
            result = response("<html>synthetic unrecognized page</html>" if mode == "persistent_missing"
                              else landing(sso_form, "fresh-sso-token"))
            if mode == "persistent_expiry":
                result.url = settings.portal_base_url + "/login.do"
                return result
            result.url = kwargs["params"]["targetURL"]
            return result
        elif mode in {"timeout", "persistent_timeout", "sso_network"} and not reopened:
            result = response(TIMEOUT_HTML)
            result.url = settings.webmaas_base_url + "/comm/pageTimeOut.do"
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
    def test_timeout_requires_exact_origin_path_and_visible_notice(self):
        base = "https://synthetic.test/webmaas"
        url = base + "/comm/pageTimeOut.do"
        self.assertTrue(is_webmaas_session_timeout(TIMEOUT_HTML, url, base))
        for page, target in (
            (TIMEOUT_HTML, "https://foreign.test/webmaas/comm/pageTimeOut.do"),
            (TIMEOUT_HTML, base + "/comm/pageTimeOut.do/other"),
            (TIMEOUT_HTML, base + "/QUY/QUY15W001.do"),
            ("<script>var text='Page time out';</script>", url),
            ("<!-- Page time out -->", url),
            ("<p hidden>Page time out</p>", url),
            ("<p>Unknown system error</p>", url),
        ):
            with self.subTest(target=target, page=page):
                self.assertFalse(is_webmaas_session_timeout(page, target, base))

    def test_timeout_before_login_is_not_authenticated_and_never_forces_login(self):
        runtime, auth, _ = readiness_runtime()
        auth._portal_authenticated = False
        with self.assertRaises(NotAuthenticatedError) as caught:
            auth.assert_not_expired(TIMEOUT_HTML, runtime.settings.webmaas_base_url + "/comm/pageTimeOut.do")
        self.assertEqual(caught.exception.info.app, "webmaas")
        auth.login.assert_not_called()

    def test_readiness_preserves_application_timeout_without_portal_relogin(self):
        runtime, auth, transport = readiness_runtime("timeout")
        sectord = auth._apps["sectord"]
        report = runtime.auth_check(("webmaas",))
        issue = report.targets[-1].issue
        self.assertEqual((issue.code, issue.category, issue.http_status),
                         ("WEBMAAS_SESSION_TIMEOUT", "AUTHENTICATION", 200))
        self.assertEqual(issue.endpoint_path, "/webmaas/comm/pageTimeOut.do")
        self.assertFalse(report.reauthenticated)
        self.assertIs(auth._apps["sectord"], sectord)
        self.assertNotIn("webmaas", auth._apps)
        self.assertTrue(runtime.auth_check(("webmaas",)).ok)
        self.assertEqual(transport.request.call_count, 3)
        self.assertFalse(any(call.kwargs.get("force") for call in auth.login.call_args_list))

    def test_direct_read_renews_only_webmaas_once_and_records_original_issue(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime, auth, transport = readiness_runtime("timeout")
            recorder = DiagnosticRecorder(Path(directory))
            runtime.diagnostics = recorder
            spec = operation_spec("webmaas.registration_landing")
            url = runtime.settings.webmaas_base_url + "/RSV/RSV11W001.do"
            result = runtime.execute(spec, lambda: runtime.request_text(spec, url), operation_name="synthetic_read")
            self.assertIn("fresh-get-token", result)
            self.assertEqual(transport.request.call_count, 4)
            self.assertFalse(any(call.kwargs.get("force") for call in auth.login.call_args_list))
            recorder.finalize(command="synthetic", status="OK", exit_code=0)
            rows = [json.loads(line) for line in recorder.trace_path.read_text(encoding="utf-8").splitlines()]
            recovery = [row for row in rows if row["event"] == "application_session_recovery_started"]
            self.assertEqual(len(recovery), 1)
            self.assertEqual(recovery[0]["issue"]["code"], "WEBMAAS_SESSION_TIMEOUT")
            self.assertEqual(recovery[0]["issue"]["http_status"], 200)

    def test_persistent_application_timeout_stops_after_one_sso_and_keeps_code(self):
        runtime, auth, transport = readiness_runtime("persistent_timeout")
        spec = operation_spec("webmaas.registration_landing")
        url = runtime.settings.webmaas_base_url + "/RSV/RSV11W001.do"
        with self.assertRaises(ApplicationSessionExpiredError) as caught:
            runtime.execute(spec, lambda: runtime.request_text(spec, url), operation_name="synthetic_read")
        self.assertEqual(caught.exception.info.code, "WEBMAAS_SESSION_TIMEOUT")
        self.assertEqual(caught.exception.info.attempt, 2)
        self.assertEqual(caught.exception.info.http_status, 200)
        self.assertFalse(caught.exception.info.retry_safe)
        self.assertEqual(transport.request.call_count, 3)
        self.assertFalse(any(call.kwargs.get("force") for call in auth.login.call_args_list))

    def test_sso_network_failure_keeps_its_cause_and_does_not_offer_password_replay(self):
        runtime, auth, transport = readiness_runtime("sso_network")
        spec = operation_spec("webmaas.registration_landing")
        url = runtime.settings.webmaas_base_url + "/RSV/RSV11W001.do"
        with self.assertRaises(AuthenticationError) as caught:
            runtime.execute(spec, lambda: runtime.request_text(spec, url), operation_name="synthetic_read")
        issue = caught.exception.info
        self.assertEqual((issue.code, issue.phase), ("WEBMAAS_SSO_RECOVERY_FAILED", "REAUTHENTICATION"))
        self.assertEqual(issue.root_cause.code, "NETWORK_DNS_FAILED")
        self.assertTrue(issue.root_cause.retry_safe)
        self.assertFalse(issue.retry_safe)
        self.assertFalse(issue.retry_recommended)
        self.assertEqual(transport.request.call_count, 2)
        self.assertFalse(any(call.kwargs.get("force") for call in auth.login.call_args_list))

    def test_recovery_retains_the_basic_info_sso_role_without_an_extra_sso(self):
        runtime, auth, transport = readiness_runtime("timeout", page="QUY15W001")
        spec = operation_spec("webmaas.basic_info_landing")
        url = runtime.settings.webmaas_base_url + "/QUY/QUY15W001.do"
        runtime.execute(spec, lambda: runtime.request_text(spec, url), operation_name="synthetic_basic_info")
        sso_calls = [call for call in transport.request.call_args_list if call.args[1].endswith("/WPSAutoLogon")]
        self.assertEqual(len(sso_calls), 1)
        self.assertEqual(sso_calls[0].kwargs["params"]["externalRoles"], "maas_QRY15")
        self.assertEqual(auth._webmaas_page, "QUY15W001")

    def test_application_timeout_cannot_replay_a_mutation_or_conditional_write(self):
        for write in ("mutation", "conditional"):
            with self.subTest(write=write):
                runtime, auth, transport = readiness_runtime("timeout")
                spec = operation_spec("webmaas.registration_landing")
                url = runtime.settings.webmaas_base_url + "/RSV/RSV11W001.do"
                def operation(write=write, runtime=runtime, spec=spec, url=url):
                    if write == "conditional":
                        runtime._operation_write_attempts[-1] = True
                    return runtime.request_text(spec, url)
                with self.assertRaises(ApplicationSessionExpiredError) as caught:
                    runtime.execute(replace(spec, mutates=write == "mutation"), operation, operation_name="synthetic_write")
                self.assertFalse(caught.exception.info.retry_safe)
                self.assertEqual(transport.request.call_count, 1)
                auth.login.assert_not_called()

    def test_offline_replay_uses_same_timeout_rule_and_retest_preserves_session_scope(self):
        base = "https://synthetic.test/webmaas"
        row = replay_response("webmaas.basic_info_landing", TIMEOUT_HTML.encode(), {},
                              response_url=base + "/comm/pageTimeOut.do", webmaas_base_url=base)
        self.assertEqual(row["error_code"], "WEBMAAS_SESSION_TIMEOUT")
        self.assertEqual(_phase(row["error_code"]), "AUTHENTICATION")
        config = _retest_config({"profile": "session", "session_pause": True}, [],
                                {"phase": "AUTHENTICATION"}, "COMPLETED_WITH_ERRORS")
        self.assertEqual(config["profile"], "session")
        self.assertTrue(config["session_pause"])
        self.assertEqual(config["login_negative_attempts"], 0)
        self.assertNotIn("output_root", config)

    def test_offline_session_recovery_requires_the_exact_recheck_capture_range(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "run_summary.json").write_text('{"schema_version": 6}', encoding="utf-8")
            (root / "capture_manifest.jsonl").write_text("", encoding="utf-8")
            parsed = root / "parsed/session"
            parsed.mkdir(parents=True)
            (parsed / "comparison.json").write_text(json.dumps({
                "challenge": {"evidence": "LOCAL_COOKIE_LOSS", "secret": "SENSITIVE"},
                "natural_ttl": {"verified": True},
            }), encoding="utf-8")
            step = {"name": "session.after_cookie_loss.sso_recheck", "status": "OK",
                    "first_capture_id": "000010", "last_capture_id": "000014"}
            for capture_id, expected in (("000009", False), ("000014", True), ("000015", False)):
                rows = [{"operation": "webmaas.registration_landing", "status": "CONTRACT_OK",
                         "capture_id": capture_id, "error_code": ""}]
                with BundleReader(root) as reader:
                    summary = _session_test_summary(reader, [step], rows)
                self.assertEqual(summary["sso_rechecks"][0]["query_form_verified_in_capture_range"], expected)
                self.assertFalse(summary["natural_ttl_verified"])
                self.assertNotIn("SENSITIVE", json.dumps(summary))

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
