from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from vghks_sdk.contracts.har import BASELINE_CONTRACTS, HarEntry
from vghks_sdk.core.errors import ParseError, error_code
from vghks_sdk.live.atomic import _query_inputs
from vghks_sdk.live.config import LiveTestConfig, resolve_live_test_config
from vghks_sdk.live.profile import LiveTestStep, _overall_status
from vghks_sdk.offline.replay import replay_response
from vghks_sdk.parsing.documents import parse_document
from vghks_sdk.queries import query_spec


class PublicReleaseRegressionTests(unittest.TestCase):
    def test_nested_navigation_preserves_main_numbers_without_duplicate_wrappers(self):
        html = """<table id="wrapper"><tr><td><table id="report">
        <tr><td colspan="2"><table id="nav"><tr><td>Next</td></tr></table></td></tr>
        <tr><th>Category</th><th>Amount</th></tr>
        <tr><td>Synthetic</td><td>123.45</td></tr>
        </table></td></tr></table>"""
        result = parse_document(html, require_table=True)
        tables = {table.identifier: table.rows for table in result.tables}
        self.assertEqual(tables["report"], (("Category", "Amount"), ("Synthetic", "123.45")))
        self.assertNotIn("wrapper", tables)
        self.assertEqual(tables["nav"], (("Next",),))
        self.assertEqual(result.html, html)

    def test_legitimate_empty_opd_landing_is_not_an_error_but_unknown_page_is(self):
        form = '<input name="docCode"><input name="opdDate">'
        empty = replay_response("prq.opd_landing", (form + "查無看診病患清單").encode(), {})
        self.assertEqual(empty["status"], "EMPTY")
        self.assertEqual(
            replay_response("prq.opd_landing", form.encode(), {})["status"], "PARSE_ERROR"
        )
        self.assertEqual(replay_response("prq.opd_landing", b"login", {})["status"], "PARSE_ERROR")

    def test_mis_sso_accepts_actual_identity_field_and_rejects_blank_identity(self):
        validator = next(
            c.validator for c in BASELINE_CONTRACTS if c.operation_key == "portal.sso_from_dn"
        )
        fields = ("HID", "ssID", "keyOne", "keyTwo", "keyThree", "targetURL", "USR_ID")
        html = (
            "<form>" + "".join(f'<input name="{k}" value="SYNTHETIC">' for k in fields) + "</form>"
        )
        entry = HarEntry("GET", "/", frozenset(), frozenset(), {}, 200, html.encode(), "text/html")
        validator(entry)
        with self.assertRaises(ParseError):
            validator(
                HarEntry(
                    "GET",
                    "/",
                    frozenset(),
                    frozenset(),
                    {},
                    200,
                    html.replace(
                        'name="USR_ID" value="SYNTHETIC"', 'name="USR_ID" value=""'
                    ).encode(),
                    "text/html",
                )
            )

    def test_test_patient_is_per_configuration_and_serializes_without_credentials(self):
        one = LiveTestConfig(test_mrn="9000001")
        two = LiveTestConfig(test_mrn="9000002")
        spec = query_spec("webmaas.basic_info")
        self.assertEqual(_query_inputs(spec, one, {}), [{"mrn": "9000001"}])
        self.assertEqual(_query_inputs(spec, two, {}), [{"mrn": "9000002"}])
        config = resolve_live_test_config(
            cli_values={"test_mrn": "CLI"},
            json_values={"test_mrn": "JSON"},
            environ={"VGHKS_TEST_MRN": "ENV"},
        )
        self.assertEqual(config.test_mrn, "CLI")
        self.assertEqual(config.to_safe_dict()["test_mrn"], "CLI")

    def test_comprehensive_opd_covers_all_seven_days_including_weekdays(self):
        config = LiveTestConfig(
            profile="comprehensive",
            doctor_card="D001",
            opd_date=date(2026, 9, 20),
            range_start=date(2026, 9, 1),
            range_end=date(2026, 9, 20),
        )
        inputs = _query_inputs(query_spec("prq.opd_patients"), config, {})
        self.assertEqual([v["visit_date"].day for v in inputs], list(range(20, 13, -1)))

    def test_coverage_gaps_are_not_transport_failures_or_full_success(self):
        steps = [LiveTestStep("oppl.schedule_form", "NO_SAMPLE")]
        self.assertEqual(_overall_status(steps, fatal_auth=False), "COMPLETED_WITH_GAPS")
        steps.append(LiveTestStep("prq.soap", "ERROR"))
        self.assertEqual(_overall_status(steps, fatal_auth=False), "COMPLETED_WITH_ERRORS")

    def test_private_embedded_defaults_are_only_read_in_frozen_exe(self):
        from vghks_sdk.live.defaults import SYNTHETIC_MRN, default_test_mrn

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "_live_defaults.json"
            path.write_text(json.dumps({"test_mrn": "9000001"}), encoding="utf-8")
            with (
                patch("vghks_sdk.live.defaults.Path", return_value=path),
                patch("sys.frozen", False, create=True),
            ):
                self.assertEqual(default_test_mrn(), SYNTHETIC_MRN)
            with (
                patch("vghks_sdk.live.defaults.Path", return_value=path),
                patch("sys.frozen", True, create=True),
            ):
                self.assertEqual(default_test_mrn(), "9000001")

    def test_public_interactive_run_obtains_patient_before_starting_queries(self):
        from vghks_sdk.live_test_app import build_parser, run_live_test_namespace

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("builtins.input", side_effect=["9000001", "SYNTHETIC-USER"]),
            patch("getpass.getpass", return_value="SYNTHETIC-PASSWORD"),
            patch("vghks_sdk.live_test_app.create_live_test_bundle", return_value=MagicMock()),
            patch(
                "vghks_sdk.live_test_app.execute_live_test",
                return_value=SimpleNamespace(exit_code=0),
            ) as execute,
        ):
            self.assertEqual(
                run_live_test_namespace(build_parser().parse_args([]), force_interactive=True), 0
            )
        self.assertEqual(execute.call_args.args[0].test_mrn, "9000001")

    def test_public_unattended_run_requires_explicit_patient_before_credentials(self):
        from vghks_sdk.live_test_app import build_parser, run_live_test_namespace

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("sys.stdin.isatty", return_value=False),
            patch("vghks_sdk.live_test_app._noninteractive_credentials") as credentials,
            patch("vghks_sdk.live_test_app.execute_live_test") as execute,
            patch(
                "vghks_sdk.live_test_app.package_bootstrap_failure",
                return_value=SimpleNamespace(exit_code=2),
            ) as failure,
        ):
            args = build_parser().parse_args(
                ["--profile", "comprehensive", "--only", "webmaas.basic_info"]
            )
            self.assertEqual(run_live_test_namespace(args), 2)
        self.assertEqual(error_code(failure.call_args.args[0]), "TEST_MRN_REQUIRED")
        credentials.assert_not_called()
        execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
