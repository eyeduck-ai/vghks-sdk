from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from vghks_sdk.live.atomic import _query_inputs, build_test_plan
from vghks_sdk.live_test_app import main
from vghks_sdk.queries import query_spec


class BundledRoundTests(unittest.TestCase):
    def test_double_click_collects_separate_credentials_and_includes_all_requested_queries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # An old or malformed sidecar must not narrow this release's test.
            (root / "live-test-config.json").write_text("{obsolete", encoding="utf-8")
            with (
                patch("vghks_sdk.live_test_app.Path.cwd", return_value=root),
                patch.dict("os.environ", {"VGHKS_LIVE_PROFILE": "auth", "VGHKS_TEST_MRN": "0000000"}, clear=True),
                patch("builtins.input", side_effect=["SYNTHETIC-DOCTOR", "SYNTHETIC-ID", ""]),
                patch(
                    "getpass.getpass", side_effect=["PORTAL-SECRET", "PAYROLL-SECRET"]
                ) as password,
                patch("vghks_sdk.live_test_app._offer_incomplete_packaging"),
                patch("vghks_sdk.live_test_app.create_live_test_bundle"),
                patch(
                    "vghks_sdk.live_test_app.execute_live_test",
                    return_value=SimpleNamespace(exit_code=0),
                ) as execute,
                patch("requests.sessions.Session.request") as network,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(main([]), 0)
        config, portal = execute.call_args.args
        earnings = execute.call_args.kwargs["earnings_credentials"]
        self.assertEqual((portal.username, portal.password), ("SYNTHETIC-DOCTOR", "PORTAL-SECRET"))
        self.assertEqual(
            (earnings.national_id, earnings.password), ("SYNTHETIC-ID", "PAYROLL-SECRET")
        )
        self.assertEqual(password.call_count, 2)
        network.assert_not_called()
        plan = build_test_plan(config)
        self.assertTrue(plan["earnings_reports"]["enabled"])
        operations = {row["key"] for row in plan["operations"]}
        self.assertTrue(
            {
                "review.cases",
                "review.case_detail",
                "oppl_records.cases",
                "oppl_records.note",
                "oppl_records.pdf",
                "prq.surgery_history",
                "prq.pdf_attachment",
            }
            <= operations
        )
        surgery = _query_inputs(query_spec("oppl_records.cases"), config, {})
        self.assertEqual([row["filter"].period for row in surgery], ["24M", "2YB"])
        self.assertTrue(all(row["filter"].procedure_code == "80416" for row in surgery))
        self.assertTrue(all(row["filter"].surgeon_card == portal.username for row in surgery))
        history = _query_inputs(query_spec("prq.surgery_history"), config, {})
        self.assertEqual([row["filter"].lookback_days for row in history], [20000, 365])
        self.assertEqual(
            _query_inputs(query_spec("review.cases"), config, {})[0]["filter"].doctor_card,
            portal.username,
        )
        saved = json.dumps(config.to_safe_dict())
        for secret in (portal.password, earnings.national_id, earnings.password):
            self.assertNotIn(secret, saved)

    def test_skipping_secondary_credentials_keeps_other_tests_enabled(self):
        for national_id in ("", "SYNTHETIC-ID"):
            with (
                self.subTest(national_id=bool(national_id)),
                tempfile.TemporaryDirectory() as temporary,
            ):
                with (
                    patch("vghks_sdk.live_test_app.Path.cwd", return_value=Path(temporary)),
                    patch.dict(
                        "os.environ",
                        {"VGHKS_TEST_MRN": "0000000", "VGHKS_USERNAME": "SYNTHETIC", "VGHKS_PASSWORD": "PORTAL-SECRET"},
                        clear=True,
                    ),
                    patch("builtins.input", side_effect=[national_id, ""]),
                    patch("getpass.getpass", return_value=""),
                    patch("vghks_sdk.live_test_app._offer_incomplete_packaging"),
                    patch("vghks_sdk.live_test_app.create_live_test_bundle"),
                    patch(
                        "vghks_sdk.live_test_app.execute_live_test",
                        return_value=SimpleNamespace(exit_code=0),
                    ) as execute,
                    redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(main([]), 0)
                self.assertIsNone(execute.call_args.kwargs["earnings_credentials"])
                self.assertTrue(execute.call_args.args[0].include_earnings)
                self.assertEqual(execute.call_args.args[0].profile, "comprehensive")

    def test_default_plan_is_offline_and_explicit_config_still_selects_a_retest(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "custom.json"
            config.write_text(
                json.dumps(
                    {
                        "profile": "comprehensive",
                        "only_operations": ["review.cases"],
                        "weekly_opd_soap": False,
                    }
                ),
                encoding="utf-8",
            )
            for arguments, full in (
                (["--plan"], True),
                (["--plan", "--config", str(config)], False),
            ):
                with (
                    patch.dict("os.environ", {"VGHKS_TEST_MRN": "0000000"}, clear=True),
                    patch("builtins.input") as prompt,
                    patch("getpass.getpass") as password,
                    patch("requests.sessions.Session.request") as network,
                    redirect_stdout(io.StringIO()) as console,
                ):
                    self.assertEqual(main(arguments), 0)
                prompt.assert_not_called()
                password.assert_not_called()
                network.assert_not_called()
                plan = json.loads(console.getvalue())
                self.assertEqual(plan["earnings_reports"]["enabled"], full)
                self.assertEqual(plan["surgery_cases"]["enabled"], full)
                self.assertEqual(len(plan["operations"]), 57 if full else 2)


if __name__ == "__main__":
    unittest.main()
