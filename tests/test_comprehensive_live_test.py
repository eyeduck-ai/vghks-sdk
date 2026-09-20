from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from vghks_sdk.core.config import PortalCredentials, RequestPolicy, SDKSettings
from vghks_sdk.core.errors import AuthenticationError, ErrorInfo, ParseError, RequestError
from vghks_sdk.core.readiness import make_auth_report, resolve_auth_targets
from vghks_sdk.core.tls import TLS12_COMPAT, TLS_DEFAULT
from vghks_sdk.live.atomic import _query_inputs, build_test_plan, run_atomic_test
from vghks_sdk.live.config import LiveTestConfig, resolve_live_test_config
from vghks_sdk.live.preflight import _dns, _https, run_network_checks
from vghks_sdk.live.profile import LIVE_TEST_MRN, LiveTestStep, _overall_status
from vghks_sdk.live.runner import execute_live_test
from vghks_sdk.live_test_app import build_parser, main, run_live_test_namespace
from vghks_sdk.models import (
    AuthCheckTarget,
    BinaryAsset,
    CaseDetail,
    ClinicalOrder,
    OrderDetailRef,
    OrderReport,
    OrderReportRef,
    PacsImageRef,
    PacsStudy,
    PacsStudyRef,
    SoapRecord,
    VisitCase,
)
from vghks_sdk.offline.analyze import analyze_bundle
from vghks_sdk.queries import QUERY_SPECS


def readiness(*, only):
    return make_auth_report(
        AuthCheckTarget(spec.key, (), "", False, 1, 0, spec.dependencies, "OK")
        for spec in resolve_auth_targets(only)
    )


class ComprehensiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cases = [
            VisitCase(
                LIVE_TEST_MRN, date(2026, 1, 20) - timedelta(days=i), "O", f"c{i}", "60", "眼科"
            )
            for i in range(12)
        ]
        self.sdk = SimpleNamespace(auth=SimpleNamespace(check=Mock(side_effect=readiness)))
        for spec in QUERY_SPECS:
            if not hasattr(self.sdk, spec.service):
                setattr(self.sdk, spec.service, SimpleNamespace())
            setattr(getattr(self.sdk, spec.service), spec.method, Mock(return_value=[]))
        self.sdk.records.get_visit_cases.return_value = self.cases
        self.sdk.records.get_case_detail.side_effect = lambda case: CaseDetail(case)
        self.sdk.records.get_soap.side_effect = lambda case: SoapRecord(case, ("synthetic",))

    def run_suite(self, **options):
        config = LiveTestConfig(profile="comprehensive", **options)
        with redirect_stdout(io.StringIO()):
            result = run_atomic_test(self.sdk, config, output_dir=self.root / "run")
        return result

    def test_plan_includes_all_modules_and_profile_limits_roundtrip(self):
        config = LiveTestConfig(profile="comprehensive")
        plan = build_test_plan(config)
        self.assertEqual(len(plan["operations"]), 55)
        self.assertEqual(len(plan["auth_targets"]), 8)
        self.assertEqual((config.max_cases, config.max_items), (6, 8))
        restored = resolve_live_test_config(json_values=config.to_safe_dict(), environ={})
        self.assertEqual(restored, config)
        self.assertEqual(LiveTestConfig(profile="atomic").max_cases, 1)

    def test_recovered_probe_is_not_an_overall_failure_but_query_errors_are(self):
        steps = [
            LiveTestStep("network.prq.https", "ERROR"),
            LiveTestStep("network.prq.https_tls12_compat", "OK"),
            LiveTestStep("auth_check.portal", "OK"),
            LiveTestStep("auth_check.prq", "OK"),
            LiveTestStep("prq.soap", "OK"),
        ]
        self.assertEqual(_overall_status(steps, fatal_auth=False), "OK")
        steps.append(LiveTestStep("prq.numeric", "ERROR"))
        self.assertEqual(_overall_status(steps, fatal_auth=False), "COMPLETED_WITH_ERRORS")

    def test_recovered_tls_probe_does_not_hide_actual_portal_login_error(self):
        steps = [
            LiveTestStep("network.portal.https", "ERROR", issue=ErrorInfo("TLS_EOF", "NETWORK")),
            LiveTestStep("network.portal.https_tls12_compat", "OK"),
            LiveTestStep(
                "auth_check.portal",
                "ERROR",
                issue=ErrorInfo("PORTAL_LOGIN_REJECTED", "AUTHENTICATION"),
            ),
        ]
        self.assertEqual(_overall_status(steps, fatal_auth=True), "AUTHENTICATION_FAILED")

    def test_case_failure_keeps_other_samples_and_history_variants(self):
        def soap(case):
            if case == self.cases[0]:
                raise ParseError("synthetic broken first case")
            return SoapRecord(case, ("synthetic",))

        self.sdk.records.get_soap.side_effect = soap
        result = self.run_suite(doctor_card="TEST", opd_date=date(2026, 1, 20))
        self.assertEqual(self.sdk.records.get_soap.call_count, 6)
        self.assertIn(
            self.cases[-1], [call.args[0] for call in self.sdk.records.get_soap.call_args_list]
        )
        selectors = [call.args[1] for call in self.sdk.orders.get_order_history.call_args_list]
        self.assertEqual(
            [item.category for item in selectors], ["*", "OR", "LAB", "RAD", "PATH", "*"]
        )
        self.assertEqual(selectors[-1].lookback_days, 30)
        self.assertEqual(self.sdk.medications.get_medication_history.call_count, 3)
        self.assertEqual(self.sdk.records.get_numeric_history.call_count, 3)
        self.sdk.surgery.get_schedule.assert_called_once()
        self.sdk.audit.get_unsigned_records.assert_called_once()
        soap_steps = [step for step in result.steps if step.operation == "prq.soap"]
        self.assertEqual([step.status for step in soap_steps].count("ERROR"), 1)
        self.assertEqual([step.status for step in soap_steps].count("OK"), 5)
        inputs = json.loads((self.root / "run/parsed/inputs/prq.order_history.json").read_text())
        self.assertEqual(inputs[1]["filter"]["category"], "OR")

    def test_subsystem_readiness_failure_does_not_hide_other_apps(self):
        def check(*, only):
            if only == ("prq",):
                raise AuthenticationError("synthetic PRQ SSO failure")
            return readiness(only=only)

        self.sdk.auth.check.side_effect = check
        result = self.run_suite(doctor_card="TEST", opd_date=date(2026, 1, 20))
        self.sdk.records.get_visit_cases.assert_not_called()
        self.sdk.patients.get_demographics.assert_called_once()
        self.sdk.surgery.get_schedule.assert_called_once()
        self.sdk.audit.get_unsigned_records.assert_called_once()
        self.assertEqual(result.status, "COMPLETED_WITH_ERRORS")
        self.assertEqual(self.sdk.auth.check.call_count, 8)

    def test_query_auth_failure_blocks_only_remaining_inputs_of_that_operation(self):
        self.sdk.records.get_soap.side_effect = AuthenticationError("synthetic expired SOAP")
        result = self.run_suite(doctor_card="TEST", opd_date=date(2026, 1, 20))
        self.sdk.records.get_soap.assert_called_once()
        self.assertEqual(self.sdk.records.get_numeric_report.call_count, 6)
        self.assertEqual(self.sdk.orders.get_order_history.call_count, 6)
        self.sdk.audit.get_unsigned_records.assert_called_once()
        soap_steps = [step for step in result.steps if step.operation == "prq.soap"]
        self.assertEqual([step.status for step in soap_steps], ["ERROR", *(["BLOCKED"] * 5)])
        self.assertEqual(result.status, "COMPLETED_WITH_ERRORS")

    def test_input_preparation_defect_is_recorded_and_other_modules_run(self):
        def inputs(spec, config, values):
            if spec.key == "prq.soap":
                raise AttributeError("synthetic changed discovery structure")
            return _query_inputs(spec, config, values)

        with patch("vghks_sdk.live.atomic._query_inputs", side_effect=inputs):
            result = self.run_suite()
        self.sdk.records.get_soap.assert_not_called()
        self.assertEqual(self.sdk.records.get_numeric_report.call_count, 6)
        self.assertEqual(
            next(step for step in result.steps if step.name == "prq.soap.prepare").status, "ERROR"
        )

    def test_history_category_failure_keeps_later_categories(self):
        def orders(_mrn, selector):
            if selector.category == "OR":
                raise ParseError("synthetic category defect")
            return []

        self.sdk.orders.get_order_history.side_effect = orders
        result = self.run_suite()
        self.assertEqual(self.sdk.orders.get_order_history.call_count, 6)
        steps = [step for step in result.steps if step.operation == "prq.order_history"]
        self.assertEqual(
            [step.status for step in steps], ["EMPTY", "ERROR", "EMPTY", "EMPTY", "EMPTY", "EMPTY"]
        )

    def test_case_order_references_survive_failed_history_and_detail(self):
        detail_ref = OrderDetailRef(LIVE_TEST_MRN, "c", "O", "1")
        report_ref = OrderReportRef(LIVE_TEST_MRN, "c", "O", "1")
        study_ref = PacsStudyRef(LIVE_TEST_MRN, "r")
        images = tuple(PacsImageRef(LIVE_TEST_MRN, "r", "s", "t", str(i)) for i in range(12))
        self.sdk.orders.get_order_history.side_effect = ParseError("synthetic history failure")
        self.sdk.orders.get_case_orders.return_value = [
            ClinicalOrder(
                LIVE_TEST_MRN,
                "c",
                "O",
                "Unmatched report name",
                detail_ref=detail_ref,
                report_ref=report_ref,
                pacs_ref=study_ref,
            )
        ]
        self.sdk.orders.get_order_detail.side_effect = ParseError("synthetic detail failure")
        self.sdk.orders.get_order_report.return_value = OrderReport(
            report_ref, {}, pacs_refs=(study_ref,)
        )
        self.sdk.orders.get_pacs_study.return_value = PacsStudy(study_ref, images)
        self.sdk.orders.download_pacs_image.return_value = BinaryAsset(
            b"\xff\xd8\xff\xd9", "image/jpeg"
        )
        self.run_suite()
        self.sdk.orders.get_order_report.assert_called_once_with(report_ref)
        self.assertEqual(self.sdk.orders.download_pacs_image.call_count, 8)
        self.assertEqual(len(list((self.root / "run/parsed").rglob("*.jpg"))), 8)

    def test_missing_optional_doctor_is_visible_and_does_not_abort(self):
        result = self.run_suite()
        self.sdk.opd.get_doctor_patients.assert_not_called()
        for key in ("prq.opd_patients", "oppl.surgery_schedule", "audit.unsigned_records"):
            self.assertEqual(
                next(step.status for step in result.steps if step.operation == key), "MISSING"
            )
        coverage = json.loads((self.root / "run/coverage.json").read_text())
        self.assertEqual(len(coverage["operations"]), 55)
        self.assertIn("MISSING", (self.root / "run/RESULTS.txt").read_text())

    def test_doctor_date_samples_stay_inside_configured_range(self):
        self.run_suite(
            weekly_opd_soap=False,
            doctor_card="TEST",
            opd_date=date(2026, 1, 20),
            range_start=date(2026, 1, 18),
            range_end=date(2026, 1, 20),
        )
        dates = [call.args[1] for call in self.sdk.opd.get_doctor_patients.call_args_list]
        self.assertEqual(dates, [date(2026, 1, 20), date(2026, 1, 19), date(2026, 1, 18)])
        self.assertEqual(self.sdk.surgery.get_schedule.call_count, 2)
        self.assertEqual(self.sdk.audit.get_unsigned_records.call_count, 2)

    def test_runner_uses_login_card_for_every_doctor_query_and_retest(self):
        config = LiveTestConfig(profile="comprehensive", output_root=self.root / "results")
        with (
            patch("vghks_sdk.live.runner.VghksSDK") as factory,
            patch("vghks_sdk.live.atomic.run_network_checks"),
            redirect_stdout(io.StringIO()),
        ):
            factory.return_value.__enter__.return_value = self.sdk
            result = execute_live_test(config, PortalCredentials("TEST", "SYNTHETIC_PASSWORD"))
        report = analyze_bundle(result.archive.archive_path, output_dir=self.root / "analysis")
        soap = next(row for row in report["operations"] if row["operation"] == "prq.soap")
        self.assertEqual(soap["live_counts"]["OK"], 6)
        self.assertEqual(soap["live_status"], "VERIFIED")
        recipe = json.loads((self.root / "analysis/retest-config.json").read_text(encoding="utf-8"))
        self.assertEqual(recipe["profile"], "comprehensive")
        self.assertEqual(recipe["doctor_card"], "TEST")
        for callback in (
            self.sdk.opd.get_doctor_patients,
            self.sdk.surgery.get_schedule,
            self.sdk.audit.get_unsigned_records,
        ):
            self.assertTrue(callback.called)
            self.assertTrue(all(call.args[0] == "TEST" for call in callback.call_args_list))

    def test_unauthenticated_base_url_404_is_reachability_evidence(self):
        def response(method, url, **kwargs):
            value = requests.Response()
            value.status_code = 404
            value._content = b"synthetic base URL not mapped"
            value.url = url
            value.request = requests.Request(method, url).prepare()
            return value

        config = LiveTestConfig(
            profile="comprehensive",
            output_root=self.root / "results",
            request_policy=RequestPolicy(min_delay_seconds=0, max_delay_seconds=0, max_attempts=1),
        )
        with (
            patch("vghks_sdk.live.runner.VghksSDK") as factory,
            patch("vghks_sdk.live.preflight._dns", return_value={"addresses": ["127.0.0.1"]}),
            patch("vghks_sdk.live.preflight._tcp", return_value={"connected": True}),
            patch("vghks_sdk.live.preflight.get_environ_proxies", return_value={}),
            patch("requests.sessions.Session.request", side_effect=response),
            redirect_stdout(io.StringIO()),
        ):
            factory.return_value.__enter__.return_value = self.sdk
            result = execute_live_test(config, PortalCredentials("TEST", "SYNTHETIC_PASSWORD"))
        self.assertTrue(
            all(
                step.status == "OK"
                for step in result.result.steps
                if step.name.startswith("network.")
            )
        )
        report = analyze_bundle(result.archive.archive_path, output_dir=self.root / "analysis")
        self.assertNotEqual(report["root_cause"]["code"], "HTTP_404")

    def test_network_faults_collect_every_service_without_credentials(self):
        steps = []
        with (
            patch(
                "vghks_sdk.live.preflight._dns",
                side_effect=RequestError("DNS", code="NETWORK_DNS_FAILED"),
            ) as dns,
            patch(
                "vghks_sdk.live.preflight._tcp",
                side_effect=RequestError("TCP", code="NETWORK_TCP_FAILED"),
            ) as tcp,
            patch("vghks_sdk.live.preflight._https", return_value={"http_status": 302}) as https,
            redirect_stdout(io.StringIO()),
        ):
            run_network_checks(SDKSettings(), steps, root=self.root)
        self.assertEqual((dns.call_count, tcp.call_count, https.call_count), (8, 8, 8))
        self.assertEqual(len(steps), 24)
        self.assertEqual(sum(step.status == "OK" for step in steps), 8)

    def test_https_probe_keeps_tls_and_does_not_follow_redirects_or_retry(self):
        with (
            patch(
                "requests.sessions.Session.request",
                side_effect=requests.exceptions.SSLError("synthetic"),
            ) as network,
            self.assertRaises(RequestError),
        ):
            _https(SDKSettings(), "https://example.invalid/")
        self.assertEqual(network.call_count, 1)
        self.assertIs(network.call_args.kwargs["verify"], True)
        self.assertIs(network.call_args.kwargs["allow_redirects"], False)
        self.assertNotIn("data", network.call_args.kwargs)

    def test_selected_patient_diagnostics_retain_optional_mis(self):
        for include_mis in (False, True):
            with (
                self.subTest(include_mis=include_mis),
                patch("vghks_sdk.live.preflight._dns", return_value={"addresses": ["127.0.0.1"]}),
                patch("vghks_sdk.live.preflight._tcp", return_value={"connected": True}),
                patch("vghks_sdk.live.preflight._https", return_value={"http_status": 200}),
                redirect_stdout(io.StringIO()),
            ):
                choices = run_network_checks(
                    SDKSettings(), [], root=self.root, only=("webmaas",), include_mis=include_mis
                )
            expected = {"portal", "sectord", "webmaas"}
            if include_mis:
                expected.add("mis")
            self.assertEqual(set(choices), expected)

    def test_dns_lookup_has_a_bounded_wait(self):
        with (
            patch("vghks_sdk.live.preflight.threading.Thread"),
            self.assertRaises(RequestError) as failure,
        ):
            _dns("example.invalid", 443, 0.001)
        self.assertEqual(failure.exception.info.code, "NETWORK_DNS_TIMEOUT")

    def test_failed_https_collects_protocol_and_proxy_variants_for_all_services(self):
        steps = []

        def probe(settings, url, *, direct=False, tls_profile=TLS_DEFAULT, **kwargs):
            if not direct and tls_profile == TLS_DEFAULT:
                raise RequestError("synthetic TLS EOF", code="TLS_EOF")
            return {"http_status": 200}

        with (
            patch("vghks_sdk.live.preflight._dns", return_value={"addresses": ["127.0.0.1"]}),
            patch("vghks_sdk.live.preflight._tcp", return_value={"connected": True}),
            patch(
                "vghks_sdk.live.preflight.get_environ_proxies",
                return_value={"https": "http://proxy.invalid"},
            ),
            patch("vghks_sdk.live.preflight._https", side_effect=probe) as https,
            redirect_stdout(io.StringIO()),
        ):
            run_network_checks(SDKSettings(), steps, root=self.root)
        self.assertEqual(https.call_count, 32)
        self.assertEqual(len(steps), 48)
        self.assertEqual(sum(step.status == "ERROR" for step in steps), 8)
        for app in (
            "portal",
            "prq",
            "sectord",
            "webmaas",
            "oppl",
            "audit",
            "oppl_records",
            "review",
        ):
            self.assertEqual(
                {step.name for step in steps if step.name.startswith(f"network.{app}.https")},
                {
                    f"network.{app}.{phase}"
                    for phase in ("https", "https_tls12", "https_direct", "https_direct_tls12")
                },
            )

    def test_direct_probe_preserves_ca_and_writes_context_even_on_failure(self):
        ca_path = requests.certs.where()
        context_path = self.root / "context.json"

        def request(session, method, url, **kwargs):
            self.assertFalse(session.trust_env)
            self.assertEqual(kwargs["verify"], ca_path)
            self.assertEqual(method, "GET")
            self.assertFalse(kwargs["allow_redirects"])
            self.assertLessEqual(kwargs["timeout"][1], 15)
            prepared = requests.Request(method, url).prepare()
            self.assertNotIn("Authorization", session.auth(prepared).headers)
            raise requests.exceptions.SSLError("EOF occurred in violation of protocol")

        with (
            patch.dict(
                "os.environ",
                {
                    "REQUESTS_CA_BUNDLE": ca_path,
                    "HTTPS_PROXY": "http://private-user:secret@proxy.invalid",
                },
            ),
            patch("requests.sessions.Session.request", autospec=True, side_effect=request),
            self.assertRaises(RequestError) as failure,
        ):
            _https(
                SDKSettings(),
                "https://example.invalid/",
                tls12_only=True,
                direct=True,
                context_path=context_path,
            )
        self.assertEqual(failure.exception.info.code, "TLS_EOF")
        context = json.loads(context_path.read_text(encoding="utf-8"))
        self.assertEqual(context["route"], "DIRECT")
        self.assertEqual(context["tls_protocol"], "TLSv1.2")
        self.assertTrue(context["certificate_verification"])
        self.assertFalse(context["portal_credentials_sent"])
        self.assertNotIn("secret", context_path.read_text(encoding="utf-8"))

    def test_successful_compatibility_profile_is_applied_before_login_in_same_run(self):
        applied = {}

        def configure(app, *, tls_profile, direct):
            applied[app] = (tls_profile, direct)

        def probe(settings, url, *, tls_profile=TLS_DEFAULT, **kwargs):
            if tls_profile != TLS12_COMPAT:
                raise RequestError("synthetic old TLS server", code="TLS_EOF")
            return {"http_status": 302}

        def check(*, only):
            # SectOrd and OPPL share one HTTPS host/port.
            self.assertEqual(len(applied), 6)
            return readiness(only=only)

        self.sdk.configure_connection = configure
        self.sdk.auth.check.side_effect = check
        with (
            patch("vghks_sdk.live.preflight._dns", return_value={}),
            patch("vghks_sdk.live.preflight._tcp", return_value={}),
            patch("vghks_sdk.live.preflight.get_environ_proxies", return_value={}),
            patch("vghks_sdk.live.preflight._https", side_effect=probe),
            patch("vghks_sdk.live.preflight.probe_schannel") as native,
            redirect_stdout(io.StringIO()),
        ):
            result = run_atomic_test(
                self.sdk,
                LiveTestConfig(
                    profile="comprehensive", doctor_card="TEST", opd_date=date(2026, 1, 20)
                ),
                output_dir=self.root,
                settings=SDKSettings(),
            )
        native.assert_not_called()
        self.assertEqual(set(applied.values()), {(TLS12_COMPAT, False)})
        self.sdk.records.get_visit_cases.assert_called_once()
        self.assertNotEqual(result.status, "CONNECTIVITY_FAILED")
        choices = json.loads((self.root / "parsed/network/selected_profiles.json").read_text())
        self.assertTrue(all(row["applied"] for row in choices.values()))
        self.assertEqual(choices["oppl"]["shared_origin_with"], "sectord")

    def test_connection_configuration_failure_does_not_abort_other_apps(self):
        def configure(app, **kwargs):
            if app == "prq":
                raise RuntimeError("synthetic local configuration failure")

        def probe(settings, url, *, tls_profile=TLS_DEFAULT, **kwargs):
            if tls_profile == TLS_DEFAULT:
                raise RequestError("EOF", code="TLS_EOF")
            return {"http_status": 200}

        with (
            patch("vghks_sdk.live.preflight._dns", return_value={}),
            patch("vghks_sdk.live.preflight._tcp", return_value={}),
            patch("vghks_sdk.live.preflight.get_environ_proxies", return_value={}),
            patch("vghks_sdk.live.preflight._https", side_effect=probe),
            redirect_stdout(io.StringIO()),
        ):
            choices = run_network_checks(
                SDKSettings(),
                [],
                root=self.root,
                configure_connection=configure,
            )
        self.assertFalse(choices["prq"]["applied"])
        self.assertTrue(choices["audit"]["applied"])

    def test_unverified_recovery_is_applied_before_login_and_queries_and_recorded_in_zip(self):
        applied = {}

        def configure(app, **options):
            applied[app] = options

        def probe(settings, url, *, verify_certificate=True, **kwargs):
            if verify_certificate:
                raise RequestError("synthetic certificate failure", code="TLS_VERIFY_FAILED")
            return {"http_status": 200}

        def check(*, only):
            self.assertEqual(len(applied), 6)  # OPPL shares the SectOrd origin.
            self.assertTrue(all(row["verify_certificate"] is False for row in applied.values()))
            return readiness(only=only)

        self.sdk.configure_connection = configure
        self.sdk.auth.check.side_effect = check
        config = LiveTestConfig(profile="comprehensive", output_root=self.root / "results")
        with (
            patch("vghks_sdk.live.runner.VghksSDK") as factory,
            patch("vghks_sdk.live.preflight._dns", return_value={}),
            patch("vghks_sdk.live.preflight._tcp", return_value={}),
            patch("vghks_sdk.live.preflight.get_environ_proxies", return_value={}),
            patch("vghks_sdk.live.preflight._https", side_effect=probe),
            patch("vghks_sdk.live.preflight.probe_schannel", return_value={}),
            redirect_stdout(io.StringIO()),
        ):
            factory.return_value.__enter__.return_value = self.sdk
            result = execute_live_test(config, PortalCredentials("TEST", "SYNTHETIC_PASSWORD"))
        self.sdk.records.get_visit_cases.assert_called_once()
        self.sdk.records.get_soap.assert_called()
        self.sdk.opd.get_doctor_patients.assert_called()
        with zipfile.ZipFile(result.archive.archive_path) as archive:
            coverage = json.loads(archive.read("coverage.json"))
            self.assertEqual(len(coverage["unverified_tls_services"]), 8)
            mode_line = next(
                line
                for line in archive.read("RESULTS.txt").decode().splitlines()
                if line.startswith("HTTPS without certificate verification: ")
            )
            self.assertEqual(
                set(mode_line.split(": ", 1)[1].split(", ")),
                {"portal", "prq", "sectord", "webmaas", "oppl", "audit", "oppl_records", "review"},
            )
            profiles = json.loads(archive.read("parsed/network/selected_profiles.json"))
            self.assertTrue(
                all(
                    row["applied"] and row["certificate_verification"] is False
                    for row in profiles.values()
                )
            )
        report = analyze_bundle(result.archive.archive_path, output_dir=self.root / "analysis")
        self.assertEqual(len(report["unverified_tls_services"]), 8)
        soap = next(row for row in report["operations"] if row["operation"] == "prq.soap")
        self.assertEqual(soap["live_status"], "VERIFIED")
        self.assertEqual(soap["live_counts"]["OK"], 6)

    def test_opt_out_collects_unverified_comparison_but_does_not_apply_it(self):
        def probe(settings, url, *, verify_certificate=True, **kwargs):
            if verify_certificate:
                raise RequestError("synthetic EOF", code="TLS_EOF")
            return {"http_status": 200}

        configure = Mock()
        with (
            patch("vghks_sdk.live.preflight._dns", return_value={}),
            patch("vghks_sdk.live.preflight._tcp", return_value={}),
            patch("vghks_sdk.live.preflight.get_environ_proxies", return_value={}),
            patch("vghks_sdk.live.preflight._https", side_effect=probe),
            patch("vghks_sdk.live.preflight.probe_schannel", return_value={}),
            redirect_stdout(io.StringIO()),
        ):
            choices = run_network_checks(
                SDKSettings(),
                [],
                root=self.root,
                configure_connection=configure,
                allow_unverified_tls=False,
            )
        configure.assert_not_called()
        self.assertEqual(len(choices), 8)
        for choice in choices.values():
            self.assertFalse(choice["applied"])
            self.assertTrue(choice["unverified_probe"]["anonymous_probe"])
            self.assertFalse(choice["unverified_probe"]["applied_to_sdk"])

    def test_unverified_probe_stays_anonymous_and_records_policy_despite_environment_ca(self):
        path = self.root / "unverified-context.json"

        def request(session, method, url, **kwargs):
            self.assertEqual(method, "GET")
            self.assertFalse(kwargs["verify"])
            self.assertFalse(kwargs["allow_redirects"])
            self.assertTrue(session.trust_env)
            self.assertFalse(session.cookies)
            self.assertNotIn("data", kwargs)
            prepared = requests.Request(method, url).prepare()
            self.assertNotIn("Authorization", session.auth(prepared).headers)
            raise requests.exceptions.SSLError("EOF occurred in violation of protocol")

        with (
            patch.dict("os.environ", {"REQUESTS_CA_BUNDLE": "does-not-exist.pem"}),
            patch(
                "requests.sessions.Session.request", autospec=True, side_effect=request
            ) as network,
            self.assertRaises(RequestError) as failure,
        ):
            _https(
                SDKSettings(),
                "https://example.invalid/",
                verify_certificate=False,
                context_path=path,
            )
        self.assertEqual(network.call_count, 1)
        self.assertEqual(failure.exception.info.code, "TLS_EOF")
        context = json.loads(path.read_text())
        self.assertFalse(context["certificate_verification"])
        self.assertFalse(context["portal_credentials_sent"])
        self.assertEqual(context["trust_mode"], "UNVERIFIED")

    def test_real_runner_tls_failure_still_probes_all_services_and_plain_zip_replays(self):
        config = LiveTestConfig(
            profile="comprehensive",
            output_root=self.root / "results",
            request_policy=RequestPolicy(min_delay_seconds=0, max_delay_seconds=0, max_attempts=1),
        )
        with (
            patch("vghks_sdk.live.preflight._dns", return_value={"addresses": ["127.0.0.1"]}),
            patch("vghks_sdk.live.preflight._tcp", return_value={"connected": True}),
            patch("vghks_sdk.live.preflight.get_environ_proxies", return_value={}),
            patch(
                "vghks_sdk.live.preflight.probe_schannel",
                side_effect=RequestError(
                    "synthetic Windows TLS failure", code="NETWORK_SCHANNEL_12175"
                ),
            ),
            patch("vghks_sdk.live.preflight.platform.system", return_value="Windows"),
            patch(
                "requests.sessions.Session.request",
                side_effect=requests.exceptions.SSLError("synthetic TLS"),
            ) as network,
            redirect_stdout(io.StringIO()),
        ):
            result = execute_live_test(config, PortalCredentials("TEST", "SYNTHETIC_PASSWORD"))
        self.assertEqual(network.call_count, 49)
        self.assertEqual(result.status, "CONNECTIVITY_FAILED")
        self.assertIsNotNone(result.archive)
        with zipfile.ZipFile(result.archive.archive_path) as archive:
            self.assertTrue(all(not info.flag_bits & 1 for info in archive.infolist()))
            summary = json.loads(archive.read("run_summary.json"))
            self.assertEqual(
                sum(step["name"].startswith("network.") for step in summary["steps"]), 72
            )
            self.assertIn("coverage.json", archive.namelist())
            self.assertIn("RESULTS.txt", archive.namelist())
        report = analyze_bundle(result.archive.archive_path, output_dir=self.root / "analysis")
        self.assertEqual(report["root_cause"]["code"], "NETWORK_TLS_FAILED")
        recipe = json.loads((self.root / "analysis/retest-config.json").read_text(encoding="utf-8"))
        self.assertEqual(recipe["profile"], "comprehensive")
        self.assertFalse(recipe["only_operations"])

    def test_first_run_only_prompts_for_login_and_defaults_doctor_queries(self):
        args = build_parser().parse_args(["--profile", "comprehensive"])
        with (
            patch("builtins.input", side_effect=["TEST-USER"]) as prompt,
            patch("getpass.getpass", return_value="TEST-PASSWORD") as password,
            patch.dict("os.environ", {"VGHKS_TEST_MRN": "0000000"}, clear=True),
            patch("vghks_sdk.live_test_app._offer_incomplete_packaging"),
            patch("vghks_sdk.live_test_app.create_live_test_bundle"),
            patch(
                "vghks_sdk.live_test_app.execute_live_test",
                return_value=SimpleNamespace(exit_code=0),
            ) as execute,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(run_live_test_namespace(args, force_interactive=True), 0)
        prompt.assert_called_once()
        password.assert_called_once()
        config = execute.call_args.args[0]
        self.assertEqual(config.doctor_card, "TEST-USER")
        self.assertEqual(config.opd_date, date.today())
        self.assertEqual((config.range_end - config.range_start).days, 29)

    def test_start_needs_no_yes_confirmation_and_configured_doctor_needs_no_prompt(self):
        args = build_parser().parse_args(
            ["--profile", "comprehensive", "--doctor-card", "TEST", "--date", "2026-01-20"]
        )
        with (
            patch("builtins.input", side_effect=["TEST-USER"]) as prompt,
            patch("getpass.getpass", return_value="TEST-PASSWORD"),
            patch.dict("os.environ", {"VGHKS_TEST_MRN": "0000000"}, clear=True),
            patch("vghks_sdk.live_test_app._offer_incomplete_packaging"),
            patch("vghks_sdk.live_test_app.create_live_test_bundle"),
            patch(
                "vghks_sdk.live_test_app.execute_live_test",
                return_value=SimpleNamespace(exit_code=0),
            ) as execute,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(run_live_test_namespace(args, force_interactive=True), 0)
        prompt.assert_called_once()
        self.assertEqual(execute.call_args.args[1].username, "TEST-USER")
        self.assertEqual(execute.call_args.args[0].doctor_card, "TEST")

    def test_selected_atomic_doctor_query_can_default_to_login_card(self):
        config = LiveTestConfig(profile="atomic", only_operations=("oppl.surgery_schedule",))
        resolved = config.with_default_doctor("TEST-USER").validate_for_execution()
        self.assertEqual(resolved.doctor_card, "TEST-USER")
        self.assertEqual(resolved.opd_date, date.today())

    def test_exe_writes_zip_next_to_it_even_with_a_different_working_directory(self):
        exe_directory = self.root / "portable"
        exe_directory.mkdir()
        working_directory = self.root / "working"
        working_directory.mkdir()
        args = build_parser().parse_args(["--profile", "auth", "--non-interactive"])
        with (
            patch(
                "vghks_sdk.live_test_app.sys.executable", str(exe_directory / "vghks-live-test.exe")
            ),
            patch("vghks_sdk.live_test_app.Path.cwd", return_value=working_directory),
            patch.dict(
                "os.environ",
                {"VGHKS_TEST_MRN": "0000000", "VGHKS_USERNAME": "TEST", "VGHKS_PASSWORD": "SYNTHETIC_PASSWORD"},
                clear=True,
            ),
            patch("vghks_sdk.live.runner.VghksSDK") as factory,
            redirect_stdout(io.StringIO()),
        ):
            factory.return_value.__enter__.return_value = self.sdk
            self.assertEqual(run_live_test_namespace(args, executable_mode=True), 0)
        archives = list(exe_directory.glob("vghks-live-test-*.zip"))
        self.assertEqual(len(archives), 1)
        self.assertFalse(archives[0].with_suffix(".zip.sha256").exists())
        self.assertFalse(list(working_directory.glob("*.zip")))
        report = analyze_bundle(archives[0], output_dir=self.root / "exe-analysis")
        self.assertEqual(report["bundle"]["status"], "READABLE")

    def test_double_click_uses_builtin_round_even_with_old_sidecar_and_profile_env(self):
        sidecar = self.root / "live-test-config.json"
        sidecar.write_text('{"profile": "atomic", "only_operations": ["prq.soap"]}')
        with (
            patch("vghks_sdk.live_test_app.Path.cwd", return_value=self.root),
            patch.dict("os.environ", {"VGHKS_LIVE_PROFILE": "auth", "VGHKS_TEST_MRN": "0000000"}, clear=True),
            patch("vghks_sdk.live_test_app.run_live_test_namespace", return_value=0) as execute,
            patch("builtins.input", return_value=""),
        ):
            self.assertEqual(main([]), 0)
        self.assertIsNone(execute.call_args.args[0].config)
        self.assertTrue(execute.call_args.args[0].bundled_round)
        self.assertEqual(execute.call_args.args[0].profile, "comprehensive")


if __name__ == "__main__":
    unittest.main()
