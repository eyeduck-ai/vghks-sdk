from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from vghks_sdk.core.errors import ErrorInfo
from vghks_sdk.live.profile import LIVE_TEST_MRN, run_live_test
from vghks_sdk.models import (
    AuthCheckTarget,
    NumericReport,
    NumericTable,
    PatientDemographics,
    RegistrationRecord,
    SoapRecord,
    VisitCase,
    VisitFilter,
)


class FakeLiveSDK:
    def __init__(self) -> None:
        self.auth = self
        self.patients = self
        self.records = self
        self.opd = self
        self.surgery = self
        self.audit = self
        self.requested_mrns: list[str] = []

    def check(self, only=None):
        from vghks_sdk.core.readiness import make_auth_report

        self.apps = only
        return make_auth_report(
            (
                AuthCheckTarget(
                    "portal", ("Login", "Session"), "/sessionCheck.do", False, 1, 1.0, (), "OK"
                ),
            )
        )

    def get_demographics(self, mrn: str):
        self.requested_mrns.append(mrn)
        return PatientDemographics(mrn, "完整測試姓名", "0912345678")

    def get_registration_history(self, mrn: str):
        self.requested_mrns.append(mrn)
        return [RegistrationRecord({"病歷號": mrn, "狀態": "完整資料"})]

    def get_visit_cases(self, mrn: str):
        self.requested_mrns.append(mrn)
        return [VisitCase(mrn, date(2026, 1, 2), "O", "CASE-1", "70", "眼科", 0)]

    def get_soap(self, case: VisitCase):
        return SoapRecord(case, ("S: 完整 SOAP", "P: APPLY"))

    def get_numeric_report(self, case: VisitCase):
        return NumericReport(case, (NumericTable("IOP", ("OD", "OS"), (("15", "16"),)),))


class FailedReadinessLiveSDK(FakeLiveSDK):
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
                    ErrorInfo("AUTHENTICATION_ERROR", "AUTHENTICATION"),
                ),
            )
        )


class LiveTestTests(unittest.TestCase):
    def test_non_exception_failed_auth_report_stops_live_test(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "live-run"
            root.mkdir()
            sdk = FailedReadinessLiveSDK()
            result = run_live_test(  # type: ignore[arg-type]
                sdk,
                output_dir=root,
                profile="core",
            )
            self.assertEqual(result.status, "AUTHENTICATION_FAILED")
            self.assertEqual(len(result.steps), 1)
            self.assertEqual(sdk.requested_mrns, [])

    def test_live_test_is_fixed_to_authorized_mrn_and_writes_full_parsed_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "live-run"
            root.mkdir()
            sdk = FakeLiveSDK()
            result = run_live_test(  # type: ignore[arg-type]
                sdk,
                output_dir=root,
                profile="core",
            )
            self.assertEqual(result.status, "OK")
            self.assertTrue(sdk.requested_mrns)
            self.assertEqual(set(sdk.requested_mrns), {LIVE_TEST_MRN})
            demographics = json.loads(
                (root / "parsed" / "patient_demographics.json").read_text(encoding="utf-8")
            )
            self.assertEqual(demographics["mrn"], "0000000")
            self.assertEqual(demographics["name"], "完整測試姓名")
            records = root / "parsed" / "visit_history_records.jsonl"
            row = json.loads(records.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(row["schema_version"], 3)
            self.assertEqual(row["soap"]["blocks"], ["S: 完整 SOAP", "P: APPLY"])
            self.assertEqual(row["numeric_report"]["tables"][0]["title"], "IOP")
            summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["test_mrn"], "0000000")
            self.assertIn("UNREDACTED", summary["warning"])

    def test_live_test_visit_filter_can_override_default_ophthalmology(self) -> None:
        class FamilySDK(FakeLiveSDK):
            def get_visit_cases(self, mrn: str):
                self.requested_mrns.append(mrn)
                return [VisitCase(mrn, date(2026, 1, 2), "O", "FAMILY", "60", "家醫科", 0)]

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "live-run"
            root.mkdir()
            result = run_live_test(
                FamilySDK(),  # type: ignore[arg-type]
                output_dir=root,
                profile="core",
                visit_filter=VisitFilter(section_codes=("60",)),
            )
            self.assertEqual(result.status, "OK")
            records = root / "parsed" / "visit_history_records.jsonl"
            row = json.loads(records.read_text(encoding="utf-8"))
            self.assertEqual(row["case"]["case_no"], "FAMILY")


if __name__ == "__main__":
    unittest.main()
