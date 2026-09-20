from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from collections import deque
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from vghks_sdk import RequestPolicy
from vghks_sdk.cli import _resolve_diagnostic_directory, build_parser, main
from vghks_sdk.core.diagnostics import DiagnosticRecorder
from vghks_sdk.core.transport import SafeSessionTransport
from vghks_sdk.diagnostics.probe import run_diagnostic_probe
from vghks_sdk.models import (
    AuthCheckTarget,
    CaseDetail,
    NumericReport,
    NumericTable,
    OutpatientPatient,
    PatientDemographics,
    RegistrationRecord,
    SoapRecord,
    VisitCase,
)


class FakeCookies:
    def clear(self) -> None:
        pass


class FakeResponse:
    def __init__(self, body: bytes, content_type: str, url: str) -> None:
        self.status_code = 200
        self.content = body
        self.headers = {"Content-Type": content_type}
        self.encoding = None
        self.url = url
        self.history: list[object] = []

    def close(self) -> None:
        pass


class FakeSession:
    def __init__(self, outcomes: list[FakeResponse]) -> None:
        self.outcomes = deque(outcomes)
        self.headers: dict[str, str] = {}
        self.cookies = FakeCookies()

    def request(self, *args: object, **kwargs: object) -> FakeResponse:
        return self.outcomes.popleft()

    def close(self) -> None:
        pass


def eye_case() -> VisitCase:
    return VisitCase(
        mrn="00000000",
        visit_date=date(2026, 1, 2),
        case_type="O",
        case_no="PRIVATE-CASE-99",
        section_code="70",
        section_name="眼科",
        index=0,
    )


class SensitiveFakeSDK:
    def __init__(self) -> None:
        self.auth = self
        self.opd = self
        self.patients = self
        self.records = self
        self.surgery = self
        self.audit = self

    def check(self, only=None):
        from vghks_sdk.core.readiness import make_auth_report

        return make_auth_report(
            (
                AuthCheckTarget(
                    "portal", ("Login", "Session"), "/sessionCheck.do", False, 1, 1.0, (), "OK"
                ),
            )
        )

    def get_doctor_patients(self, doctor: str, day: date):
        return [OutpatientPatient("00000000", "不可外流姓名", day, doctor_card=doctor)]

    def get_demographics(self, mrn: str):
        return PatientDemographics(
            mrn,
            "不可外流姓名",
            "0912345678",
            "071234567",
            birthday="1980-01-02",
        )

    def get_registration_history(self, mrn: str):
        return [RegistrationRecord({"病歷號": mrn, "姓名": "不可外流姓名"})]

    def get_visit_cases(self, mrn: str):
        return [eye_case()]

    def get_case_detail(self, case: VisitCase):
        return CaseDetail(case, ("soap-tab",), ("病歷內容",))

    def get_soap(self, case: VisitCase):
        return SoapRecord(case, ("不可外流的完整SOAP文字",))

    def get_numeric_report(self, case: VisitCase):
        return NumericReport(
            case,
            (NumericTable("敏感報告標題", ("欄位",), (("敏感數值",),)),),
        )


class MissingRecordFakeSDK(SensitiveFakeSDK):
    def get_soap(self, case: VisitCase):
        return SoapRecord(case, ())

    def get_numeric_report(self, case: VisitCase):
        return NumericReport(case, ())


class FailedReadinessFakeSDK(SensitiveFakeSDK):
    def check(self, only=None):
        from vghks_sdk.core.readiness import make_auth_report

        return make_auth_report(
            (
                AuthCheckTarget(
                    "portal", ("Login", "Session"), "/sessionCheck.do", False, 1, 1.0, (), "OK"
                ),
                AuthCheckTarget(
                    "prq",
                    ("Records",),
                    "",
                    False,
                    1,
                    1.0,
                    ("portal",),
                    "ERROR",
                    "AUTHENTICATION_ERROR",
                ),
            )
        )


