from __future__ import annotations

import csv
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from vghks_sdk.models import (
    NumericReport,
    NumericTable,
    OutpatientPatient,
    PatientDemographics,
    SoapRecord,
    VisitCase,
    VisitFilter,
)
from vghks_sdk.workflows import export_latest_records, export_visit_history

TEST_MRN = "00000000"
EYE_FILTER = VisitFilter(section_name_contains=("眼科",))


def visit_case(
    day: int | None,
    case_no: str,
    *,
    section_code: str = "70",
    section_name: str = "眼科上午",
    case_type: str = "O",
    mrn: str = TEST_MRN,
) -> VisitCase:
    return VisitCase(
        mrn=mrn,
        visit_date=date(2026, 1, day) if day is not None else None,
        case_type=case_type,
        case_no=case_no,
        section_code=section_code,
        section_name=section_name,
        index=0,
    )


class FakeSDK:
    def __init__(self) -> None:
        self.soap_calls: list[str] = []
        self.opd = self
        self.records = self
        self.patients = self

    def get_doctor_patients(self, doctor: str, day: date):
        return [OutpatientPatient(TEST_MRN, "測試病人", day, doctor_card=doctor)]

    def get_visit_cases(self, mrn: str):
        return [visit_case(3, "NEW"), visit_case(2, "OLD")]

    def find_visit_cases(self, mrn: str, visit_filter: VisitFilter):
        return visit_filter.select(self.get_visit_cases(mrn))

    def get_soap(self, case: VisitCase):
        self.soap_calls.append(case.case_no)
        text = "P: APPLY" if case.case_no == "NEW" else "P: old"
        return SoapRecord(case, (text,))

    def get_demographics(self, mrn: str):
        return PatientDemographics(mrn, "測試病人", "0900000000", "")

    def get_numeric_report(self, case: VisitCase):
        return NumericReport(
            case,
            (NumericTable("IOP", ("日期", "OD"), (("2026-01-03", "15"),)),),
        )


class EmptyRecordSDK(FakeSDK):
    def get_soap(self, case: VisitCase):
        self.soap_calls.append(case.case_no)
        return SoapRecord(case, ())

    def get_numeric_report(self, case: VisitCase):
        return NumericReport(case, ())


class PerPatientFailureSDK(FakeSDK):
    def __init__(self) -> None:
        super().__init__()
        self.visit_calls = 0

    def get_visit_cases(self, mrn: str):
        self.visit_calls += 1
        if self.visit_calls == 1:
            raise RuntimeError("synthetic unexpected failure")
        return [visit_case(4, "SECOND", mrn=mrn)]


class VisitFilterTests(unittest.TestCase):
    def test_name_normalization_code_exact_and_or_semantics(self) -> None:
        selector = VisitFilter(
            section_name_contains=("eye clinic",),
            section_codes=(" ７０ ",),
        )
        by_name = visit_case(1, "NAME", section_code="99", section_name="ＥＹＥ CLINIC 上午")
        by_code = visit_case(2, "CODE", section_code=" 70 ", section_name="家醫科")
        neither = visit_case(3, "NONE", section_code="700", section_name="家醫科")
        self.assertTrue(selector.matches(by_name))
        self.assertTrue(selector.matches(by_code))
        self.assertFalse(selector.matches(neither))

    def test_all_sections_date_bounds_missing_date_dedup_and_sort(self) -> None:
        selector = VisitFilter(
            all_sections=True,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 3),
        )
        first = visit_case(1, "FIRST", section_code="60", section_name="家醫科")
        last = visit_case(3, "LAST", section_code="70", section_name="眼科")
        selected = selector.select(
            [first, visit_case(None, "UNDATED"), last, last, visit_case(4, "LATE")]
        )
        self.assertEqual([case.case_no for case in selected], ["LAST", "FIRST"])

    def test_invalid_configurations_raise_configuration_error(self) -> None:
        from vghks_sdk.core.errors import ConfigurationError

        invalid = (
            lambda: VisitFilter(),
            lambda: VisitFilter(all_sections=True, section_codes=("70",)),
            lambda: VisitFilter(section_codes=("70",), case_types=("I",)),
            lambda: VisitFilter(section_codes=("70",), start_date=date(2026, 1, 1)),
            lambda: VisitFilter(
                section_codes=("70",),
                start_date=date(2026, 1, 2),
                end_date=date(2026, 1, 1),
            ),
        )
        for factory in invalid:
            with self.subTest(factory=factory), self.assertRaises(ConfigurationError):
                factory()

    def test_non_outpatient_and_unknown_section_do_not_match(self) -> None:
        selector = VisitFilter(section_name_contains=("眼科",))
        self.assertFalse(selector.matches(visit_case(1, "I", case_type="I")))
        self.assertEqual(selector.select([visit_case(1, "OTHER", section_name="家醫科")]), [])


