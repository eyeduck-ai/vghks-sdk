from __future__ import annotations

import csv
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from vghks_sdk import (
    DoctorOpdPatientSource,
    MrnPatientSource,
    SoapSearch,
    VisitFilter,
)
from vghks_sdk.core.errors import AuthenticationError, ConfigurationError
from vghks_sdk.models import OutpatientPatient, PatientDemographics, SoapRecord, VisitCase
from vghks_sdk.workflows import scan_soap

TEST_MRN = "00000000"
OTHER_MRN = "00000001"
EYE_FILTER = VisitFilter(section_name_contains=("眼科",))


def make_case(
    mrn: str,
    day: int,
    case_no: str,
    *,
    section_code: str = "70",
    section_name: str = "眼科",
) -> VisitCase:
    return VisitCase(
        mrn=mrn,
        visit_date=date(2026, 1, day),
        case_type="O",
        case_no=case_no,
        section_code=section_code,
        section_name=section_name,
        index=0,
    )


class FakeScanSDK:
    def __init__(self) -> None:
        self.opd = self
        self.records = self
        self.patients = self
        self.opd_by_date: dict[date, object] = {}
        self.cases_by_mrn: dict[str, object] = {}
        self.soap_by_case: dict[str, object] = {}
        self.demographics_by_mrn: dict[str, object] = {}
        self.opd_calls: list[date] = []
        self.soap_calls: list[str] = []
        self.demographics_calls: list[str] = []

    def get_doctor_patients(self, doctor: str, day: date):
        self.opd_calls.append(day)
        result = self.opd_by_date.get(day, [])
        if isinstance(result, BaseException):
            raise result
        return result

    def find_visit_cases(self, mrn: str, visit_filter: VisitFilter):
        result = self.cases_by_mrn.get(mrn, [])
        if isinstance(result, BaseException):
            raise result
        return visit_filter.select(result)

    def get_soap(self, case: VisitCase):
        self.soap_calls.append(case.case_no)
        result = self.soap_by_case.get(case.case_no, SoapRecord(case, ()))
        if isinstance(result, BaseException):
            raise result
        return result

    def get_demographics(self, mrn: str):
        self.demographics_calls.append(mrn)
        result = self.demographics_by_mrn.get(
            mrn, PatientDemographics(mrn, "測試病人", "0900000000", "")
        )
        if isinstance(result, BaseException):
            raise result
        return result


class SearchModelTests(unittest.TestCase):
    def test_legacy_apply_models_and_workflow_are_not_exported(self) -> None:
        import vghks_sdk.models as models
        import vghks_sdk.workflows as workflows

        self.assertFalse(hasattr(models, "ApplyMatch"))
        self.assertFalse(hasattr(workflows, "scan_apply"))

    def test_mrn_source_normalizes_deduplicates_and_preserves_order(self) -> None:
        source = MrnPatientSource((" ００００００００ ", TEST_MRN, OTHER_MRN))
        self.assertEqual(source.mrns, (TEST_MRN, OTHER_MRN))
        with self.assertRaises(ConfigurationError):
            MrnPatientSource(())
        with self.assertRaises(ConfigurationError):
            MrnPatientSource(("bad value",))

    def test_doctor_source_validates_dates(self) -> None:
        source = DoctorOpdPatientSource(" Ｄ００１ ", date(2026, 1, 1), date(2026, 1, 2))
        self.assertEqual(source.card_no, "D001")
        with self.assertRaises(ConfigurationError):
            DoctorOpdPatientSource("D001", date(2026, 1, 2), date(2026, 1, 1))

    def test_literal_and_regex_flags_compile(self) -> None:
        literal = SoapSearch("apply", ignore_case=True).compile()
        self.assertEqual(literal.search("APPLY").group(), "APPLY")
        regex = SoapSearch(
            r"^P:.{0,100}APPLY\s+OD",
            mode="regex",
            ignore_case=True,
            multiline=True,
            dotall=True,
        ).compile()
        self.assertIsNotNone(regex.search("x\nP: apply\nOD"))
        for pattern in (r"[A-Z]+\s{1,5}OD", r"(APPLY|FOLLOW UP)", r".*APPLY.*"):
            with self.subTest(pattern=pattern):
                SoapSearch(pattern, mode="regex")

    def test_restricted_regex_rejects_unsafe_constructs(self) -> None:
        invalid = (
            "a*",
            r"\b",
            "a+b+",
            "a++",
            "a+?",
            "a*+",
            "(ab)+",
            "(ab){1,2}",
            "(?=a)a",
            "(?i)a",
            "(?P<value>a)",
            "(?(1)a|b)",
            r"(a)\1",
            "a{1,501}",
            "a{1,}",
            "a?b?c?d?e?f?g?h?i?Z",
            "a*a*a*Z",
            "x" * 257,
        )
        for pattern in invalid:
            with self.subTest(pattern=pattern), self.assertRaises(ConfigurationError):
                SoapSearch(pattern, mode="regex")
        with self.assertRaises(ConfigurationError):
            SoapSearch("")
        with self.assertRaises(ConfigurationError):
            SoapSearch("APPLY", multiline=True)