class DiagnosticsTests(unittest.TestCase):
    def test_diagnostic_paths_can_be_automatic_or_explicit(self) -> None:
        parser = build_parser()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch("vghks_sdk.cli.Path.cwd", return_value=root):
                self.assertIsNone(_resolve_diagnostic_directory(parser.parse_args(["auth-check"])))
                automatic = _resolve_diagnostic_directory(
                    parser.parse_args(["auth-check", "--diagnostics"])
                )
                diagnose_default = _resolve_diagnostic_directory(parser.parse_args(["diagnose"]))
                explicit = _resolve_diagnostic_directory(
                    parser.parse_args(["auth-check", "--diagnostics", str(root / "chosen")])
                )
            self.assertEqual(automatic.parent, root / "diagnostics")
            self.assertTrue(automatic.name.startswith("auth-check-"))
            self.assertEqual(diagnose_default.parent, root / "diagnostics")
            self.assertTrue(diagnose_default.name.startswith("diagnose-"))
            self.assertEqual(explicit, root / "chosen")

    def test_diagnose_without_output_argument_creates_bundle_under_cwd(self) -> None:
        environment = {
            "VGHKS_USERNAME": "TEST-USER",
            "VGHKS_PASSWORD": "TEST-PASSWORD",
            "VGHKS_CA_BUNDLE": "",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sdk_context = MagicMock()
            sdk_context.__enter__.return_value = SensitiveFakeSDK()
            sdk_context.__exit__.return_value = None
            with (
                patch.dict(os.environ, environment, clear=False),
                patch("vghks_sdk.cli.Path.cwd", return_value=root),
                patch("vghks_sdk.cli.VghksSDK", return_value=sdk_context),
                redirect_stdout(io.StringIO()),
            ):
                result = main(["diagnose"])
            self.assertEqual(result, 0)
            bundles = list((root / "diagnostics").glob("diagnose-*"))
            self.assertEqual(len(bundles), 1)
            self.assertTrue((bundles[0] / "diagnostics.jsonl").is_file())
            self.assertTrue((bundles[0] / "summary.json").is_file())

    def test_probe_stops_when_typed_auth_report_is_not_ok(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = DiagnosticRecorder(Path(temp_dir) / "diag")
            result = run_diagnostic_probe(
                FailedReadinessFakeSDK(),  # type: ignore[arg-type]
                recorder,
                doctor_card="D001",
                probe_date=date(2026, 1, 2),
            )
            recorder.finalize(
                command="diagnose",
                status=result.status,
                exit_code=1,
                checks=tuple(dict(check.as_mapping()) for check in result.checks),
            )
            self.assertEqual(len(result.checks), 1)
            self.assertEqual(result.checks[0].check, "auth_primary")
            self.assertEqual(result.checks[0].status, "ERROR")

    def test_http_diagnostics_keep_structure_but_not_values_or_body(self) -> None:
        secrets = (
            "00000000",
            "0912345678",
            "不可外流姓名",
            "synthetic-secret-password",
            "SUPER-SECRET-SSO-TOKEN",
            "不可外流的完整SOAP文字",
        )
        html = """
        <html><form id="RSV11WForm" action="/RSV/RSV11W001.do">
          <input name="patno" value="00000000">
          <input name="mpassword" value="synthetic-secret-password">
          <input name="ssID" value="SUPER-SECRET-SSO-TOKEN">
        </form><div id="data"><div class="soap"><pre>
          不可外流姓名 0912345678 不可外流的完整SOAP文字
        </pre></div></div></html>
        """.encode()
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = DiagnosticRecorder(Path(temp_dir) / "diag")
            session = FakeSession(
                [
                    FakeResponse(
                        html,
                        "text/html; charset=UTF-8",
                        "https://example.test/query.do?mrn=00000000",
                    )
                ]
            )
            transport = SafeSessionTransport(
                policy=RequestPolicy(min_delay_seconds=0, max_delay_seconds=0, max_attempts=1),
                session=session,  # type: ignore[arg-type]
                sleeper=lambda _: None,
                diagnostics=recorder,
            )
            transport.request(
                "POST",
                "https://example.test/query.do?mrn=00000000",
                params={"doctor": "D001"},
                data={"patno": "00000000", "mpassword": "synthetic-secret-password"},
                retry_safe=True,
            )
            recorder.finalize(command="test", status="OK", exit_code=0)

            bundle_text = recorder.trace_path.read_text(encoding="utf-8")
            bundle_text += recorder.summary_path.read_text(encoding="utf-8")
            for secret in secrets:
                self.assertNotIn(secret, bundle_text)

            events = [json.loads(line) for line in recorder.trace_path.read_text().splitlines()]
            response = next(event for event in events if event["event"] == "http_response")
            shape = response["response"]["html_shape"]
            self.assertEqual(shape["selector_counts"]["#data .soap pre"], 1)
            request = next(event for event in events if event["event"] == "http_request")
            self.assertIn("patno", request["form_keys"])
            self.assertIn("mrn", request["query_keys"])

    def test_probe_summary_does_not_serialize_returned_clinical_values(self) -> None:
        secrets = (
            "00000000",
            "不可外流姓名",
            "0912345678",
            "不可外流的完整SOAP文字",
            "敏感報告標題",
            "敏感數值",
            "PRIVATE-CASE-99",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = DiagnosticRecorder(Path(temp_dir) / "diag")
            result = run_diagnostic_probe(
                SensitiveFakeSDK(),  # type: ignore[arg-type]
                recorder,
                doctor_card="D001",
                probe_date=date(2026, 1, 2),
            )
            checks = tuple(dict(check.as_mapping()) for check in result.checks)
            recorder.finalize(
                command="diagnose",
                status=result.status,
                exit_code=0,
                checks=checks,
            )
            bundle_text = recorder.trace_path.read_text(encoding="utf-8")
            bundle_text += recorder.summary_path.read_text(encoding="utf-8")
            for secret in secrets:
                self.assertNotIn(secret, bundle_text)
            self.assertEqual(result.failed_count, 0)
            summary = json.loads(recorder.summary_path.read_text(encoding="utf-8"))
            names = [item["check"] for item in summary["checks"]]
            self.assertIn("soap", names)
            self.assertIn("numeric_report", names)
            visit_check = next(check for check in result.checks if check.check == "visit_cases")
            self.assertIn("matching_outpatient_count", visit_check.details)
            self.assertNotIn("eye_outpatient_count", visit_check.details)

    def test_cli_configuration_error_is_written_to_bundle_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            diagnostic_dir = Path(temp_dir) / "diag"
            environment = {
                "VGHKS_USERNAME": "",
                "VGHKS_USER": "",
                "VGHKS_PASSWORD": "",
                "VGHKS_CA_BUNDLE": "",
            }
            with patch.dict(os.environ, environment, clear=False), redirect_stderr(io.StringIO()):
                result = main(
                    [
                        "auth-check",
                        "--diagnostics",
                        str(diagnostic_dir),
                        "--output",
                        str(Path(temp_dir) / "auth.json"),
                    ]
                )
            self.assertEqual(result, 2)
            summary = json.loads((diagnostic_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["error_code"], "CONFIGURATION_ERROR")
            self.assertFalse(summary["contains_raw_request_or_response"])

    def test_probe_marks_empty_soap_and_numeric_report_as_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = DiagnosticRecorder(Path(temp_dir) / "diag")
            result = run_diagnostic_probe(
                MissingRecordFakeSDK(),  # type: ignore[arg-type]
                recorder,
                doctor_card="D001",
                probe_date=date(2026, 1, 2),
            )
            by_name = {check.check: check for check in result.checks}
            self.assertEqual(by_name["soap"].error_code, "SOAP_MISSING")
            self.assertEqual(by_name["numeric_report"].error_code, "NUMERIC_MISSING")
            self.assertEqual(result.failed_count, 2)
            recorder.finalize(
                command="diagnose",
                status=result.status,
                exit_code=1,
                checks=tuple(dict(check.as_mapping()) for check in result.checks),
            )


if __name__ == "__main__":
    unittest.main()
