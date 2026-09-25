"""Focused source-level checks for the next intranet regression executable."""

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
from vghks_sdk.core.readiness import make_auth_report
from vghks_sdk.live.atomic import (
    _classify,
    _counts,
    _query_inputs,
    build_test_plan,
    run_atomic_test,
)
from vghks_sdk.live.config import LiveTestConfig
from vghks_sdk.live.presets import REGRESSION_QUERIES, regression_round
from vghks_sdk.live_test_app import main
from vghks_sdk.models import AuthCheckTarget, NumericHistoryReport, NumericTable, VisitCase
from vghks_sdk.queries import query_spec


class RegressionLiveTests(unittest.TestCase):
    def test_case_sample_includes_one_legacy_mrn_when_available(self) -> None:
        config = LiveTestConfig(profile="regression", test_mrn="TEST001", max_cases=2)
        cases = [
            VisitCase("TEST001", date(2026, 9, 21), "O", "NEW1", "70", "眼科", lookup_mrn="TEST001"),
            VisitCase("TEST001", date(2026, 9, 20), "O", "NEW2", "70", "眼科", lookup_mrn="TEST001"),
            VisitCase("OLD001", date(2015, 1, 1), "O", "OLD1", "70", "眼科", lookup_mrn="TEST001"),
        ]
        inputs = _query_inputs(query_spec("prq.soap"), config, {"prq.visit_cases": [cases]})
        self.assertEqual([item["case"].case_no for item in inputs], ["NEW1", "OLD1"])
        self.assertEqual(
            _counts(cases),
            {"record_count": 3, "source_mrn_count": 2, "related_mrn_visit_count": 1},
        )

    def test_plan_is_bounded_and_uses_real_auth_dependencies(self) -> None:
        config = LiveTestConfig(profile="regression", test_mrn="TEST001")
        with patch("requests.sessions.Session.request") as network:
            plan = build_test_plan(config)
        self.assertEqual(config.only_operations, REGRESSION_QUERIES)
        self.assertEqual([row["key"] for row in plan["operations"]], list(REGRESSION_QUERIES))
        self.assertEqual(plan["auth_targets"], ["portal", "sectord", "webmaas", "prq", "oppl"])
        self.assertFalse(plan["weekly_opd_soap"]["enabled"])
        self.assertFalse(plan["earnings_reports"]["enabled"])
        network.assert_not_called()

    def test_mismatch_blocks_soap_but_preserves_independent_results(self) -> None:
        targets = ("portal", "sectord", "webmaas", "prq", "oppl")
        readiness = make_auth_report(
            tuple(AuthCheckTarget(key, (), "", False, 1, 0, (), "OK") for key in targets)
        )
        numeric = NumericHistoryReport(
            "TEST001",
            (NumericTable("Va", ("日期", "OD"), (("2026-01-02", "0.8"),)),),
        )
        sdk = SimpleNamespace(
            auth=SimpleNamespace(check=Mock(return_value=readiness)),
            patients=SimpleNamespace(get_basic_info=Mock(return_value={"mrn": "TEST001"})),
            records=SimpleNamespace(
                get_visit_cases=Mock(
                    side_effect=ParseError(
                        "synthetic patient mismatch", code="PRQ_CASE_PATIENT_MISMATCH"
                    )
                ),
                get_soap=Mock(),
                get_numeric_report=Mock(),
                get_numeric_history=Mock(return_value=numeric),
            ),
            surgery=SimpleNamespace(get_schedule=Mock(return_value=[])),
        )
        today = date.today()
        config = LiveTestConfig(profile="regression", test_mrn="TEST001")
        ready = config.with_default_doctor("DOC1")
        self.assertEqual(ready.range_start, today - timedelta(days=29))
        self.assertEqual(ready.range_end, today + timedelta(days=30))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with redirect_stdout(io.StringIO()):
                result = run_atomic_test(sdk, ready, output_dir=root)
            steps = {step.name: step for step in result.steps}
            self.assertEqual(result.status, "COMPLETED_WITH_ERRORS")
            self.assertEqual(steps["prq.visit_cases.0001"].error_code, "PRQ_CASE_PATIENT_MISMATCH")
            self.assertEqual(steps["prq.soap"].status, "BLOCKED")
            self.assertEqual(steps["prq.numeric"].status, "BLOCKED")
            self.assertEqual(steps["prq.numeric_history.0001"].status, "OK")
            self.assertEqual(steps["oppl.surgery_schedule.0001"].status, "EMPTY")
            self.assertTrue((root / "parsed/atomic/prq.numeric_history/0001.json").is_file())
            self.assertTrue((root / "parsed/atomic/oppl.surgery_schedule/0001.json").is_file())
            self.assertEqual(
                json.loads((root / "parsed/inputs/oppl.surgery_schedule.json").read_text())[0][
                    "doctor_card"
                ],
                "DOC1",
            )
        sdk.records.get_soap.assert_not_called()
        sdk.records.get_numeric_report.assert_not_called()
        sdk.records.get_numeric_history.assert_called_once()
        sdk.surgery.get_schedule.assert_called_once()

    def test_built_default_profile_selects_regression_preset(self) -> None:
        self.assertEqual(regression_round()["only_operations"], list(REGRESSION_QUERIES))
        with (
            patch(
                "vghks_sdk.live_test_app.build_identity",
                return_value={"default_profile": "regression"},
            ),
            patch("vghks_sdk.live_test_app.run_live_test_namespace", return_value=0) as runner,
            patch("builtins.input", return_value=""),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main([]), 0)
        self.assertEqual(runner.call_args.args[0].profile, "regression")
        self.assertTrue(runner.call_args.args[0].bundled_regression)

    def test_numeric_alignment_issues_are_visible_in_step_status(self) -> None:
        report = NumericHistoryReport(
            "TEST001",
            (
                NumericTable(
                    "Va",
                    ("日期", "OD"),
                    (("2026-01-02", "0.8"),),
                    parsing_issues=("NUMERIC_HEADER_SPAN_MISMATCH",),
                ),
            ),
        )
        self.assertEqual(_counts(report)["numeric_issue_count"], 1)
        self.assertEqual(_counts(report)["numeric_error_count"], 1)
        self.assertEqual(_classify(report), "ERROR")

    def test_aligned_eye_span_mismatch_is_a_warning_with_raw_values_kept(self) -> None:
        report = NumericHistoryReport(
            "TEST001",
            (
                NumericTable(
                    "IOP",
                    ("日期", "IOP", "OS", "OD"),
                    (("2026-09-21", "error", ""),),
                    header_rows=(("日期", "IOP"), ("OS", "OD")),
                    column_paths=(("日期",), ("IOP", "OS"), ("IOP", "OD")),
                    parsing_issues=("NUMERIC_HEADER_SPAN_MISMATCH",),
                ),
            ),
        )
        self.assertEqual(
            _counts(report),
            {
                "record_count": 1,
                "numeric_row_count": 1,
                "numeric_issue_count": 1,
                "numeric_warning_count": 1,
                "numeric_error_count": 0,
            },
        )
        self.assertEqual(_classify(report), "OK")

    def test_unrelated_group_span_mismatch_stays_an_error(self) -> None:
        report = NumericHistoryReport(
            "TEST001",
            (
                NumericTable(
                    "Test",
                    ("日期", "Group", "A", "B"),
                    (("2026-09-21", "x", "y"),),
                    header_rows=(("日期", "Group"), ("A", "B")),
                    column_paths=(("日期",), ("Group", "A"), ("Group", "B")),
                    parsing_issues=("NUMERIC_HEADER_SPAN_MISMATCH",),
                ),
            ),
        )
        self.assertEqual(_counts(report)["numeric_error_count"], 1)
        self.assertEqual(_classify(report), "ERROR")


if __name__ == "__main__":
    unittest.main()