class SoapScanWorkflowTests(unittest.TestCase):
    def test_doctor_source_merges_dates_deduplicates_then_applies_limit(self) -> None:
        sdk = FakeScanSDK()
        first = date(2026, 1, 1)
        second = date(2026, 1, 2)
        sdk.opd_by_date[first] = [
            OutpatientPatient(TEST_MRN, "門診姓名", first),
            OutpatientPatient(OTHER_MRN, "第二人", first),
        ]
        sdk.opd_by_date[second] = [OutpatientPatient(TEST_MRN, "門診姓名", second)]
        case = make_case(TEST_MRN, 3, "MATCH")
        sdk.cases_by_mrn[TEST_MRN] = [case]
        sdk.soap_by_case["MATCH"] = SoapRecord(case, ("P: APPLY",))
        sdk.demographics_by_mrn[TEST_MRN] = RuntimeError("fixture")
        with tempfile.TemporaryDirectory() as temp_dir:
            result = scan_soap(
                sdk,
                source=DoctorOpdPatientSource("D001", first, second),
                search=SoapSearch("APPLY"),
                visit_filter=EYE_FILTER,
                output_dir=Path(temp_dir),
                max_patients=1,
            )
            match = json.loads(result.matches_path.read_text(encoding="utf-8"))
            status = json.loads(result.status_path.read_text(encoding="utf-8"))
        self.assertEqual(sdk.opd_calls, [first, second])
        self.assertEqual(result.patient_count, 1)
        self.assertEqual(match["source_opd_dates"], ["2026-01-01", "2026-01-02"])
        self.assertEqual(match["name"], "門診姓名")
        self.assertEqual(match["telephone"], "")
        self.assertEqual(status["status"], "MATCHED_WITH_ERRORS")

    def test_failed_doctor_day_aborts_before_output_creation(self) -> None:
        sdk = FakeScanSDK()
        first = date(2026, 1, 1)
        second = date(2026, 1, 2)
        sdk.opd_by_date[first] = []
        sdk.opd_by_date[second] = RuntimeError("source failure")
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "out"
            with self.assertRaises(RuntimeError):
                scan_soap(
                    sdk,
                    source=DoctorOpdPatientSource("D001", first, second),
                    search=SoapSearch("APPLY"),
                    visit_filter=EYE_FILTER,
                    output_dir=output,
                )
            self.assertFalse(output.exists())

    def test_literal_match_saves_complete_matching_block_and_stops(self) -> None:
        sdk = FakeScanSDK()
        newest = make_case(TEST_MRN, 3, "NEW")
        oldest = make_case(TEST_MRN, 2, "OLD")
        sdk.cases_by_mrn[TEST_MRN] = [oldest, newest]
        sdk.soap_by_case["NEW"] = SoapRecord(
            newest, ("S: first\nline", "P: before APPLY after\nnext")
        )
        sdk.soap_by_case["OLD"] = SoapRecord(oldest, ("P: APPLY older",))
        with tempfile.TemporaryDirectory() as temp_dir:
            result = scan_soap(
                sdk,
                source=MrnPatientSource((TEST_MRN, TEST_MRN)),
                search=SoapSearch("APPLY"),
                visit_filter=EYE_FILTER,
                output_dir=Path(temp_dir),
            )
            match = json.loads(result.matches_path.read_text(encoding="utf-8"))
        evidence = match["evidence"]
        self.assertEqual(sdk.soap_calls, ["NEW"])
        self.assertEqual(evidence["matched_text"], "APPLY")
        self.assertEqual(evidence["block_indices"], [1])
        self.assertEqual(evidence["evidence_scope"], "MATCHED_BLOCK")
        self.assertEqual(evidence["evidence_block_indices"], [1])
        self.assertEqual(evidence["evidence_blocks"], ["P: before APPLY after\nnext"])
        self.assertEqual(
            evidence["matched_text"],
            "\n\n".join(["S: first\nline", "P: before APPLY after\nnext"])[
                evidence["start"] : evidence["end"]
            ],
        )

    def test_cross_block_regex_saves_full_soap(self) -> None:
        sdk = FakeScanSDK()
        case = make_case(TEST_MRN, 3, "CROSS")
        blocks = ("S: one", "P: APPLY", "OD today\nnext")
        sdk.cases_by_mrn[TEST_MRN] = [case]
        sdk.soap_by_case["CROSS"] = SoapRecord(case, blocks)
        with tempfile.TemporaryDirectory() as temp_dir:
            result = scan_soap(
                sdk,
                source=MrnPatientSource((TEST_MRN,)),
                search=SoapSearch(r"APPLY\s+OD", mode="regex"),
                visit_filter=EYE_FILTER,
                output_dir=Path(temp_dir),
            )
            match = json.loads(result.matches_path.read_text(encoding="utf-8"))
        evidence = match["evidence"]
        self.assertEqual(evidence["matched_text"], "APPLY\n\nOD")
        self.assertEqual(evidence["block_indices"], [1, 2])
        self.assertEqual(evidence["evidence_scope"], "FULL_SOAP")
        self.assertEqual(evidence["evidence_blocks"], list(blocks))

    def test_all_matches_scans_every_case_but_only_first_occurrence(self) -> None:
        sdk = FakeScanSDK()
        cases = [make_case(TEST_MRN, 3, "NEW"), make_case(TEST_MRN, 2, "OLD")]
        sdk.cases_by_mrn[TEST_MRN] = cases
        sdk.soap_by_case["NEW"] = SoapRecord(cases[0], ("APPLY then APPLY",))
        sdk.soap_by_case["OLD"] = SoapRecord(cases[1], ("APPLY older",))
        with tempfile.TemporaryDirectory() as temp_dir:
            result = scan_soap(
                sdk,
                source=MrnPatientSource((TEST_MRN,)),
                search=SoapSearch("APPLY"),
                visit_filter=EYE_FILTER,
                output_dir=Path(temp_dir),
                all_matches=True,
            )
            rows = [
                json.loads(line)
                for line in result.matches_path.read_text(encoding="utf-8").splitlines()
            ]
        self.assertEqual(result.match_count, 2)
        self.assertEqual(sdk.soap_calls, ["NEW", "OLD"])
        self.assertEqual([row["evidence"]["start"] for row in rows], [0, 0])

    def test_missing_error_and_large_regex_continue_to_older_match(self) -> None:
        sdk = FakeScanSDK()
        cases = [
            make_case(TEST_MRN, 5, "MISSING"),
            make_case(TEST_MRN, 4, "ERROR"),
            make_case(TEST_MRN, 3, "LARGE"),
            make_case(TEST_MRN, 2, "MATCH"),
        ]
        sdk.cases_by_mrn[TEST_MRN] = cases
        sdk.soap_by_case["MISSING"] = SoapRecord(cases[0], ())
        sdk.soap_by_case["ERROR"] = RuntimeError("fixture")
        sdk.soap_by_case["LARGE"] = SoapRecord(cases[2], ("X" * 250_001,))
        sdk.soap_by_case["MATCH"] = SoapRecord(cases[3], ("P: APPLY",))
        with tempfile.TemporaryDirectory() as temp_dir:
            result = scan_soap(
                sdk,
                source=MrnPatientSource((TEST_MRN,)),
                search=SoapSearch(r"APPLY", mode="regex"),
                visit_filter=EYE_FILTER,
                output_dir=Path(temp_dir),
            )
            status = json.loads(result.status_path.read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "MATCHED_WITH_ERRORS")
        self.assertEqual(status["soap_missing_count"], 1)
        self.assertEqual(status["soap_error_count"], 2)
        self.assertIn(
            "SOAP_TEXT_TOO_LARGE",
            [issue["code"] for issue in status["issues"]],
        )
        self.assertEqual(result.error_count, 1)

    def test_patient_failure_continues_and_demographics_only_runs_after_match(self) -> None:
        sdk = FakeScanSDK()
        sdk.cases_by_mrn[TEST_MRN] = RuntimeError("fixture")
        case = make_case(OTHER_MRN, 3, "NONE")
        sdk.cases_by_mrn[OTHER_MRN] = [case]
        sdk.soap_by_case["NONE"] = SoapRecord(case, ("no keyword",))
        with tempfile.TemporaryDirectory() as temp_dir:
            result = scan_soap(
                sdk,
                source=MrnPatientSource((TEST_MRN, OTHER_MRN)),
                search=SoapSearch("APPLY"),
                visit_filter=EYE_FILTER,
                output_dir=Path(temp_dir),
            )
            statuses = [
                json.loads(line)
                for line in result.status_path.read_text(encoding="utf-8").splitlines()
            ]
        self.assertEqual([row["status"] for row in statuses], ["ERROR", "NO_MATCH"])
        self.assertEqual(sdk.demographics_calls, [])
        self.assertEqual(result.error_count, 1)

    def test_no_match_with_missing_soap_is_incomplete(self) -> None:
        sdk = FakeScanSDK()
        missing = make_case(TEST_MRN, 3, "MISSING")
        plain = make_case(TEST_MRN, 2, "PLAIN")
        sdk.cases_by_mrn[TEST_MRN] = [missing, plain]
        sdk.soap_by_case["MISSING"] = SoapRecord(missing, ("  ",))
        sdk.soap_by_case["PLAIN"] = SoapRecord(plain, ("no keyword",))
        with tempfile.TemporaryDirectory() as temp_dir:
            result = scan_soap(
                sdk,
                source=MrnPatientSource((TEST_MRN,)),
                search=SoapSearch("APPLY"),
                visit_filter=EYE_FILTER,
                output_dir=Path(temp_dir),
            )
            status = json.loads(result.status_path.read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "INCOMPLETE")
        self.assertEqual(status["soap_missing_count"], 1)
        self.assertEqual(status["soap_searched_count"], 1)

    def test_mrn_demographics_failure_leaves_identity_fields_blank(self) -> None:
        sdk = FakeScanSDK()
        case = make_case(TEST_MRN, 3, "MATCH")
        sdk.cases_by_mrn[TEST_MRN] = [case]
        sdk.soap_by_case["MATCH"] = SoapRecord(case, ("P: APPLY",))
        sdk.demographics_by_mrn[TEST_MRN] = RuntimeError("fixture")
        with tempfile.TemporaryDirectory() as temp_dir:
            result = scan_soap(
                sdk,
                source=MrnPatientSource((TEST_MRN,)),
                search=SoapSearch("APPLY"),
                visit_filter=EYE_FILTER,
                output_dir=Path(temp_dir),
            )
            match = json.loads(result.matches_path.read_text(encoding="utf-8"))
            status = json.loads(result.status_path.read_text(encoding="utf-8"))
        self.assertEqual(match["name"], "")
        self.assertEqual(match["telephone"], "")
        self.assertEqual(status["status"], "MATCHED_WITH_ERRORS")
        self.assertIn(
            "DEMOGRAPHICS_UNEXPECTED_ERROR",
            [issue["code"] for issue in status["issues"]],
        )

    def test_visit_filter_controls_specialty_and_inclusive_case_date(self) -> None:
        sdk = FakeScanSDK()
        eye = make_case(TEST_MRN, 2, "EYE")
        family = make_case(
            TEST_MRN,
            4,
            "FAMILY",
            section_code="60",
            section_name="家醫科",
        )
        sdk.cases_by_mrn[TEST_MRN] = [eye, family]
        sdk.soap_by_case["EYE"] = SoapRecord(eye, ("P: APPLY",))
        sdk.soap_by_case["FAMILY"] = SoapRecord(family, ("P: APPLY",))
        selector = VisitFilter(
            section_codes=("60",),
            start_date=date(2026, 1, 4),
            end_date=date(2026, 1, 4),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            result = scan_soap(
                sdk,
                source=MrnPatientSource((TEST_MRN,)),
                search=SoapSearch("APPLY"),
                visit_filter=selector,
                output_dir=Path(temp_dir),
            )
        self.assertEqual(result.match_count, 1)
        self.assertEqual(sdk.soap_calls, ["FAMILY"])

    def test_authentication_failure_is_fatal(self) -> None:
        sdk = FakeScanSDK()
        case = make_case(TEST_MRN, 3, "AUTH")
        sdk.cases_by_mrn[TEST_MRN] = [case]
        sdk.soap_by_case["AUTH"] = AuthenticationError("fixture")
        with tempfile.TemporaryDirectory() as temp_dir, self.assertRaises(AuthenticationError):
            scan_soap(
                sdk,
                source=MrnPatientSource((TEST_MRN,)),
                search=SoapSearch("APPLY"),
                visit_filter=EYE_FILTER,
                output_dir=Path(temp_dir),
            )

    def test_schema_outputs_index_manifest_and_overwrite_boundary(self) -> None:
        sdk = FakeScanSDK()
        case = make_case(TEST_MRN, 3, "MATCH")
        sdk.cases_by_mrn[TEST_MRN] = [case]
        sdk.soap_by_case["MATCH"] = SoapRecord(case, ("P: APPLY\ncomplete",))
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir)
            result = scan_soap(
                sdk,
                source=MrnPatientSource((TEST_MRN,)),
                search=SoapSearch("APPLY"),
                visit_filter=EYE_FILTER,
                output_dir=output,
            )
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            match = json.loads(result.matches_path.read_text(encoding="utf-8"))
            with result.index_path.open(encoding="utf-8-sig", newline="") as handle:
                index = next(csv.DictReader(handle))
            self.assertEqual(manifest["schema_version"], 4)
            self.assertEqual(match["schema_version"], 4)
            self.assertEqual(manifest["search"]["pattern"], "APPLY")
            self.assertNotIn("P: APPLY", json.dumps(index, ensure_ascii=False))
            self.assertFalse((output / "requests").exists())
            with self.assertRaises(ValueError):
                scan_soap(
                    sdk,
                    source=MrnPatientSource((TEST_MRN,)),
                    search=SoapSearch("APPLY"),
                    visit_filter=EYE_FILTER,
                    output_dir=output,
                )


if __name__ == "__main__":
    unittest.main()