class WorkflowTests(unittest.TestCase):
    def test_latest_records_selects_newest_matching_case_and_writes_schema_three(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "mrns.csv"
            input_path.write_text(f"病歷號\n{TEST_MRN}\n{TEST_MRN}\n", encoding="utf-8-sig")
            result = export_latest_records(
                FakeSDK(),  # type: ignore[arg-type]
                input_path=input_path,
                visit_filter=EYE_FILTER,
                output_dir=root / "out",
            )
            record = json.loads(result.records_path.read_text(encoding="utf-8"))
            self.assertEqual(result.record_count, 1)
            self.assertEqual(record["schema_version"], 3)
            self.assertEqual(record["case"]["case_no"], "NEW")
            with result.index_path.open(encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertIn("最新就診日期", row)

    def test_latest_records_reports_missing_payload_codes_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "mrns.txt"
            input_path.write_text(f"{TEST_MRN}\n", encoding="utf-8")
            sdk = EmptyRecordSDK()
            result = export_latest_records(
                sdk,  # type: ignore[arg-type]
                input_path=input_path,
                visit_filter=EYE_FILTER,
                output_dir=root / "out",
            )
            record = json.loads(result.records_path.read_text(encoding="utf-8"))
            self.assertEqual(record["status"], "SOAP_AND_NUMERIC_MISSING")
            self.assertEqual(
                [issue["code"] for issue in record["issues"]],
                ["SOAP_MISSING", "NUMERIC_MISSING"],
            )
            self.assertEqual(sdk.soap_calls, ["NEW"])

    def test_latest_records_continues_after_one_patient_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "mrns.txt"
            input_path.write_text(f"{TEST_MRN}\n", encoding="utf-8")
            with patch(
                "vghks_sdk.workflows.latest_records.read_mrns",
                return_value=[TEST_MRN, TEST_MRN],
            ):
                result = export_latest_records(
                    PerPatientFailureSDK(),  # type: ignore[arg-type]
                    input_path=input_path,
                    visit_filter=EYE_FILTER,
                    output_dir=root / "out",
                )
            records = [
                json.loads(line)
                for line in result.records_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [issue["code"] for issue in records[0]["issues"]],
                ["UNEXPECTED_ERROR"],
            )
            self.assertEqual(records[1]["status"], "OK")

    def test_visit_history_exports_every_matching_case_and_independent_parts(self) -> None:
        class HistorySDK(FakeSDK):
            def get_visit_cases(self, mrn: str):
                return [
                    visit_case(1, "OLDEST", section_code="60", section_name="家醫科"),
                    visit_case(6, "OTHER", section_code="70", section_name="眼科"),
                    visit_case(5, "NEWEST", section_code="60", section_name="家醫科"),
                    visit_case(5, "NEWEST", section_code="60", section_name="家醫科"),
                    visit_case(
                        7, "INPATIENT", section_code="60", section_name="家醫科", case_type="I"
                    ),
                ]

            def get_soap(self, case: VisitCase):
                self.soap_calls.append(case.case_no)
                if case.case_no == "NEWEST":
                    raise RuntimeError("SOAP fixture failure")
                return SoapRecord(case, ("S: complete", "P: APPLY"))

            def get_numeric_report(self, case: VisitCase):
                if case.case_no == "OLDEST":
                    raise RuntimeError("numeric fixture failure")
                return super().get_numeric_report(case)

        with tempfile.TemporaryDirectory() as temp_dir:
            sdk = HistorySDK()
            result = export_visit_history(
                sdk,
                mrns=[TEST_MRN, TEST_MRN],
                visit_filter=VisitFilter(section_codes=("60",)),
                output_dir=Path(temp_dir) / "out",
            )
            rows = [
                json.loads(line)
                for line in result.records_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(result.patient_count, 1)
            self.assertEqual(result.case_count, 2)
            self.assertEqual([row["case"]["case_no"] for row in rows], ["NEWEST", "OLDEST"])
            self.assertEqual(rows[0]["status"], "PARTIAL_ERROR")
            self.assertIsNone(rows[0]["soap"])
            self.assertEqual(len(rows[0]["numeric_report"]["tables"]), 1)
            self.assertEqual(rows[1]["status"], "PARTIAL_ERROR")
            self.assertIsNone(rows[1]["numeric_report"])
            self.assertEqual(rows[0]["schema_version"], 3)
            with result.index_path.open(encoding="utf-8-sig", newline="") as handle:
                headers = next(csv.reader(handle))
            self.assertIn("就診日期", headers)
            self.assertNotIn("眼科日期", headers)

    def test_visit_history_writes_generic_no_match_status_and_patient_error(self) -> None:
        class BatchSDK(FakeSDK):
            def __init__(self) -> None:
                super().__init__()
                self.visit_calls = 0

            def get_visit_cases(self, mrn: str):
                self.visit_calls += 1
                if self.visit_calls == 1:
                    raise RuntimeError("patient fixture failure")
                return []

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "vghks_sdk.workflows.visit_history.normalize_mrns",
                return_value=[TEST_MRN, TEST_MRN],
            ):
                result = export_visit_history(
                    BatchSDK(),  # type: ignore[arg-type]
                    mrns=[TEST_MRN],
                    visit_filter=VisitFilter(all_sections=True),
                    output_dir=Path(temp_dir) / "out",
                )
            rows = [
                json.loads(line)
                for line in result.records_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(rows[0]["status"], "ERROR")
            self.assertEqual(rows[1]["status"], "NO_MATCHING_VISIT")
            self.assertIsNone(rows[1]["case"])

    def test_visit_history_uses_unified_missing_statuses_for_each_case(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = export_visit_history(
                EmptyRecordSDK(),  # type: ignore[arg-type]
                mrns=[TEST_MRN],
                visit_filter=EYE_FILTER,
                output_dir=Path(temp_dir) / "out",
            )
            rows = [
                json.loads(line)
                for line in result.records_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(row["status"] == "SOAP_AND_NUMERIC_MISSING" for row in rows))
            self.assertTrue(all(row["soap_status"] == "SOAP_MISSING" for row in rows))
            self.assertTrue(all(row["numeric_status"] == "NUMERIC_MISSING" for row in rows))

    def test_visit_history_all_sections_exports_multiple_departments(self) -> None:
        class MixedSDK(FakeSDK):
            def get_visit_cases(self, mrn: str):
                return [
                    visit_case(2, "EYE", section_code="70", section_name="眼科"),
                    visit_case(3, "FAMILY", section_code="60", section_name="家醫科"),
                ]

        with tempfile.TemporaryDirectory() as temp_dir:
            result = export_visit_history(
                MixedSDK(),  # type: ignore[arg-type]
                mrns=[TEST_MRN],
                visit_filter=VisitFilter(all_sections=True),
                output_dir=Path(temp_dir) / "out",
            )
            rows = [
                json.loads(line)
                for line in result.records_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([row["case"]["case_no"] for row in rows], ["FAMILY", "EYE"])


if __name__ == "__main__":
    unittest.main()
