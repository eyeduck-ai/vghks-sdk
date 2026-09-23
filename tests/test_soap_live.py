"""SOAP EXE plan and continuation over synthetic multi-patient OPD data."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vghks_sdk.core.errors import ParseError
from vghks_sdk.live.atomic import build_test_plan, run_atomic_test
from vghks_sdk.live.config import LiveTestConfig, resolve_live_test_config
from vghks_sdk.live.soap import select_soap_patients
from vghks_sdk.models import OutpatientPatient, SoapDiagnosis, SoapRecord, VisitCase

DAY = date(2026, 9, 21)


def patient(mrn: str, doctor: str = "DOC1F", section: str = "70") -> OutpatientPatient:
    return OutpatientPatient(
        mrn, "Synthetic", DAY, section_code=section, room="01",
        doctor_card=doctor, doctor_label_present=True,
    )


def case(mrn: str, day: date = DAY, section: str = "70") -> VisitCase:
    return VisitCase(mrn, day, "O", "SYN001", section, "Synthetic section")


class SoapLiveTests(unittest.TestCase):
    def test_plan_selects_distinct_dedicated_mrns_and_does_not_require_a_test_mrn(self):
        config = resolve_live_test_config(cli_values={"profile": "soap"}, environ={})
        plan = build_test_plan(config)
        self.assertEqual(config.soap_date, DAY)
        self.assertEqual(config.max_cases, 8)
        self.assertEqual(
            [item["key"] for item in plan["operations"]],
            ["prq.opd_patients", "prq.visit_cases", "prq.soap"],
        )
        roster = [
            patient("P1"), patient("P1"), patient("P2", section="V1"),
            patient("SHARED", doctor=""), patient("OTHER", doctor="OTHER"),
        ]
        self.assertEqual(
            {row.mrn for row in select_soap_patients(roster, doctor_card="DOC1", limit=8)},
            {"P1", "P2"},
        )

    def test_patient_failure_does_not_stop_other_mrns_and_preserves_all_stages(self):
        roster = [
            patient("P1"), patient("P2", section="V1"), patient("P3"),
            patient("P4"), patient("SHARED", doctor=""),
        ]

        def visits(mrn: str):
            if mrn == "P2":
                raise ParseError("synthetic visit failure", code="SYNTHETIC_VISIT_FAILURE")
            if mrn == "P3":
                return [case(mrn, date(2026, 9, 20))]
            return [case(mrn)]

        def soap(row: VisitCase):
            if row.mrn == "P4":
                raise ParseError("synthetic SOAP failure", code="SYNTHETIC_SOAP_FAILURE")
            return SoapRecord(
                row, ("S: synthetic", "O: synthetic", "A+P: synthetic"),
                subjective="synthetic", objective="synthetic", assessment_plan="synthetic",
                diagnoses=(SoapDiagnosis("H00", "Synthetic", "ICD", "H00 Synthetic"),),
                present_sections=("S", "O", "AP", "DIAGNOSES"),
            )

        sdk = SimpleNamespace(
            opd=SimpleNamespace(get_doctor_patients=Mock(return_value=roster)),
            records=SimpleNamespace(
                get_visit_cases=Mock(side_effect=visits), get_soap=Mock(side_effect=soap)
            ),
        )
        readiness = SimpleNamespace(
            ok=True,
            targets=[
                SimpleNamespace(target="portal", status="OK"),
                SimpleNamespace(target="prq", status="OK"),
            ],
        )
        with tempfile.TemporaryDirectory() as temporary, patch(
            "vghks_sdk.live.soap.independent_readiness", return_value=readiness
        ):
            root = Path(temporary)
            result = run_atomic_test(
                sdk, LiveTestConfig(profile="soap"), output_dir=root, login_card="DOC1"
            )
            self.assertEqual(result.status, "COMPLETED_WITH_ERRORS")
            self.assertEqual(sdk.records.get_visit_cases.call_count, 4)
            self.assertEqual(sdk.records.get_soap.call_count, 2)
            self.assertEqual(
                {call.args[0].mrn for call in sdk.records.get_soap.call_args_list},
                {"P1", "P4"},
            )
            summary = json.loads((root / "run_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["selected_patient_count"], 4)
            self.assertEqual(summary["soap_response_count"], 1)
            self.assertTrue((root / "parsed/soap/roster.json").is_file())
            self.assertTrue((root / "parsed/soap/classified_roster.json").is_file())
            self.assertTrue((root / "parsed/soap/patients/0001/visit_cases.json").is_file())
            self.assertTrue((root / "parsed/soap/patients/0001/soap_0001.json").is_file())
            self.assertTrue((root / "parsed/soap/patients/0003/matching_visits.json").is_file())
            coverage = json.loads(
                (root / "parsed/soap/field_coverage.json").read_text(encoding="utf-8")
            )
            self.assertEqual(coverage["field_record_counts"]["diagnoses"], 1)
            self.assertEqual(coverage["field_record_counts"]["orders"], 0)
            self.assertIn("SYNTHETIC_SOAP_FAILURE", {
                row["issue"]["code"] for row in summary["steps"] if row["issue"]
            })

    def test_empty_roster_is_a_gap_without_querying_unrelated_patients(self):
        sdk = SimpleNamespace(
            opd=SimpleNamespace(get_doctor_patients=Mock(return_value=[])),
            records=SimpleNamespace(get_visit_cases=Mock(), get_soap=Mock()),
        )
        readiness = SimpleNamespace(
            ok=True,
            targets=[
                SimpleNamespace(target="portal", status="OK"),
                SimpleNamespace(target="prq", status="OK"),
            ],
        )
        with tempfile.TemporaryDirectory() as temporary, patch(
            "vghks_sdk.live.soap.independent_readiness", return_value=readiness
        ):
            result = run_atomic_test(
                sdk, LiveTestConfig(profile="soap"),
                output_dir=Path(temporary), login_card="DOC1",
            )
            self.assertEqual(result.status, "COMPLETED_WITH_GAPS")
            sdk.records.get_visit_cases.assert_not_called()
            sdk.records.get_soap.assert_not_called()


if __name__ == "__main__":
    unittest.main()
