from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vghks_sdk.core.errors import ParseError
from vghks_sdk.live.atomic import run_atomic_test
from vghks_sdk.live.config import LiveTestConfig, resolve_live_test_config
from vghks_sdk.models import OutpatientPatient, SoapRecord, VisitCase
from vghks_sdk.parsing.prq import parse_opd_patients
from vghks_sdk.search import DoctorOpdPatientSource
from vghks_sdk.workflows.opd_soap import scan_opd_soap

DAY = date(2026, 9, 19)


def patient(mrn="TEST001", day=DAY, section="70", room="01", doctor="DOC1F"):
    return OutpatientPatient(
        mrn,
        "Synthetic",
        day,
        section_code=section,
        room=room,
        doctor_card=doctor,
        doctor_label_present=True,
    )


def case(mrn="TEST001", day=DAY, section="70", case_no="case1", case_type="O"):
    return VisitCase(mrn, day, case_type, case_no, section, "Synthetic section")


class OpdWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sdk = SimpleNamespace(
            opd=SimpleNamespace(get_doctor_patients=Mock(return_value=[])),
            records=SimpleNamespace(
                get_visit_cases=Mock(return_value=[]),
                get_soap=Mock(side_effect=lambda row: SoapRecord(row, ("P: ARRANGE cata",))),
            ),
        )

    def run_workflow(self, **kwargs):
        return scan_opd_soap(
            self.sdk,
            source=DoctorOpdPatientSource("DOC1", DAY - timedelta(days=6), DAY),
            output_dir=self.root,
            **kwargs,
        )

    def read(self, name):
        return json.loads((self.root / name).read_text(encoding="utf-8"))

    def test_shared_lists_same_date_and_section_only_and_cache_repeated_patient(self):
        earlier = DAY - timedelta(days=1)
        self.sdk.opd.get_doctor_patients.side_effect = lambda card, day: (
            [
                patient(day=day),
                patient(day=day, room="02"),
                patient("SHARED1", day, "71", doctor=""),
            ]
            if day in {earlier, DAY}
            else []
        )
        cases = [
            case(),
            case(day=earlier, case_no="case2"),
            case(day=DAY - timedelta(days=30), case_no="old"),
            case(section="71", case_no="shared"),
            case(case_type="I", case_no="inpatient"),
            case("OTHER01"),
        ]
        self.sdk.records.get_visit_cases.return_value = [*cases, cases[0]]
        result = self.run_workflow()
        self.assertEqual(result.status, "OK")
        self.assertEqual(self.sdk.opd.get_doctor_patients.call_count, 7)
        self.sdk.records.get_visit_cases.assert_called_once_with("TEST001")
        self.assertEqual(
            {call.args[0].case_no for call in self.sdk.records.get_soap.call_args_list},
            {"case1", "case2"},
        )
        self.assertEqual(result.counts["shared_registrations"], 2)
        self.assertEqual(result.counts["matches"], 2)
        self.assertEqual(result.counts["matched_patients"], 1)
        self.assertEqual(len(self.read("matches.json")[0]["registrations"]), 2)
        self.assertEqual(
            self.read("stage1_opd/2026-09-19.json")["section_counts"], {"70": 2, "71": 1}
        )
        self.assertEqual(len(self.read("stage2_visits/patient-0001.returned.json")), 7)
        self.assertEqual(
            self.read("matches.json")[0]["evaluation"]["evidence"]["matched_text"], "ARRANGE cata"
        )
        self.assertEqual(self.read("manifest.json")["source"]["card_no"], "DOC1")

    def test_registration_without_actual_visit_never_fetches_historical_soap(self):
        self.sdk.opd.get_doctor_patients.side_effect = lambda card, day: (
            [patient()] if day == DAY else []
        )
        self.sdk.records.get_visit_cases.return_value = [
            case(day=DAY - timedelta(days=1)),
            case(section="71"),
        ]
        result = self.run_workflow()
        self.sdk.records.get_soap.assert_not_called()
        self.assertEqual(result.counts["registrations_without_visit"], 1)
        self.assertEqual(self.read("matches.json"), [])

    def test_failures_continue_and_remain_unknown_instead_of_negative_results(self):
        def opd(card, day):
            if day == DAY - timedelta(days=5):
                raise ParseError("broken date")
            return [patient(f"TEST{i:03d}") for i in range(1, 5)] if day == DAY else []

        def visits(mrn):
            if mrn == "TEST001":
                raise ParseError("broken patient")
            return [case(mrn)]

        def soap(row):
            if row.mrn == "TEST002":
                raise ParseError("broken SOAP")
            return SoapRecord(row, () if row.mrn == "TEST003" else ("P: arrange CATA",))

        self.sdk.opd.get_doctor_patients.side_effect = opd
        self.sdk.records.get_visit_cases.side_effect = visits
        self.sdk.records.get_soap.side_effect = soap
        result = self.run_workflow()
        self.assertEqual(result.status, "INCOMPLETE")
        for key in (
            "days_failed",
            "visit_query_errors",
            "registrations_unknown",
            "soap_errors",
            "soap_missing",
            "matches",
        ):
            self.assertEqual(result.counts[key], 1, key)
        self.assertEqual(result.counts["patients_checked"], 4)
        self.assertEqual(result.counts["registrations_without_visit"], 0)
        self.assertEqual(
            self.read("stage2_visits/patient-0001.json")["registrations"][0]["status"], "UNKNOWN"
        )
        self.assertEqual(self.read("stage3_soap/patient-0003-case-0001.json")["status"], "MISSING")

    def test_unknown_doctors_are_retained_without_patient_lookup(self):
        self.sdk.opd.get_doctor_patients.side_effect = lambda card, day: (
            [patient(doctor="OTHER"), patient("OTHER70", section="72", doctor="DOC1F2")]
            if day == DAY
            else []
        )
        result = self.run_workflow()
        self.assertEqual(result.status, "INCOMPLETE")
        self.assertEqual(result.counts["unclassified_registrations"], 2)
        self.sdk.records.get_visit_cases.assert_not_called()

    def test_missing_mrn_is_preserved_and_marked_unqueryable(self):
        source = """<script>aryOpdSec[0]='70';aryOpdDoc[0]='DOC1F';</script>
        <script>if(aryOpdSec[0]=='70'){new KSCase('','1','','First','F','60');new KSCase('','1','','First','F','60');}</script>
        <script>if(aryOpdSec[0]=='70'){new KSCase('','2','','Second','M','60');}</script>"""
        rows = parse_opd_patients(source, visit_date=DAY, doctor_card="DOC1")
        self.assertEqual(len(rows), 2)
        self.sdk.opd.get_doctor_patients.side_effect = lambda card, day: rows if day == DAY else []
        result = self.run_workflow()
        self.assertEqual(result.status, "INCOMPLETE")
        self.assertEqual(result.counts["dedicated_registrations"], 2)
        self.assertEqual(result.counts["unqueryable_registrations"], 2)
        self.sdk.records.get_visit_cases.assert_not_called()
        self.assertEqual(
            self.read("stage1_opd/2026-09-19.json")["registrations"][0]["issue"], "MISSING_MRN"
        )

    def test_missing_case_date_is_unknown_and_wrong_soap_case_cannot_match(self):
        self.sdk.opd.get_doctor_patients.side_effect = lambda card, day: (
            [patient()] if day == DAY else []
        )
        self.sdk.records.get_visit_cases.return_value = [case(day=None), case(case_no="known")]
        self.sdk.records.get_soap.side_effect = None
        self.sdk.records.get_soap.return_value = SoapRecord(case("OTHER01"), ("arrange CATA",))
        result = self.run_workflow()
        self.assertEqual(result.status, "INCOMPLETE")
        self.assertEqual(result.counts["registrations_unknown"], 1)
        self.assertEqual(result.counts["soap_errors"], 1)
        self.assertEqual(result.counts["matches"], 0)

    def test_patients_are_not_sampled_and_context_stays_with_each_patient(self):
        self.sdk.opd.get_doctor_patients.side_effect = lambda card, day: (
            [patient(f"TEST{i:03d}") for i in range(15)] if day == DAY else []
        )
        current = []

        def visits(mrn):
            current[:] = [mrn]
            return [case(mrn, case_no=str(i)) for i in range(9)]

        def soap(row):
            self.assertEqual(current, [row.mrn])
            return SoapRecord(row, ("No phrase",))

        self.sdk.records.get_visit_cases.side_effect = visits
        self.sdk.records.get_soap.side_effect = soap
        result = self.run_workflow()
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.counts["patients_checked"], 15)
        self.assertEqual(result.counts["soap_checked"], 135)
        self.assertEqual(result.counts["matches"], 0)

    def test_interrupt_retains_already_completed_stages_and_matches(self):
        self.sdk.opd.get_doctor_patients.side_effect = lambda card, day: (
            [patient()] if day == DAY else []
        )
        first = case()
        self.sdk.records.get_visit_cases.return_value = [first, case(case_no="case2")]
        self.sdk.records.get_soap.side_effect = [
            SoapRecord(first, ("arrange CATA",)),
            KeyboardInterrupt(),
        ]
        with self.assertRaises(KeyboardInterrupt):
            self.run_workflow()
        self.assertEqual(self.read("manifest.json")["status"], "INTERRUPTED")
        self.assertEqual(len(self.read("matches.json")), 1)
        self.assertTrue((self.root / "stage2_visits/patient-0001.returned.json").is_file())

    def test_blocked_readiness_keeps_three_stage_status_files_without_network(self):
        result = self.run_workflow(blocked_reason="PRQ_READINESS_FAILED")
        self.assertEqual(result.status, "BLOCKED")
        for stage in ("stage1_opd", "stage2_visits", "stage3_soap"):
            self.assertEqual(self.read(stage + "/status.json")["status"], "BLOCKED")
        self.sdk.opd.get_doctor_patients.assert_not_called()

    def test_comprehensive_uses_login_card_and_includes_checkpoints_and_live_steps(self):
        from test_comprehensive_live_test import readiness

        self.sdk.auth = SimpleNamespace(check=Mock(side_effect=readiness))
        config = LiveTestConfig(
            profile="comprehensive", doctor_card="DIFFERENT", opd_date=DAY, weekly_opd_end=DAY
        )
        with (
            patch("vghks_sdk.live.atomic.resolve_queries", return_value=()),
            redirect_stdout(io.StringIO()),
        ):
            result = run_atomic_test(self.sdk, config, output_dir=self.root, login_card="LOGIN1")
        self.assertEqual(result.status, "OK")
        self.assertEqual(self.sdk.opd.get_doctor_patients.call_count, 7)
        self.assertTrue(
            all(
                call.args[0] == "LOGIN1" for call in self.sdk.opd.get_doctor_patients.call_args_list
            )
        )
        manifest = self.read("parsed/workflows/opd_soap_week/manifest.json")
        self.assertEqual(manifest["source"]["start"], "2026-09-13")
        self.assertEqual(manifest["status"], "OK")
        self.assertEqual(len([s for s in result.steps if s.name.startswith("weekly_opd.")]), 7)
        restored = resolve_live_test_config(json_values=config.to_safe_dict(), environ={})
        self.assertEqual(restored.weekly_opd_end, DAY)

    def test_detached_patient_scripts_keep_section_room_and_overlapping_registrations(self):
        source = """<script>aryOpdSec[0]='70';aryOpdRoom[0]='02';aryOpdDoc[0]='DOC1F';
        aryOpdSec[1]='71';aryOpdRoom[1]='01';aryOpdDoc[1]='';</script>
        <div id="room0"></div><script>if(aryOpdSec[0]=='70'){new KSCase('','1','TEST001','Name','F','60');}</script>
        <div id="room1"></div><script>if(aryOpdSec[1]=='71'){new KSCase('','1','TEST001','Name','F','60');}</script>"""
        rows = parse_opd_patients(source, visit_date=DAY, doctor_card="DOC1")
        self.assertEqual(
            [(row.section_code, row.room) for row in rows], [("70", "02"), ("71", "01")]
        )
        self.assertEqual(rows[0].doctor_card, "DOC1F")
        self.assertEqual(rows[1].doctor_card, "")
        self.assertTrue(rows[1].doctor_label_present)
        ambiguous = source.replace("aryOpdSec[0]=='70'", "aryOpdSec[0]=='70' && aryOpdSec[1]=='71'")
        self.assertEqual(
            parse_opd_patients(ambiguous, visit_date=DAY, doctor_card="DOC1")[0].section_code, ""
        )


if __name__ == "__main__":
    unittest.main()
