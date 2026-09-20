from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from vghks_sdk.core.readiness import make_auth_report
from vghks_sdk.live.profile import run_live_test
from vghks_sdk.models import (
    AuthCheckTarget,
    CaseDetail,
    NumericReport,
    NumericTable,
    PatientDemographics,
    RegistrationRecord,
    SoapRecord,
    VisitCase,
    VisitFilter,
)
from vghks_sdk.search import SoapSearch


def _target(key: str) -> AuthCheckTarget:
    dependencies = () if key == "portal" else ("portal",)
    return AuthCheckTarget(key, (key,), f"/{key}", key != "portal", 2, 1.0, dependencies, "OK")


class ProfileSDK:
    def __init__(self) -> None:
        self.auth = self
        self.patients = self
        self.records = self
        self.opd = self
        self.surgery = self
        self.audit = self
        self.calls: list[str] = []
        self.cases = [
            VisitCase("00000000", date(2026, 2, 2), "O", "NEW", "70", "眼科", 0),
            VisitCase("00000000", date(2026, 1, 1), "O", "OLD", "70", "眼科", 1),
        ]

    def check(self, only=None):
        self.calls.append("auth")
        keys = ("portal", "prq", "sectord", "webmaas", "oppl", "audit")
        return make_auth_report(tuple(_target(key) for key in keys))

    def get_demographics(self, mrn):
        self.calls.append("demographics")
        return PatientDemographics(mrn, "Test", "0900")

    def get_registration_history(self, mrn):
        self.calls.append("registration")
        return [RegistrationRecord({"mrn": mrn})]

    def get_visit_cases(self, mrn):
        self.calls.append("case_list")
        return list(self.cases)

    def get_case_detail(self, case):
        self.calls.append(f"detail:{case.case_no}")
        return CaseDetail(case, ("soap", "numeric"), ("SOAP", "Numeric"))

    def get_soap(self, case):
        self.calls.append(f"soap:{case.case_no}")
        return SoapRecord(case, (f"P: APPLY {case.case_no}",))

    def get_numeric_report(self, case):
        self.calls.append(f"numeric:{case.case_no}")
        return NumericReport(case, (NumericTable("IOP", ("OD",), (("15",),)),))

    def get_doctor_patients(self, card, visit_date):
        self.calls.append("opd")
        return []

    def get_schedule(self, card, start, end):
        self.calls.append("surgery")
        return []

    def get_unsigned_records(self, card, start, end):
        self.calls.append("audit_records")
        return []


class LiveProfileTests(unittest.TestCase):
    def test_core_fetches_only_latest_and_searches_without_another_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sdk = ProfileSDK()
            result = run_live_test(
                sdk,  # type: ignore[arg-type]
                output_dir=Path(temp_dir),
                profile="core",
                visit_filter=VisitFilter(section_codes=("70",)),
                soap_search=SoapSearch("APPLY"),
            )
            self.assertEqual(result.status, "OK")
            self.assertEqual(sdk.calls.count("case_list"), 1)
            self.assertIn("soap:NEW", sdk.calls)
            self.assertNotIn("soap:OLD", sdk.calls)
            self.assertEqual(sdk.calls.count("soap:NEW"), 1)
            search = json.loads(
                (Path(temp_dir) / "parsed" / "soap_search.json").read_text(encoding="utf-8")
            )
            self.assertEqual(search["match_count"], 1)

    def test_full_fetches_every_case_and_all_independent_subsystems(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sdk = ProfileSDK()
            result = run_live_test(
                sdk,  # type: ignore[arg-type]
                output_dir=Path(temp_dir),
                profile="full",
                doctor_card="D001",
                probe_date=date(2026, 7, 1),
                visit_filter=VisitFilter(section_name_contains=("眼科",)),
                soap_search=SoapSearch(r"APPLY\s+(?:NEW|OLD)", mode="regex"),
            )
            self.assertEqual(result.status, "OK")
            self.assertEqual(sdk.calls.count("case_list"), 1)
            self.assertEqual(sdk.calls.count("soap:NEW"), 1)
            self.assertEqual(sdk.calls.count("soap:OLD"), 1)
            self.assertIn("opd", sdk.calls)
            self.assertIn("surgery", sdk.calls)
            self.assertIn("audit_records", sdk.calls)
            history = json.loads(
                (Path(temp_dir) / "parsed" / "visit_history.json").read_text(encoding="utf-8")
            )
            self.assertEqual(history["schema_version"], 3)
            self.assertEqual(len(history["records"]), 2)

    def test_no_matching_case_is_packable_completed_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sdk = ProfileSDK()
            result = run_live_test(
                sdk,  # type: ignore[arg-type]
                output_dir=Path(temp_dir),
                profile="core",
                visit_filter=VisitFilter(section_codes=("999",)),
                soap_search=SoapSearch("APPLY"),
            )
            self.assertEqual(result.status, "COMPLETED_WITH_ERRORS")
            statuses = {step.name: step.status for step in result.steps}
            self.assertEqual(statuses["matching_visit_cases"], "MISSING")
            self.assertEqual(statuses["soap_search"], "MISSING")


if __name__ == "__main__":
    unittest.main()
