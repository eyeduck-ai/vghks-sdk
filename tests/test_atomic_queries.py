from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vghks_sdk.core.errors import AuthenticationError, ConfigurationError, ErrorInfo, ParseError
from vghks_sdk.core.readiness import make_auth_report
from vghks_sdk.live.atomic import build_test_plan, run_atomic_test
from vghks_sdk.live.config import LiveTestConfig, resolve_live_test_config
from vghks_sdk.live.profile import LIVE_TEST_MRN
from vghks_sdk.live_test_app import main
from vghks_sdk.models import (
    AuthCheckTarget,
    BinaryAsset,
    ClinicalOrder,
    NumericReport,
    OrderDetailRef,
    OrderReport,
    OrderReportRef,
    PacsImageRef,
    PacsStudy,
    PacsStudyRef,
    SoapRecord,
    VisitCase,
    VisitFilter,
)
from vghks_sdk.queries import Queries, resolve_queries


def readiness(*keys: str):
    return make_auth_report(
        tuple(AuthCheckTarget(key, (), "", False, 1, 0, (), "OK") for key in ("portal", *keys))
    )


class AtomicQueriesTests(unittest.TestCase):
    def setUp(self):
        self.case = VisitCase(LIVE_TEST_MRN, date(2026, 1, 2), "O", "case-b", "60", "眼科")
        older = VisitCase(LIVE_TEST_MRN, date(2026, 1, 1), "O", "case-a", "60", "眼科")
        self.sdk = SimpleNamespace(
            auth=SimpleNamespace(check=Mock(return_value=readiness("prq", "webmaas"))),
            records=SimpleNamespace(
                get_visit_cases=Mock(return_value=[older, self.case]),
                get_soap=Mock(return_value=SoapRecord(self.case, ("S: synthetic",))),
                get_numeric_report=Mock(return_value=NumericReport(self.case, ())),
            ),
            patients=SimpleNamespace(get_demographics=Mock(return_value={"synthetic": True})),
        )
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_config(self, **kwargs):
        config = LiveTestConfig(profile="atomic", **kwargs)
        with redirect_stdout(io.StringIO()):
            result = run_atomic_test(self.sdk, config, output_dir=self.root)
        return result

    def test_catalog_dispatch_does_not_implicitly_fetch_dependencies(self):
        result = Queries(self.sdk).run("prq.soap", case=self.case)
        self.assertEqual(result.blocks, ("S: synthetic",))
        self.sdk.records.get_visit_cases.assert_not_called()
        self.sdk.records.get_soap.assert_called_once_with(self.case)

    def test_unknown_query_and_wrong_inputs_fail_before_service_call(self):
        for key, arguments in [("unknown", {}), ("prq.soap", {"mrn": "123"})]:
            with self.assertRaises(ConfigurationError):
                Queries(self.sdk).run(key, **arguments)
        self.sdk.records.get_soap.assert_not_called()

    def test_plan_expands_and_deduplicates_without_sdk_or_credentials(self):
        config = LiveTestConfig(
            profile="atomic", only_operations=("prq.soap", "prq.numeric", "prq.soap")
        )
        with patch("requests.sessions.Session.request") as network:
            plan = build_test_plan(config)
        self.assertEqual(
            [row["key"] for row in plan["operations"]],
            ["prq.visit_cases", "prq.soap", "prq.numeric"],
        )
        self.assertEqual(plan["auth_targets"], ["prq"])
        network.assert_not_called()

    def test_soap_only_fetches_latest_case_and_only_required_app(self):
        result = self.run_config(only_operations=("prq.soap",))
        self.assertEqual(result.status, "OK")
        self.sdk.auth.check.assert_called_once_with(only=("prq",))
        self.sdk.records.get_soap.assert_called_once_with(self.case)
        self.sdk.records.get_numeric_report.assert_not_called()
        self.sdk.patients.get_demographics.assert_not_called()
        self.assertTrue((self.root / "test_plan.json").is_file())
        self.assertTrue((self.root / "step_results.json").is_file())

    def test_case_limit_and_explicit_date_filter(self):
        self.run_config(only_operations=("prq.soap",), max_cases=2, visit_date=date(2026, 1, 1))
        self.assertEqual(self.sdk.records.get_soap.call_count, 1)
        self.assertEqual(self.sdk.records.get_soap.call_args.args[0].case_no, "case-a")

    def test_parser_failure_does_not_block_independent_query(self):
        self.sdk.records.get_soap.side_effect = ParseError(
            "synthetic", code="PRQ_SOAP_CONTAINER_MISSING"
        )
        result = self.run_config(only_operations=("prq.soap", "prq.numeric"))
        self.assertEqual(result.status, "COMPLETED_WITH_ERRORS")
        self.sdk.records.get_numeric_report.assert_called_once()
        self.assertEqual(result.steps[-1].status, "EMPTY")

    def test_discovery_failure_blocks_dependents_without_calling_them(self):
        self.sdk.records.get_visit_cases.side_effect = ParseError("synthetic")
        result = self.run_config(only_operations=("prq.soap", "webmaas.demographics"))
        self.sdk.records.get_soap.assert_not_called()
        self.sdk.patients.get_demographics.assert_called_once()
        self.assertEqual(
            next(step for step in result.steps if step.operation == "prq.soap").status, "BLOCKED"
        )

    def test_no_matching_case_is_no_sample_not_verified(self):
        result = self.run_config(
            only_operations=("prq.soap",), visit_filter=VisitFilter(section_codes=("999",))
        )
        self.sdk.records.get_soap.assert_not_called()
        self.assertEqual(result.steps[-1].status, "NO_SAMPLE")

    def test_empty_query_is_explicit_and_has_no_positive_evidence(self):
        self.sdk.records.get_soap.return_value = SoapRecord(self.case, ())
        result = self.run_config(only_operations=("prq.soap",))
        self.assertEqual(result.steps[-1].status, "EMPTY")
        self.assertEqual(result.steps[-1].details["record_count"], 0)

    def test_auth_expiry_stops_later_queries(self):
        self.sdk.records.get_soap.side_effect = AuthenticationError("synthetic")
        result = self.run_config(only_operations=("prq.soap", "prq.numeric"))
        self.sdk.records.get_numeric_report.assert_not_called()
        self.assertEqual(result.status, "AUTHENTICATION_FAILED")
        self.assertEqual(result.steps[-1].status, "BLOCKED")

    def test_tls_readiness_is_connectivity_not_password_failure(self):
        self.sdk.auth.check.return_value = make_auth_report(
            (
                AuthCheckTarget(
                    "portal",
                    (),
                    "",
                    False,
                    0,
                    1,
                    (),
                    "ERROR",
                    ErrorInfo("NETWORK_TLS_FAILED", "NETWORK"),
                ),
            )
        )
        result = self.run_config(only_operations=("prq.soap",))
        self.assertEqual(result.status, "CONNECTIVITY_FAILED")
        self.sdk.records.get_visit_cases.assert_not_called()
        self.assertEqual(result.steps[0].issue.code, "NETWORK_TLS_FAILED")

    def test_interrupt_leaves_completed_steps_on_disk(self):
        self.sdk.records.get_soap.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.run_config(only_operations=("prq.soap",))
        checkpoint = json.loads((self.root / "step_results.json").read_text())
        self.assertEqual(
            [row["name"] for row in checkpoint], ["auth_check", "prq.visit_cases.0001"]
        )

    def test_invalid_selection_and_limits_rejected_before_network(self):
        for values in (
            {"max_cases": 0},
            {"max_items": True},
            {"max_cases": 101},
            {"only_operations": "prq.soap"},
            {"only_operations": ["unknown"]},
        ):
            with self.assertRaises(ConfigurationError):
                LiveTestConfig(profile="atomic", **values)
        with self.assertRaises(ConfigurationError):
            LiveTestConfig(
                profile="atomic", only_operations=("oppl.surgery_schedule",)
            ).validate_for_execution()
        with self.assertRaises(ConfigurationError):
            LiveTestConfig(
                profile="atomic", only_operations=("prq.pdf_attachment",), download_assets=False
            ).validate_for_execution()

    def test_config_schema_four_roundtrip_and_old_schema_supported(self):
        config = LiveTestConfig(profile="atomic", only_operations=("prq.soap",), max_cases=3)
        restored = resolve_live_test_config(json_values=config.to_safe_dict(), environ={})
        self.assertEqual(restored.only_operations, config.only_operations)
        self.assertEqual(restored.max_cases, 3)

    def test_pacs_dependencies_and_asset_count_are_bounded(self):
        detail_ref = OrderDetailRef(LIVE_TEST_MRN, "c", "O", "1")
        report_ref = OrderReportRef(LIVE_TEST_MRN, "c", "O", "1")
        study_ref = PacsStudyRef(LIVE_TEST_MRN, "r")
        images = tuple(PacsImageRef(LIVE_TEST_MRN, "r", "s", "t", str(i)) for i in range(5))
        self.sdk.orders = SimpleNamespace(
            get_order_history=Mock(
                return_value=[
                    ClinicalOrder(
                        LIVE_TEST_MRN,
                        "c",
                        "O",
                        "DBR",
                        detail_ref=detail_ref,
                        report_ref=report_ref,
                        pacs_ref=study_ref,
                    )
                ]
            ),
            get_order_detail=Mock(side_effect=ParseError("detail failed")),
            get_order_report=Mock(
                return_value=OrderReport(report_ref, {"synthetic": "field"}, pacs_refs=(study_ref,))
            ),
            get_pacs_study=Mock(return_value=PacsStudy(study_ref, images)),
            download_pacs_image=Mock(return_value=BinaryAsset(b"\xff\xd8\xff\xd9", "image/jpeg")),
        )
        self.run_config(only_operations=("prq.pacs_image",), max_items=2)
        # A failed detail lookup must not discard direct report/PACS refs.
        self.sdk.orders.get_order_report.assert_called_once_with(report_ref)
        self.sdk.orders.download_pacs_image.assert_has_calls(
            [unittest.mock.call(images[0]), unittest.mock.call(images[1])]
        )
        self.assertEqual(self.sdk.orders.download_pacs_image.call_count, 2)
        self.assertEqual(len(list((self.root / "parsed").rglob("*.jpg"))), 2)

    def test_cli_plan_and_catalog_are_offline(self):
        with (
            patch("requests.sessions.Session.request") as network,
            patch("vghks_sdk.live_test_app._interactive_credentials") as credentials,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(["--plan", "--only", "prq.soap"]), 0)
            self.assertEqual(main(["--list-operations"]), 0)
        credentials.assert_not_called()
        network.assert_not_called()

    def test_every_catalog_dependency_is_resolvable(self):
        resolved = resolve_queries(tuple(spec.key for spec in Queries.catalog))
        self.assertEqual(len(resolved), 55)

    def test_zero_argument_exe_uses_combined_round(self):
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("vghks_sdk.live_test_app.run_live_test_namespace", return_value=0) as runner,
            patch("builtins.input", return_value=""),
        ):
            self.assertEqual(main([]), 0)
        self.assertEqual(runner.call_args.args[0].profile, "comprehensive")

    def test_auth_profile_never_queries_clinical_data(self):
        with redirect_stdout(io.StringIO()):
            result = run_atomic_test(self.sdk, LiveTestConfig(profile="auth"), output_dir=self.root)
        self.assertEqual([step.name for step in result.steps], ["auth_check"])
        self.sdk.records.get_visit_cases.assert_not_called()
        self.sdk.patients.get_demographics.assert_not_called()
        self.assertIn("audit", self.sdk.auth.check.call_args.kwargs["only"])


if __name__ == "__main__":
    unittest.main()
