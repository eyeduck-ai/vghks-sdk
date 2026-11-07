"""Manufactured PACS return bundles: evidence review never replays a handoff."""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from tests.test_pacs_launch_parsing import (
    CLIENT_IP,
    LOGIN_ID,
    MRN,
    REQUEST_NO,
    STUDY_UID,
    bootstrap_html,
    viewer_html,
)
from vghks_sdk.core.errors import AuthenticationError, ConfigurationError
from vghks_sdk.integrations.smartiris import (
    SmartIrisAvailability,
    SmartIrisExchange,
    SmartIrisLaunchResult,
    SmartIrisStepResult,
)
from vghks_sdk.live.pacs_launch import run_pacs_launch_test
from vghks_sdk.live.pacs_launch_capture import PacsLaunchCapture
from vghks_sdk.models.pacs_launch import SMARTWONDER_BASE_URL, PacsLaunchRef
from vghks_sdk.offline.analyze import analyze_bundle
from vghks_sdk.parsing.pacs_launch import parse_pacs_launch_bootstrap, parse_pacs_launch_context

SECRET = "PRIVATE_EVIDENCE_MUST_NOT_LEAK"


class PacsLaunchOfflineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.input = self.root / "bundle"
        self.output = self.root / "analysis"
        self.reference = PacsLaunchRef(MRN, REQUEST_NO)
        self.context = parse_pacs_launch_context(
            parse_pacs_launch_bootstrap(bootstrap_html(), reference=self.reference,
                                        expected_login_id=LOGIN_ID),
            viewer_html(),
        )
        blocker = patch("socket.socket", side_effect=AssertionError("network prohibited"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def write_json(self, relative, value):
        path = self.input / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def read_json(self, relative):
        return json.loads((self.input / relative).read_text(encoding="utf-8"))

    @staticmethod
    def availability(status="AVAILABLE"):
        return SmartIrisAvailability(
            status=status, installation="PRESENT", process="RUNNING",
            listener="LISTENING", owner="MATCH_EXPECTED_PATH" if status == "AVAILABLE" else "UNKNOWN",
            port=15516, issues=() if status == "AVAILABLE" else ("OWNER_UNCONFIRMED",),
        )

    @staticmethod
    def record_page(capture, endpoint, params, text):
        request = requests.Request("GET", SMARTWONDER_BASE_URL + endpoint,
                                   params=params, headers={"Cookie": SECRET}).prepare()
        response = requests.Response()
        response.url, response.request, response.status_code = request.url, request, 200
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        response._content = text.encode("utf-8")
        capture.record_response(response=response, attempt=1, max_attempts=1,
                                throttle_delay_seconds=0, elapsed_seconds=0,
                                will_retry=False)

    def make_bundle(self, *, open_viewer=True, login_error=False, availability=None,
                    native_fail_after=None):
        self.sdk = Mock()
        if login_error:
            self.sdk.auth.login.side_effect = AuthenticationError(SECRET, code="LOGIN_REJECTED")
        with PacsLaunchCapture(self.input) as capture:
            def prepare(reference):
                self.assertEqual(reference, self.reference)
                operation = capture.start_operation(name="prepare_pacs_launch", app_key="smartwonder")
                try:
                    self.record_page(capture, "/Query/qryExternalAP.jsp", {
                        "LoginID": LOGIN_ID, "PatID": MRN, "AccNo": REQUEST_NO, "LoginLevel": "RAD",
                    }, bootstrap_html())
                    self.record_page(capture, "/Viewer/vwrIris.jsp", {
                        "LoginID": LOGIN_ID, "WonderClientIP": CLIENT_IP,
                    }, viewer_html())
                finally:
                    capture.finish_operation(operation)
                return self.context

            self.sdk.orders.prepare_pacs_launch.side_effect = prepare

            def client_factory(**options):
                def open_study(context):
                    commands = (
                        ("login", {"id": context.login_id, "pwd": ""}),
                        ("wonderurl", {"url": context.wonder_url + "/"}),
                        ("setanonymousmode", {"mode": "0"}),
                        ("cmove", {"cmoveurl": context.cmove_url, "url": context.wado_url,
                                   "dicomserver": "SK", "level": "STUDY",
                                   "uid": context.study_uid, "command": "1"}),
                    )
                    steps = []
                    for index, (name, params) in enumerate(commands, 1):
                        failed = index == native_fail_after
                        body = b"" if failed else b"ok"
                        status = None if failed else 200
                        code = "SMARTIRIS_TIMEOUT" if failed else ""
                        request = requests.Request("GET", "http://127.0.0.1:15516/" + name,
                                                   params={**params, "timestamp": str(index)}).prepare()
                        options["capture"](SmartIrisExchange(
                            endpoint_path="/" + name, request_url=request.url, http_status=status,
                            response_headers={} if failed else {"Content-Length": "2", "X-Private": SECRET},
                            body=body, complete=not failed, error_code=code,
                        ))
                        steps.append(SmartIrisStepResult(
                            name, "OUTCOME_UNKNOWN" if failed else "HTTP_RESPONSE_RECEIVED",
                            status, len(body), "" if failed else sha256(body).hexdigest(), code,
                        ))
                        if failed:
                            return SmartIrisLaunchResult("OUTCOME_UNKNOWN", tuple(steps), code)
                    return SmartIrisLaunchResult("HANDOFF_SENT", tuple(steps))

                self.client = SimpleNamespace(check_availability=Mock(return_value=availability or self.availability()),
                                              open_study=Mock(side_effect=open_study), close=Mock())
                return self.client

            summary = run_pacs_launch_test(
                self.sdk, self.reference, output_dir=self.input, open_viewer=open_viewer,
                client_factory=client_factory, observe=lambda: "MATCHED", raw_capture=capture,
            )
        self.write_json("environment.json", {"build": {"sdk_version": "0.24.1"}, "private": SECRET})
        self.write_json("run_config.json", {"schema_version": 1, "profile": "pacs_launch",
                                            "test_mrn": MRN, "request_no": REQUEST_NO,
                                            "selection_mode": "EXPLICIT_REFERENCE",
                                            "open_viewer": open_viewer, "credentials_included": False,
                                            "synthetic_loopback": False, "private": SECRET})
        return summary

    def inspect(self, source=None):
        with patch("vghks_sdk.integrations.smartiris.SmartIrisClient.open_study",
                   side_effect=AssertionError("native handoff prohibited")):
            report = analyze_bundle(source or self.input, output_dir=self.output)
        self.assertEqual(report["profile"], "pacs_launch")
        for value in (MRN, REQUEST_NO, LOGIN_ID, CLIENT_IP, STUDY_UID, SECRET, SMARTWONDER_BASE_URL):
            self.assertNotIn(value, json.dumps(report))
            for path in self.output.iterdir():
                if path.is_file():
                    self.assertNotIn(value, path.read_text(encoding="utf-8"))
        retest = json.loads((self.output / "retest-config.json").read_text(encoding="utf-8"))
        self.assertEqual(retest["kind"], "MANUAL_REVIEW_ONLY")
        self.assertIs(retest["automatic_execution"], False)
        self.assertEqual((retest["password_posts"], retest["native_handoffs"]), (0, 0))
        return report

    def test_explicit_reference_complete_handoff_is_verified_without_network(self):
        self.make_bundle()
        report = self.inspect()
        self.assertEqual(report["bundle_status"], "HANDOFF_SENT")
        self.assertEqual(report["analysis_status"], "OK")
        recorded = report["pacs_launch_test"]["recorded"]
        self.assertEqual((recorded["prepare"], recorded["handoff"], recorded["manual_observation"]),
                         ("OK", "HANDOFF_SENT", "MATCHED"))
        self.assertEqual(report["pacs_launch_test"]["native"]["response_count"], 4)

    def test_prepare_only_never_requires_availability_or_native_evidence(self):
        self.make_bundle(open_viewer=False)
        report = self.inspect()
        self.assertEqual(report["bundle_status"], "PREPARED")
        self.assertEqual(report["analysis_status"], "OK")
        self.assertEqual(report["pacs_launch_test"]["recorded"]["native_calls"], 0)
        self.assertEqual(report["pacs_launch_test"]["native"]["response_count"], 0)

    def test_login_failure_remains_failure_with_zero_prepare_or_native(self):
        self.make_bundle(login_error=True)
        report = self.inspect()
        self.assertEqual(report["bundle_status"], "ERROR")
        self.assertEqual(report["analysis_status"], "NEEDS_ATTENTION")
        self.assertEqual(report["pacs_launch_test"]["recorded"]["login"], "ERROR")
        self.sdk.orders.prepare_pacs_launch.assert_not_called()

    def test_unknown_availability_preserves_unknown_and_stopped_handoff(self):
        self.make_bundle(availability=self.availability("UNKNOWN"))
        report = self.inspect()
        self.assertEqual(report["bundle_status"], "BRIDGE_UNCONFIRMED")
        self.assertEqual(report["analysis_status"], "NEEDS_ATTENTION")
        self.assertEqual(report["pacs_launch_test"]["availability"]["status"], "UNKNOWN")
        self.assertEqual(report["pacs_launch_test"]["native"]["response_count"], 0)

    def test_legacy_missing_availability_is_a_gap_not_new_installed_evidence(self):
        self.make_bundle()
        summary = self.read_json("run_summary.json")
        summary.pop("bridge_availability")
        summary["sdk_version"] = "0.24.0"
        self.write_json("run_summary.json", summary)
        self.write_json("environment.json", {"build": {"sdk_version": "0.24.0"}})
        plan = self.read_json("test_plan.json")
        plan["sdk_version"] = "0.24.0"
        plan.pop("local_availability_checks")
        self.write_json("test_plan.json", plan)
        report = self.inspect()
        self.assertEqual(report["bundle_status"], "HANDOFF_SENT")
        self.assertEqual(report["analysis_status"], "COMPLETED_WITH_GAPS")
        self.assertNotEqual(report["pacs_launch_test"]["availability"]["status"], "AVAILABLE")

    def test_partial_native_response_does_not_become_success(self):
        self.make_bundle(native_fail_after=3)
        report = self.inspect()
        self.assertEqual(report["bundle_status"], "ERROR")
        self.assertEqual(report["analysis_status"], "NEEDS_ATTENTION")
        self.assertEqual(report["pacs_launch_test"]["recorded"]["handoff"], "OUTCOME_UNKNOWN")
        self.assertEqual(report["pacs_launch_test"]["native"]["response_count"], 3)

    def test_missing_body_is_not_hidden_by_successful_summary(self):
        self.make_bundle()
        (self.input / "private/native/004.body").unlink()
        report = self.inspect()
        self.assertEqual(report["bundle_status"], "HANDOFF_SENT")
        self.assertEqual(report["analysis_status"], "NEEDS_ATTENTION")
        self.assertTrue(report["pacs_launch_test"]["issues"])

    def test_response_hash_mismatch_is_detected(self):
        self.make_bundle()
        (self.input / "private/native/004.body").write_bytes(b"changed")
        report = self.inspect()
        self.assertEqual(report["analysis_status"], "NEEDS_ATTENTION")

    def test_native_summary_count_mismatch_does_not_override_raw_evidence(self):
        self.make_bundle()
        summary = self.read_json("run_summary.json")
        summary["native_calls"] = 0
        self.write_json("run_summary.json", summary)
        report = self.inspect()
        self.assertEqual(report["analysis_status"], "NEEDS_ATTENTION")
        self.assertEqual(report["pacs_launch_test"]["recorded"]["native_calls"], 0)
        self.assertEqual(report["pacs_launch_test"]["native"]["response_count"], 4)

    def test_selected_patient_mismatch_invalidates_prepared_evidence(self):
        self.make_bundle()
        self.write_json("private/selected_order.json", {"mrn": "1111111", "request_no": REQUEST_NO})
        report = self.inspect()
        self.assertEqual(report["analysis_status"], "NEEDS_ATTENTION")

    def test_native_study_parameter_mismatch_is_not_a_verified_handoff(self):
        self.make_bundle()
        metadata = self.read_json("private/native/004.json")
        address = urlsplit(metadata["request_url"])
        params = dict(parse_qsl(address.query, keep_blank_values=True))
        params["uid"] = "1.2.3.99"
        metadata["request_url"] = urlunsplit(address._replace(query=urlencode(params)))
        self.write_json("private/native/004.json", metadata)
        report = self.inspect()
        self.assertEqual(report["analysis_status"], "NEEDS_ATTENTION")

    def test_prepare_pages_from_different_operations_cannot_be_paired(self):
        self.make_bundle()
        path = self.input / "capture_manifest.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        rows[-1]["sdk_operation_id"] = "op-000099"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        report = self.inspect()
        self.assertEqual(report["analysis_status"], "NEEDS_ATTENTION")

    def test_unrecognized_summary_strings_are_not_echoed_into_safe_reports(self):
        self.make_bundle()
        summary = self.read_json("run_summary.json")
        summary["manual_observation"] = SECRET
        summary["bridge_availability"]["issues"] = [SECRET]
        summary["native_result"]["error_code"] = SECRET
        self.write_json("run_summary.json", summary)
        report = self.inspect()
        self.assertIn(report["analysis_status"], {"COMPLETED_WITH_GAPS", "NEEDS_ATTENTION"})

    def test_successful_overall_status_cannot_hide_failed_stages(self):
        original = self.make_bundle(open_viewer=False)
        for stage in ("login", "prepare"):
            with self.subTest(stage=stage):
                summary = {**original, stage: "ERROR", "issue": None, "exit_code": 0}
                self.write_json("run_summary.json", summary)
                report = self.inspect()
                self.assertEqual(report["analysis_status"], "NEEDS_ATTENTION")
                self.assertEqual(report["pacs_launch_test"]["recorded"][stage], "ERROR")

    def test_orphan_native_body_cannot_be_ignored(self):
        self.make_bundle()
        (self.input / "private/native/005.body").write_bytes(b"ok")
        report = self.inspect()
        self.assertEqual(report["analysis_status"], "NEEDS_ATTENTION")
        self.assertNotEqual(report["pacs_launch_test"]["native"]["status"], "VERIFIED")

    def test_unrecognized_native_metadata_name_cannot_be_ignored(self):
        self.make_bundle()
        self.write_json("private/native/extra.json", self.read_json("private/native/004.json"))
        report = self.inspect()
        self.assertEqual(report["analysis_status"], "NEEDS_ATTENTION")
        self.assertNotEqual(report["pacs_launch_test"]["native"]["status"], "VERIFIED")

    def test_context_summary_mismatch_is_not_verified_preparation(self):
        self.make_bundle(open_viewer=False)
        summary = self.read_json("parsed/launch_context_summary.json")
        summary["study_count"] = 2
        self.write_json("parsed/launch_context_summary.json", summary)
        report = self.inspect()
        self.assertEqual(report["analysis_status"], "NEEDS_ATTENTION")
        self.assertNotEqual(report["pacs_launch_test"]["prepare"]["status"], "VERIFIED")

    def test_raw_response_length_mismatch_is_not_verified_preparation(self):
        self.make_bundle(open_viewer=False)
        path = self.input / "capture_manifest.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        rows[0]["response"]["body_length"] += 1
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        report = self.inspect()
        self.assertEqual(report["analysis_status"], "NEEDS_ATTENTION")
        self.assertNotEqual(report["pacs_launch_test"]["prepare"]["status"], "VERIFIED")

    def test_malformed_raw_request_reports_problem_without_crashing(self):
        self.make_bundle(open_viewer=False)
        path = self.input / "capture_manifest.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        rows[0]["request"] = [SECRET]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        report = self.inspect()
        self.assertEqual(report["analysis_status"], "NEEDS_ATTENTION")

    def test_malformed_native_result_reports_problem_without_crashing(self):
        self.make_bundle()
        summary = self.read_json("run_summary.json")
        summary["native_result"] = [SECRET]
        self.write_json("run_summary.json", summary)
        report = self.inspect()
        self.assertEqual(report["analysis_status"], "NEEDS_ATTENTION")

    def test_malformed_availability_field_reports_problem_without_crashing(self):
        self.make_bundle()
        summary = self.read_json("run_summary.json")
        summary["bridge_availability"]["status"] = [SECRET]
        self.write_json("run_summary.json", summary)
        report = self.inspect()
        self.assertEqual(report["analysis_status"], "NEEDS_ATTENTION")

    def test_malformed_issue_reports_problem_without_crashing(self):
        self.make_bundle()
        summary = self.read_json("run_summary.json")
        summary["issue"] = [SECRET]
        self.write_json("run_summary.json", summary)
        report = self.inspect()
        self.assertEqual(report["analysis_status"], "NEEDS_ATTENTION")

    def test_packaging_failure_preserves_independent_execution_and_original_failure(self):
        summary = self.make_bundle()
        self.write_json("parsed/pacs_launch_execution.json", summary)
        self.write_json("run_summary.json", {"schema_version": 6, "status": "PACKAGING_FAILED",
                                             "issue": {"message": SECRET, "code": "OUTPUT_WRITE_FAILED"}})
        report = self.inspect()
        self.assertEqual(report["bundle_status"], "PACKAGING_FAILED")
        self.assertEqual(report["analysis_status"], "NEEDS_ATTENTION")
        self.assertEqual(report["pacs_launch_test"]["recorded"]["handoff"], "HANDOFF_SENT")

    def test_schema_one_different_profile_is_rejected(self):
        self.make_bundle()
        summary = self.read_json("run_summary.json")
        summary["profile"] = "atomic"
        self.write_json("run_summary.json", summary)
        with self.assertRaises(ConfigurationError) as caught:
            analyze_bundle(self.input, output_dir=self.output)
        self.assertEqual(caught.exception.info.code, "BUNDLE_SUMMARY_UNSUPPORTED")

    def test_zip_uses_same_readonly_evidence_rules(self):
        self.make_bundle()
        archive = self.root / "synthetic.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            for path in self.input.rglob("*"):
                if path.is_file():
                    handle.write(path, path.relative_to(self.input).as_posix())
        report = self.inspect(archive)
        self.assertEqual(report["analysis_status"], "OK")


if __name__ == "__main__":
    unittest.main()
