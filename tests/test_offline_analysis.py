from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from vghks_sdk.core.config import PortalCredentials, RequestPolicy
from vghks_sdk.core.errors import ConfigurationError
from vghks_sdk.live.bundle import LiveTestBundleManager
from vghks_sdk.live.capture import RawCaptureRecorder
from vghks_sdk.live.config import LiveTestConfig
from vghks_sdk.live.runner import execute_live_test
from vghks_sdk.live_test_app import analyze_bundle_command
from vghks_sdk.local_io import write_json_atomic
from vghks_sdk.offline.analyze import analyze_bundle, compare_reports, inspect_bundle
from vghks_sdk.offline.bundle import BundleReader
from vghks_sdk.offline.replay import identify_operation, replay_bundle, replay_response

SECRET = "SECRET_PAYLOAD_SHOULD_NOT_APPEAR"


class OfflineAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def bundle(
        self, *, body=None, status="OK", step_status="OK", network_error=False, checkpoint=False
    ):
        manager = LiveTestBundleManager(self.root / "run")
        config = {
            "schema_version": 4,
            "profile": "atomic",
            "only_operations": ["prq.soap"],
            "visit_filter": {"section_codes": ["60"]},
            "max_cases": 2,
            "username": SECRET,  # Legacy/untrusted extras must never be copied.
        }
        manager.write_config(config)
        write_json_atomic(
            manager.run_directory / "environment.json", {"sdk_version": "0.8.0", "probe": SECRET}
        )
        request = requests.Request(
            "GET",
            "https://intranet.example/PRQWeb/QueryBillingSOAP.do",
            params={
                "reqCode": "qrySOAP",
                "caseNo": "case-private",
                "casesec": "60",
                "hhisnum": "900123",
                "hid": SECRET,
            },
            headers={"Cookie": SECRET},
        ).prepare()
        with RawCaptureRecorder(manager.run_directory, prepare_root=False) as capture:
            capture.set_live_step("prq.soap.0001")
            if network_error:
                capture.record_network_error(
                    method="GET",
                    url=request.url,
                    kwargs={},
                    request=request,
                    error=requests.exceptions.SSLError(SECRET + " CERTIFICATE_VERIFY_FAILED"),
                    attempt=1,
                    max_attempts=1,
                    throttle_delay_seconds=0,
                    elapsed_seconds=0,
                    will_retry=False,
                )
            else:
                response = requests.Response()
                response.status_code = 200
                response.request = request
                response.url = request.url
                response.headers["Content-Type"] = "text/html; charset=big5"
                response._content = (
                    body
                    if body is not None
                    else f'<div id="data"><div class="soap"><pre>{SECRET}</pre></div></div>'.encode(
                        "cp950"
                    )
                )
                capture.record_response(
                    response=response,
                    attempt=1,
                    max_attempts=1,
                    throttle_delay_seconds=0,
                    elapsed_seconds=0.01,
                    will_retry=False,
                )
        steps = [
            {
                "name": "prq.soap.0001",
                "operation": "prq.soap",
                "status": step_status,
                "details": {"record_count": 0 if step_status == "EMPTY" else 1},
            }
        ]
        if checkpoint:
            write_json_atomic(manager.run_directory / "step_results.json", steps)
        summary = {
            "schema_version": 6,
            "status": status,
            "profile": "atomic",
            "steps": [] if checkpoint else steps,
        }
        with redirect_stdout(io.StringIO()):
            archive = manager.finalize(status=status, summary=summary)
        return archive

    def test_zip_opens_and_replays_without_network_or_secret_output(self):
        archive = self.bundle()
        with patch("requests.sessions.Session.request") as network:
            report = analyze_bundle(archive.archive_path, output_dir=self.root / "analysis")
        network.assert_not_called()
        self.assertEqual(report["bundle"]["status"], "READABLE")
        self.assertNotIn("integrity", report)
        row = next(row for row in report["operations"] if row["operation"] == "prq.soap")
        self.assertEqual(row["live_status"], "VERIFIED")
        self.assertEqual(row["replay_parsed"], 1)
        output = (self.root / "analysis" / "analysis.json").read_text(encoding="utf-8")
        for value in (SECRET, "900123", "case-private", "intranet.example"):
            self.assertNotIn(value, output)
        config = json.loads(
            (self.root / "analysis" / "retest-config.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("username", config)
        self.assertEqual(config["max_cases"], 2)

    def test_new_zip_requires_no_sidecar_or_file_manifest(self):
        archive = self.bundle()
        self.assertFalse(archive.archive_path.with_suffix(".zip.sha256").exists())
        with BundleReader(archive.archive_path) as reader:
            self.assertNotIn("files_manifest.json", reader.names)
            self.assertEqual(reader.json("run_summary.json")["status"], "OK")

    def test_retry_recovery_requires_same_group_and_preserves_later_http_failure(self):
        failure = {
            "kind": "NETWORK_ERROR", "will_retry": True,
            "error": {"message": "CERTIFICATE_VERIFY_FAILED", "type": "SSLError"},
        }
        response = {"kind": "HTTP_EXCHANGE", "response": {"status_code": 200}}
        rows = [
            {**failure, "request_group_id": "first"},
            {**failure, "request_group_id": "unresolved"},
            {**response, "request_group_id": "independent"},
            {**response, "request_group_id": "first", "response": {"status_code": 503}},
            failure, response,  # Missing group IDs cannot prove recovery.
        ]
        reader = Mock(spec=BundleReader)
        reader.json.return_value = {}
        reader.jsonl.return_value = rows
        results = replay_bundle(reader)
        self.assertTrue(results[0]["recovered"])
        self.assertNotIn("recovered", results[1])
        self.assertEqual(results[3]["status"], "HTTP_ERROR")
        self.assertEqual(results[3]["error_code"], "HTTP_503")
        self.assertNotIn("recovered", results[4])

    def test_missing_samples_are_reported_as_gaps_with_successful_cli_exit(self):
        archive = self.bundle(status="COMPLETED_WITH_GAPS")
        path = archive.run_directory / "run_summary.json"
        summary = json.loads(path.read_text(encoding="utf-8"))
        summary["steps"].extend(
            [
                {
                    "name": "visits.filters.doctor_card",
                    "status": "NO_SAMPLE",
                    "issue": {"code": "FILTER_FIELD_UNAVAILABLE", "message": SECRET},
                },
                {
                    "name": SECRET,
                    "operation": "prq.numeric",
                    "status": "NO_SAMPLE",
                    "issue": {"code": "QUERY_INPUT_UNAVAILABLE"},
                },
                {
                    "name": "login.personnel.unit",
                    "operation": "personnel.search",
                    "status": "NO_SAMPLE",
                    "details": {"reason": "NO_UNAMBIGUOUS_OPTION"},
                },
                {
                    "name": "login.personnel.subunits",
                    "operation": "personnel.search",
                    "status": "NO_SAMPLE",
                    "details": {"reason": SECRET},
                },
            ]
        )
        write_json_atomic(path, summary)
        output = self.root / "gaps-analysis"
        console = io.StringIO()
        with redirect_stdout(console), patch("requests.sessions.Session.request") as network:
            code = analyze_bundle_command(archive.run_directory, output)
        network.assert_not_called()
        self.assertEqual(code, 0)
        report = json.loads((output / "analysis.json").read_text(encoding="utf-8"))
        self.assertEqual(report["analysis_status"], "COMPLETED_WITH_GAPS")
        self.assertIsNone(report["root_cause"])
        self.assertEqual(report["problems"], [])
        self.assertEqual(report["query_summary"]["NO_SAMPLE"], 2)
        self.assertEqual(len(report["no_sample_steps"]), 4)
        self.assertEqual(report["no_sample_steps"][0]["step"], "visits.filters.doctor_card")
        self.assertEqual(report["no_sample_steps"][2]["step"], "login.personnel.unit")
        self.assertEqual(report["no_sample_steps"][2]["reason_code"], "NO_UNAMBIGUOUS_OPTION")
        self.assertEqual(report["no_sample_steps"][3]["reason_code"], "")
        text = (output / "analysis.md").read_text(encoding="utf-8")
        self.assertIn("visits.filters.doctor_card", text)
        self.assertIn("FILTER_FIELD_UNAVAILABLE", text)
        self.assertIn("login.personnel.unit", text)
        self.assertIn("NO_UNAMBIGUOUS_OPTION", text)
        self.assertNotIn("Root cause:", console.getvalue())
        for content in (json.dumps(report), text, console.getvalue()):
            self.assertNotIn(SECRET, content)

    def test_no_sample_marker_does_not_hide_a_real_response_error(self):
        archive = self.bundle(body=b"unrecognized SOAP", status="COMPLETED_WITH_GAPS")
        path = archive.run_directory / "run_summary.json"
        summary = json.loads(path.read_text(encoding="utf-8"))
        summary["steps"].append(
            {
                "name": "visits.filters.doctor_card",
                "status": "NO_SAMPLE",
                "issue": {"code": "FILTER_FIELD_UNAVAILABLE"},
            }
        )
        write_json_atomic(path, summary)
        with redirect_stdout(io.StringIO()):
            code = analyze_bundle_command(archive.run_directory, self.root / "mixed-analysis")
        self.assertEqual(code, 1)
        report = json.loads((self.root / "mixed-analysis/analysis.json").read_text(encoding="utf-8"))
        self.assertEqual(report["analysis_status"], "NEEDS_ATTENTION")
        self.assertEqual(report["root_cause"]["code"], "PRQ_SOAP_CONTAINER_MISSING")
        self.assertEqual(len(report["no_sample_steps"]), 1)

    def test_legacy_checksum_sidecar_is_ignored(self):
        archive = self.bundle()
        archive.archive_path.with_suffix(".zip.sha256").write_bytes(b"invalid\xff")
        with BundleReader(archive.archive_path) as reader:
            self.assertEqual(reader.json("run_summary.json")["status"], "OK")

    def test_legacy_file_hashes_do_not_block_replay_of_current_contents(self):
        archive = self.bundle()
        response = next((archive.run_directory / "responses").iterdir())
        write_json_atomic(
            archive.run_directory / "files_manifest.json",
            {
                "schema_version": 1,
                "files": [
                    {
                        "path": response.relative_to(archive.run_directory).as_posix(),
                        "size": 999,
                        "sha256": "0" * 64,
                    }
                ],
            },
        )
        response.write_bytes(b"changed")
        with BundleReader(archive.run_directory) as reader:
            report, _ = inspect_bundle(reader)
        soap = next(row for row in report["operations"] if row["operation"] == "prq.soap")
        self.assertEqual(soap["replay_errors"], 1)

    def test_extra_notes_do_not_require_a_manifest_update(self):
        archive = self.bundle()
        (archive.run_directory / "extra.txt").write_text("unlisted")
        with BundleReader(archive.run_directory) as reader:
            self.assertEqual(reader.read("extra.txt"), b"unlisted")

    def test_required_summary_is_still_needed_to_analyze_a_run(self):
        archive = self.bundle()
        (archive.run_directory / "run_summary.json").unlink()
        with self.assertRaises(ConfigurationError) as caught:
            BundleReader(archive.run_directory)
        self.assertEqual(caught.exception.info.code, "BUNDLE_FILE_MISSING")

    def test_zip_traversal_never_extracts_or_creates_a_file(self):
        source = self.root / "bad.zip"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("../outside.txt", "bad")
        with self.assertRaises(ConfigurationError) as caught:
            BundleReader(source)
        self.assertEqual(caught.exception.info.code, "BUNDLE_PATH_INVALID")
        self.assertFalse((self.root / "outside.txt").exists())

    def test_zip_case_collisions_are_rejected(self):
        source = self.root / "bad.zip"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("file.txt", "a")
            archive.writestr("FILE.txt", "b")
        with self.assertRaises(ConfigurationError) as caught:
            BundleReader(source)
        self.assertEqual(caught.exception.info.code, "BUNDLE_DUPLICATE_PATH")

    def test_zip_symlink_is_rejected(self):
        source = self.root / "bad.zip"
        info = zipfile.ZipInfo("link")
        info.external_attr = 0o120777 << 16
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr(info, "../secret")
        with self.assertRaises(ConfigurationError) as caught:
            BundleReader(source)
        self.assertEqual(caught.exception.info.code, "BUNDLE_SYMLINK")

    def test_analysis_never_writes_into_original_run(self):
        archive = self.bundle()
        with self.assertRaises(ConfigurationError) as caught:
            analyze_bundle(archive.run_directory, output_dir=archive.run_directory / "analysis")
        self.assertEqual(caught.exception.info.code, "ANALYSIS_OUTPUT_IN_BUNDLE")

    def test_network_failure_yields_auth_retest_not_a_password_claim(self):
        archive = self.bundle(
            status="AUTHENTICATION_FAILED", step_status="BLOCKED", network_error=True
        )
        with BundleReader(archive.archive_path) as reader:
            report, config = inspect_bundle(reader)
        self.assertEqual(report["root_cause"]["code"], "TLS_VERIFY_FAILED")
        self.assertEqual(report["root_cause"]["phase"], "CONNECTIVITY")
        self.assertEqual(config["profile"], "auth")
        self.assertEqual(config["only_operations"], [])

    def test_legacy_blank_login_and_misclassified_eof_report_both_causes(self):
        manager = LiveTestBundleManager(self.root / "legacy")
        manager.write_config({"schema_version": 4, "profile": "comprehensive"})
        with RawCaptureRecorder(manager.run_directory, prepare_root=False) as capture:
            capture.set_live_step("network.prq.https")
            request = requests.Request("GET", "https://example.invalid/PRQWeb/").prepare()
            capture.record_network_error(
                method="GET",
                url=request.url,
                kwargs={},
                request=request,
                error=requests.exceptions.SSLError(
                    "EOF occurred in violation of protocol " + SECRET
                ),
                attempt=1,
                max_attempts=1,
                throttle_delay_seconds=0,
                elapsed_seconds=0,
                will_retry=False,
            )
            capture.set_live_step("auth_check.portal")
            response = requests.Response()
            response.status_code = 200
            response._content = b"\n"
            response.url = "https://example.invalid/login.do?thetime=1"
            response.request = requests.Request(
                "POST",
                response.url,
                data={
                    "muid": SECRET,
                    "mpassword": SECRET,
                    "ssoId2": "",
                },
            ).prepare()
            capture.record_response(
                response=response,
                attempt=1,
                max_attempts=1,
                throttle_delay_seconds=0,
                elapsed_seconds=0,
                will_retry=False,
            )
        summary = {
            "schema_version": 6,
            "profile": "comprehensive",
            "status": "AUTHENTICATION_FAILED",
            "steps": [
                {
                    "name": "network.prq.https",
                    "status": "ERROR",
                    "first_capture_id": "000001",
                    "last_capture_id": "000001",
                    "issue": {"code": "TLS_VERIFY_FAILED"},
                },
                {
                    "name": "auth_check.portal",
                    "status": "ERROR",
                    "first_capture_id": "000002",
                    "last_capture_id": "000002",
                    "issue": {"code": "PORTAL_LOGIN_TARGET_MISSING", "operation": "portal.login"},
                },
                {"name": "prq.soap", "operation": "prq.soap", "status": "BLOCKED"},
            ],
        }
        with redirect_stdout(io.StringIO()):
            archive = manager.finalize(status="AUTHENTICATION_FAILED", summary=summary)
        with patch("requests.sessions.Session.request") as network:
            report = analyze_bundle(archive.archive_path, output_dir=self.root / "analysis")
        network.assert_not_called()
        self.assertEqual(report["root_cause"]["code"], "PORTAL_LOGIN_RESPONSE_EMPTY")
        self.assertEqual([item["code"] for item in report["preflight_findings"]], ["TLS_EOF"])
        self.assertEqual(
            report["portal_login_evidence"],
            [
                {
                    "http_status": 200,
                    "response_bytes": 1,
                    "response_blank": True,
                    "entry_page_requested_before_login": False,
                    "origin_header_present": False,
                    "referer_is_entry_page": False,
                }
            ],
        )
        self.assertFalse(any(item["live_status"] == "VERIFIED" for item in report["operations"]))
        markdown = (self.root / "analysis/analysis.md").read_text(encoding="utf-8")
        self.assertIn("TLS_EOF", markdown)
        self.assertNotIn("TLS_VERIFY_FAILED", markdown)
        self.assertNotIn(SECRET, markdown)

    def test_offline_login_empty_bytes_and_whitespace_have_same_diagnosis(self):
        for content in (b"", b"\n", b" \r\n\t"):
            with self.subTest(content=content):
                result = replay_response("portal.login", content, {})
                self.assertEqual(result["error_code"], "PORTAL_LOGIN_RESPONSE_EMPTY")

    def test_successful_portal_and_blocked_queries_are_reported_separately(self):
        from vghks_sdk.queries import QUERY_SPECS

        manager = LiveTestBundleManager(self.root / "separate-status")
        with RawCaptureRecorder(manager.run_directory, prepare_root=False):
            pass
        manager.write_config({"schema_version": 4, "profile": "comprehensive"})
        write_json_atomic(
            manager.run_directory / "parsed/readiness.json",
            {
                "targets": [
                    {"target": "portal", "status": "OK"},
                    {"target": "prq", "status": "ERROR", "issue": {"code": "TLS_EOF"}},
                    {
                        "target": "webmaas",
                        "status": "BLOCKED",
                        "issue": {"code": "DEPENDENCY_FAILED"},
                    },
                ],
            },
        )
        steps = [
            {"name": "network.prq.https_tls12", "status": "ERROR", "issue": {"code": "TLS_EOF"}},
            *[
                {"name": spec.key, "operation": spec.key, "status": "BLOCKED"}
                for spec in QUERY_SPECS
            ],
        ]
        with redirect_stdout(io.StringIO()):
            archive = manager.finalize(
                status="COMPLETED_WITH_ERRORS",
                summary={
                    "schema_version": 6,
                    "status": "COMPLETED_WITH_ERRORS",
                    "steps": steps,
                },
            )
        with patch("requests.sessions.Session.request") as network:
            report = analyze_bundle(
                archive.archive_path, output_dir=self.root / "separate-analysis"
            )
        network.assert_not_called()
        self.assertEqual(report["authentication"][0]["status"], "OK")
        self.assertEqual(report["authentication"][1]["error_code"], "TLS_EOF")
        self.assertEqual(report["query_summary"]["BLOCKED"], 57)
        self.assertEqual(report["query_summary"]["FAILED"], 0)
        markdown = (self.root / "separate-analysis/analysis.md").read_text(encoding="utf-8")
        self.assertIn("入口登入與 Session 已通過", markdown)
        self.assertIn("本輪已實測 TLS 1.2 仍失敗", markdown)

    def test_recovered_probe_does_not_override_successful_live_readiness(self):
        manager = LiveTestBundleManager(self.root / "recovered")
        with RawCaptureRecorder(manager.run_directory, prepare_root=False):
            pass
        manager.write_config({"schema_version": 4, "profile": "comprehensive"})
        write_json_atomic(
            manager.run_directory / "parsed/readiness.json",
            {
                "targets": [
                    {"target": "portal", "status": "OK"},
                    {"target": "prq", "status": "OK"},
                ],
            },
        )
        with redirect_stdout(io.StringIO()):
            archive = manager.finalize(
                status="OK",
                summary={
                    "schema_version": 6,
                    "status": "OK",
                    "steps": [
                        {
                            "name": "network.prq.https",
                            "status": "ERROR",
                            "issue": {"code": "TLS_EOF"},
                        },
                        {"name": "network.prq.https_tls12_compat", "status": "OK"},
                        {"name": "auth_check.prq", "status": "OK"},
                    ],
                },
            )
        report = analyze_bundle(archive.archive_path, output_dir=self.root / "recovered-analysis")
        self.assertEqual(report["analysis_status"], "OK")
        self.assertIsNone(report["root_cause"])
        self.assertTrue(report["preflight_findings"][0]["recovered"])

    def test_empty_and_not_executed_operations_are_never_verified(self):
        archive = self.bundle(body=b'<div id="data"></div>', step_status="EMPTY")
        with BundleReader(archive.archive_path) as reader:
            report, config = inspect_bundle(reader)
        evidence = {row["operation"]: row for row in report["operations"]}
        self.assertEqual(evidence["prq.soap"]["live_status"], "EMPTY")
        self.assertEqual(evidence["prq.soap"]["replay_empty"], 1)
        self.assertEqual(evidence["prq.numeric"]["live_status"], "NOT_TESTED")
        self.assertEqual(config["only_operations"], ["prq.soap"])

    def test_unverified_probe_is_not_query_evidence_and_opt_out_survives_retest(self):
        manager = LiveTestBundleManager(self.root / "unverified")
        with RawCaptureRecorder(manager.run_directory, prepare_root=False) as capture:
            capture.set_live_step("network.prq.https_unverified")
            response = requests.Response()
            response.status_code = 200
            response.url = "https://intranet.example/PRQWeb/"
            response.request = requests.Request("GET", response.url).prepare()
            response._content = SECRET.encode()
            capture.record_response(
                response=response,
                attempt=1,
                max_attempts=1,
                throttle_delay_seconds=0,
                elapsed_seconds=0,
                will_retry=False,
            )
        manager.write_config({"profile": "comprehensive", "allow_unverified_tls": False})
        write_json_atomic(
            manager.run_directory / "parsed/network/prq-https_unverified-context.json",
            {"certificate_verification": False, "url": SECRET},
        )
        write_json_atomic(
            manager.run_directory / "parsed/network/selected_profiles.json",
            {
                "prq": {"applied": False, "certificate_verification": False},
                SECRET: {"applied": True, "certificate_verification": False},
            },
        )
        with redirect_stdout(io.StringIO()):
            archive = manager.finalize(
                status="CONNECTIVITY_FAILED",
                summary={
                    "schema_version": 6,
                    "status": "CONNECTIVITY_FAILED",
                    "steps": [
                        {
                            "name": "network.prq.https",
                            "status": "ERROR",
                            "issue": {"code": "TLS_VERIFY_FAILED"},
                        },
                        {"name": "network.prq.https_unverified", "status": "OK"},
                        {"name": "prq.soap", "operation": "prq.soap", "status": "BLOCKED"},
                    ],
                },
            )
        report = analyze_bundle(archive.archive_path, output_dir=self.root / "unverified-analysis")
        recipe = json.loads(
            (self.root / "unverified-analysis/retest-config.json").read_text(encoding="utf-8")
        )
        self.assertFalse(recipe["allow_unverified_tls"])
        self.assertFalse(report["unverified_tls_services"])
        self.assertEqual(report["query_summary"]["VERIFIED"], 0)
        self.assertEqual(report["certificate_comparisons"][0]["unverified_status"], "OK")
        self.assertTrue(report["certificate_comparisons"][0]["anonymous_probe"])
        self.assertIs(report["connection_trials"][1]["certificate_verification"], False)
        self.assertNotIn(SECRET, json.dumps(report))

    def test_local_parser_success_does_not_relabel_failed_live_operation(self):
        archive = self.bundle(status="COMPLETED_WITH_ERRORS", step_status="ERROR")
        with BundleReader(archive.archive_path) as reader:
            report, config = inspect_bundle(reader)
        evidence = next(row for row in report["operations"] if row["operation"] == "prq.soap")
        self.assertEqual(evidence["live_status"], "FAILED")
        self.assertEqual(evidence["replay_parsed"], 1)
        self.assertEqual(config["only_operations"], ["prq.soap"])

    def test_interrupted_run_uses_checkpoint(self):
        archive = self.bundle(status="INTERRUPTED", checkpoint=True)
        with BundleReader(archive.archive_path) as reader:
            report, _ = inspect_bundle(reader)
        evidence = next(row for row in report["operations"] if row["operation"] == "prq.soap")
        self.assertEqual(evidence["live_status"], "VERIFIED")

    def test_parser_replay_supports_big5_and_rejects_login_html(self):
        body = '<div id="data"><div class="soap"><pre>測試</pre></div></div>'.encode("cp950")
        result = replay_response(
            "prq.soap", body, {"hhisnum": "901", "caseNo": "case"}, mime="text/html; charset=big5"
        )
        self.assertEqual(result["status"], "PARSED")
        result = replay_response(
            "prq.soap", b'<input name="muid"><input name="mpassword">', {"hhisnum": "901"}
        )
        self.assertEqual(result["error_code"], "AUTH_SESSION_LOGIN_FORM")

    def test_live_pdf_error_page_is_not_treated_as_success(self):
        result = replay_response(
            "prq.pdf_attachment", b'<script>location="about:blank"</script>', {}
        )
        self.assertEqual(result["status"], "PARSE_ERROR")
        self.assertEqual(result["error_code"], "PDF_BINARY_INVALID")
        valid = replay_response("prq.pdf_attachment", b"%PDF-1.4\n%%EOF\n", {})
        self.assertEqual(valid["status"], "PARSED")

    def test_semantic_request_matching_uses_form_constants(self):
        request = {
            "method": "POST",
            "url": "https://example/PRQWeb/QueryOrderResult.do",
            "body": {
                "text": "Use=Dur&date=4000&hhisnum=901&hid=x&ordersubtype=*&ordertype=*&orstepc=*"
            },
        }
        self.assertEqual(identify_operation(request), "prq.order_history")
        request["body"]["text"] = request["body"]["text"].replace("Use=Dur", "Use=Case")
        self.assertEqual(identify_operation(request), "")

    def test_comparison_distinguishes_regression_from_not_retested(self):
        before = {
            "recorded_build": {},
            "operations": [
                {"operation": "a", "live_status": "VERIFIED"},
                {"operation": "b", "live_status": "VERIFIED"},
            ],
        }
        after = {
            "operations": [
                {"operation": "a", "live_status": "FAILED"},
                {"operation": "b", "live_status": "NOT_TESTED"},
                {"operation": "c", "live_status": "VERIFIED"},
            ]
        }
        delta = compare_reports(before, after)
        self.assertEqual(delta["regressions"], ["a"])
        self.assertEqual(delta["not_retested"], ["b"])
        self.assertEqual(delta["newly_verified"], ["c"])

    def test_live_tls_failure_roundtrips_through_new_bundle_and_analyzer(self):
        config = LiveTestConfig(
            profile="atomic",
            only_operations=("prq.soap",),
            output_root=self.root,
            request_policy=RequestPolicy(min_delay_seconds=0, max_delay_seconds=0),
        )
        with (
            patch(
                "requests.sessions.Session.request",
                side_effect=requests.exceptions.SSLError("synthetic certificate failure"),
            ) as network,
            redirect_stdout(io.StringIO()),
        ):
            execution = execute_live_test(config, PortalCredentials("synthetic-user", SECRET))
        self.assertEqual(network.call_count, config.request_policy.max_attempts)
        self.assertEqual(execution.status, "CONNECTIVITY_FAILED")
        self.assertIn("CONNECTIVITY_FAILED.zip", execution.archive.archive_path.name)
        with BundleReader(execution.archive.archive_path) as reader:
            report, recipe = inspect_bundle(reader)
        self.assertEqual(report["root_cause"]["code"], "NETWORK_TLS_FAILED")
        self.assertEqual(recipe["profile"], "auth")
        self.assertFalse(any(row["live_status"] == "VERIFIED" for row in report["operations"]))


if __name__ == "__main__":
    unittest.main()
