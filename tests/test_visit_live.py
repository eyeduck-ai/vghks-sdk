from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vghks_sdk.core.errors import ParseError
from vghks_sdk.core.readiness import make_auth_report, resolve_auth_targets
from vghks_sdk.live.atomic import build_test_plan, run_atomic_test
from vghks_sdk.live.config import LiveTestConfig
from vghks_sdk.live_test_app import main
from vghks_sdk.models import AuthCheckTarget, PatientBasicInfo, SoapRecord, VisitCase
from vghks_sdk.offline.analyze import _retest_config

MRN = "00000000"
IDENTIFIER = "SYNTHETIC-ID"


def sample_cases():
    return [
        VisitCase(
            MRN,
            date(2026, 1, 4 - n),
            category,
            f"C{n}",
            "70",
            "眼科",
            doctor_name="測試醫師",
            doctor_card="D001",
        )
        for n, category in enumerate(("O", "O", "A", "E"))
    ]


def ready(*, only):
    return make_auth_report(
        AuthCheckTarget(s.key, (), "", False, 1, 0, s.dependencies, "OK")
        for s in resolve_auth_targets(only)
    )


class VisitLiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cases = sample_cases()
        self.sdk = SimpleNamespace(
            auth=SimpleNamespace(check=Mock(side_effect=ready)),
            patients=SimpleNamespace(
                get_basic_info=Mock(
                    return_value=PatientBasicInfo(MRN, "Synthetic", national_id=IDENTIFIER)
                )
            ),
            records=SimpleNamespace(
                get_visit_cases=Mock(return_value=self.cases),
                get_soap=Mock(side_effect=lambda case: SoapRecord(case, ("synthetic SOAP",))),
            ),
            orders=SimpleNamespace(get_case_orders=Mock(return_value=[{"synthetic": "order"}])),
        )
        self.config = LiveTestConfig(profile="visits", test_mrn=MRN)

    def run_profile(self, **kwargs):
        with redirect_stdout(io.StringIO()):
            return run_atomic_test(self.sdk, self.config, output_dir=self.root, **kwargs)

    def test_scope_auto_id_equivalence_filters_and_bounded_followup(self):
        result = self.run_profile()
        self.assertEqual(result.status, "OK")
        plan = json.loads((self.root / "test_plan.json").read_text())
        self.assertEqual(
            {row["key"] for row in plan["operations"]},
            {"webmaas.basic_info", "prq.visit_cases", "prq.soap", "prq.case_orders"},
        )
        self.sdk.patients.get_basic_info.assert_called_once_with(MRN)
        self.assertEqual(self.sdk.records.get_visit_cases.call_args_list[0].args, (MRN,))
        self.assertEqual(
            self.sdk.records.get_visit_cases.call_args_list[1].kwargs, {"national_id": IDENTIFIER}
        )
        self.sdk.records.get_soap.assert_called_once_with(self.cases[0])
        self.sdk.orders.get_case_orders.assert_called_once_with(self.cases[0])
        self.assertTrue((self.root / "parsed/visits/filters/combined.json").is_file())
        self.assertTrue((self.root / "parsed/visits/by_national_id.json").is_file())

    def test_manual_id_needs_only_portal_and_prq_and_no_basic_query(self):
        result = self.run_profile(patient_national_id=IDENTIFIER)
        self.assertEqual(result.status, "OK")
        self.sdk.patients.get_basic_info.assert_not_called()
        plan = json.loads((self.root / "test_plan.json").read_text())
        self.assertEqual(plan["auth_targets"], ["portal", "prq"])

    def test_id_failure_still_runs_filters_and_both_followup_operations(self):
        self.sdk.records.get_visit_cases.side_effect = [
            self.cases,
            ParseError("synthetic ID failure"),
        ]
        self.sdk.records.get_soap.side_effect = [
            ParseError("synthetic SOAP failure"),
            SoapRecord(self.cases[1], ("ok",)),
        ]
        result = self.run_profile()
        self.assertEqual(result.status, "COMPLETED_WITH_ERRORS")
        self.assertEqual(self.sdk.records.get_soap.call_count, 2)
        self.sdk.orders.get_case_orders.assert_called_once()
        self.assertTrue(
            any(s.name == "visits.filters.combined" and s.status == "OK" for s in result.steps)
        )
        source = json.loads((self.root / "parsed/visits/selection_source.json").read_text())
        self.assertEqual(source["source"], "mrn_fallback")

    def test_mrn_failure_does_not_stop_manual_id_lookup(self):
        self.sdk.records.get_visit_cases.side_effect = [ParseError("baseline failed"), self.cases]
        result = self.run_profile(patient_national_id=IDENTIFIER)
        self.assertEqual(result.status, "COMPLETED_WITH_ERRORS")
        self.sdk.records.get_soap.assert_called_once_with(self.cases[0])

    def test_wrong_patient_is_reported_and_never_used_for_followup(self):
        wrong = [replace(case, mrn="OTHER") for case in self.cases]
        self.sdk.records.get_visit_cases.side_effect = [self.cases, wrong]
        result = self.run_profile(patient_national_id=IDENTIFIER)
        self.assertEqual(result.status, "COMPLETED_WITH_ERRORS")
        self.assertTrue(any(s.error_code == "TEST_PATIENT_MISMATCH" for s in result.steps))
        self.assertTrue(
            all(call.args[0].mrn == MRN for call in self.sdk.records.get_soap.call_args_list)
        )

    def test_empty_category_is_gap_but_absent_raw_doctor_card_is_not_required(self):
        self.sdk.records.get_visit_cases.return_value = [replace(self.cases[0], doctor_card="")]
        result = self.run_profile()
        self.assertEqual(result.status, "COMPLETED_WITH_GAPS")
        self.assertEqual(result.error_count, 0)
        missing = {s.name for s in result.steps if s.status == "NO_SAMPLE"}
        self.assertIn("visits.filters.category_A", missing)
        self.assertNotIn("visits.filters.doctor_card", {s.name for s in result.steps})

    def test_empty_lists_produce_no_clinical_requests(self):
        self.sdk.records.get_visit_cases.return_value = []
        result = self.run_profile()
        self.assertEqual(result.status, "COMPLETED_WITH_GAPS")
        self.sdk.records.get_soap.assert_not_called()
        self.sdk.orders.get_case_orders.assert_not_called()

    def test_retest_recipe_preserves_id_comparison_scope(self):
        recipe = _retest_config(self.config.to_safe_dict(), [], None, "COMPLETED_WITH_ERRORS")
        self.assertEqual(recipe["profile"], "visits")
        self.assertEqual(recipe["only_operations"], [])

    def test_built_default_profile_is_used_for_zero_argument_start(self):
        with (
            patch(
                "vghks_sdk.live_test_app.build_identity", return_value={"default_profile": "visits"}
            ),
            patch("vghks_sdk.live_test_app.Path.cwd", return_value=self.root),
            patch.dict(
                "os.environ",
                {
                    "VGHKS_USERNAME": "SYNTHETIC",
                    "VGHKS_PASSWORD": "SECRET",
                    "VGHKS_TEST_MRN": MRN,
                    "VGHKS_LIVE_PROFILE": "comprehensive",
                },
                clear=True,
            ),
            patch("builtins.input", side_effect=["", "", ""]),
            patch("vghks_sdk.live_test_app._offer_incomplete_packaging"),
            patch("vghks_sdk.live_test_app.create_live_test_bundle"),
            patch(
                "vghks_sdk.live_test_app.execute_live_test",
                return_value=SimpleNamespace(exit_code=0),
            ) as execute,
            patch("getpass.getpass") as password,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main([]), 0)
        config = execute.call_args.args[0]
        self.assertEqual(config.profile, "visits")
        self.assertEqual(config.max_cases, 3)
        self.assertFalse(config.include_earnings)
        self.assertIsNone(execute.call_args.kwargs["patient_national_id"])
        password.assert_not_called()
        self.assertEqual(len(build_test_plan(config)["operations"]), 4)


if __name__ == "__main__":
    unittest.main()
