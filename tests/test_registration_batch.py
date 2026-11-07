from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from vghks_sdk.core.errors import ConfigurationError, LoginRejectedError, ParseError
from vghks_sdk.core.readiness import make_auth_report
from vghks_sdk.live.atomic import build_test_plan
from vghks_sdk.live.config import LiveTestConfig, load_live_test_config, resolve_live_test_config
from vghks_sdk.live.registrations import run_registration_test
from vghks_sdk.live_test_app import (
    _interactive_wizard,
    _namespace_cli_values,
    build_parser,
    main,
)
from vghks_sdk.local_io import write_json_atomic, write_jsonl_line
from vghks_sdk.models import AuthCheckTarget, RegistrationRecord
from vghks_sdk.offline.retest import build_retest_config


class BatchSDK:
    def __init__(self, responses=None, *, login_error=False):
        self.auth = self
        self.patients = self
        self.responses = responses or {}
        self.calls = []
        self.auth_calls = []
        self.login_error = login_error

    def check(self, only=None):
        self.auth_calls.append(only)
        if self.login_error:
            raise LoginRejectedError("synthetic rejection")
        return make_auth_report(tuple(
            AuthCheckTarget(key, (key,), f"/{key}", False, 1, 0.0, (), "OK") for key in only
        ))

    def get_registration_history(self, mrn):
        self.calls.append(mrn)
        result = self.responses.get(mrn, [RegistrationRecord(
            {"掛號人員": "MWEB"}, mrn=mrn, registered_by="MWEB", no_show=True,
        )])
        if isinstance(result, BaseException):
            raise result
        return result


class RegistrationBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_batch(self, sdk, mrns=("TEST001", "TEST002", "TEST003")):
        with redirect_stdout(io.StringIO()), patch("socket.socket.connect", side_effect=AssertionError("offline only")):
            result = run_registration_test(
                sdk, LiveTestConfig(profile="registrations", patient_mrns=mrns), output_dir=self.root,
            )
        summary = json.loads((self.root / "parsed/registrations/batch_summary.json").read_text())
        return result, summary

    def test_config_deduplicates_preserves_zeroes_and_read_only_scope(self):
        config = LiveTestConfig(profile="registrations", patient_mrns=["000001", " 000001 ", "ＴＥＳＴ001"])
        self.assertEqual(config.patient_mrns, ("000001", "TEST001"))
        self.assertEqual(config.login_negative_attempts, 0)
        self.assertFalse(config.download_assets)
        self.assertFalse(config.weekly_opd_soap)
        self.assertEqual(build_test_plan(config)["patient_count"], 2)
        self.assertEqual([row["key"] for row in build_test_plan(config)["operations"]],
                         ["webmaas.registration_query"])
        self.assertIsNone(LiveTestConfig(profile="registrations").output_root)

    def test_invalid_inputs_and_unrelated_profiles_are_rejected_before_queries(self):
        for values in ("TEST001", ["../unsafe"], [123]):
            with self.subTest(values=values), self.assertRaises(ConfigurationError):
                LiveTestConfig(profile="registrations", patient_mrns=values)
        for field in ("login_negative_attempts", "include_surgery", "include_unsigned", "include_earnings"):
            with self.subTest(field=field), self.assertRaises(ConfigurationError):
                LiveTestConfig(profile="registrations", **{field: 1 if field == "login_negative_attempts" else True})
        with self.assertRaises(ConfigurationError):
            LiveTestConfig(profile="atomic", patient_mrns=["TEST001"])
        sdk = BatchSDK()
        with self.assertRaises(ConfigurationError):
            self.run_batch(sdk, ())
        self.assertEqual(sdk.auth_calls, [])

    def test_plan_without_inputs_is_offline_and_requests_input(self):
        plan = build_test_plan(LiveTestConfig(profile="registrations"))
        self.assertTrue(plan["patient_input_required"])
        self.assertEqual(plan["patient_count"], 0)
        self.assertEqual(plan["negative_password_post_limit"], 0)

    def test_cli_plan_without_inputs_does_not_require_credentials_or_network(self):
        output = io.StringIO()
        with redirect_stdout(output), patch("socket.socket.connect", side_effect=AssertionError("offline only")):
            self.assertEqual(main(["--profile", "registrations", "--plan"]), 0)
        plan = json.loads(output.getvalue())
        self.assertEqual(plan["profile"], "registrations")
        self.assertTrue(plan["patient_input_required"])

    def test_cli_plan_batch_inputs_select_batch_scope(self):
        output = io.StringIO()
        with redirect_stdout(output), patch("socket.socket.connect", side_effect=AssertionError("offline only")):
            self.assertEqual(main(["--plan", "--patient-mrn", "TEST001", "--patient-mrn", "TEST002"]), 0)
        plan = json.loads(output.getvalue())
        self.assertEqual((plan["profile"], plan["patient_count"]), ("registrations", 2))

    def test_all_patients_are_processed_above_the_old_sample_limit(self):
        mrns = tuple(f"TEST{index:04d}" for index in range(105))
        sdk = BatchSDK()
        step_checkpoints, batch_checkpoints = [], []

        def write_checkpoint(path, value):
            if path.name == "step_results.json":
                step_checkpoints.append(sum(step.name.startswith("registrations.patient_") for step in value))
            elif path.name == "batch_summary.json":
                batch_checkpoints.append(value["completed_patient_count"])
            return write_json_atomic(path, value)

        with (
            patch("vghks_sdk.live.registrations.write_json_atomic", side_effect=write_checkpoint),
            patch("vghks_sdk.live.steps.write_json_atomic", side_effect=write_checkpoint),
        ):
            result, summary = self.run_batch(sdk, mrns)
        self.assertEqual(sdk.calls, list(mrns))
        self.assertEqual((result.status, summary["completed_patient_count"]), ("OK", 105))
        self.assertEqual(summary["no_show_count"], 105)
        self.assertEqual(summary["pending_patient_count"], 0)
        self.assertEqual([count for count in step_checkpoints if count], [100, 105])
        self.assertEqual(batch_checkpoints, [0, 100, 105])
        journal = self.root / "parsed/registrations/patient_results.jsonl"
        entries = [json.loads(line) for line in journal.read_bytes().splitlines()]
        self.assertEqual([entry["mrn"] for entry in entries], list(mrns))

    def test_interrupted_journal_sync_rolls_back_the_in_flight_patient(self):
        sdk = BatchSDK()

        def append(handle, entry, *, durable):
            self.assertTrue(durable)
            self.assertTrue((self.root / entry["step"]["output"]).is_file())
            if entry["index"] == 2:
                with patch("vghks_sdk.local_io.os.fsync", side_effect=[KeyboardInterrupt(), None]):
                    write_jsonl_line(handle, entry, durable=durable)
            else:
                write_jsonl_line(handle, entry, durable=durable)

        with patch("vghks_sdk.live.registrations.write_jsonl_line", side_effect=append), self.assertRaises(KeyboardInterrupt):
            self.run_batch(sdk)
        parsed = self.root / "parsed/registrations"
        journal = [json.loads(line) for line in (parsed / "patient_results.jsonl").read_bytes().splitlines()]
        summary = json.loads((parsed / "batch_summary.json").read_bytes())
        self.assertEqual([entry["index"] for entry in journal], [1])
        self.assertEqual((summary["completed_patient_count"], summary["pending_patient_count"]), (1, 2))
        self.assertTrue((parsed / "patients/000002.json").is_file())
        self.assertEqual(sdk.calls, ["TEST001", "TEST002"])

    def test_empty_error_and_success_remain_distinct_and_continue(self):
        sdk = BatchSDK({"TEST001": [], "TEST002": ParseError("synthetic layout")})
        result, summary = self.run_batch(sdk)
        self.assertEqual(sdk.calls, ["TEST001", "TEST002", "TEST003"])
        self.assertEqual(result.status, "COMPLETED_WITH_ERRORS")
        self.assertEqual(summary["patient_status_counts"], {"OK": 1, "EMPTY": 1, "ERROR": 1, "BLOCKED": 0})
        self.assertEqual(json.loads((self.root / "parsed/registrations/patients/000001.json").read_text()), [])
        self.assertFalse((self.root / "parsed/registrations/patients/000002.json").exists())
        self.assertEqual(summary["registered_by_codes"], {"MWEB": 1})

    def test_unknown_attendance_stays_unknown_and_no_show_sample_gap_is_preserved(self):
        sdk = BatchSDK({"TEST001": [RegistrationRecord({}, mrn="TEST001", registered_by="OPDTESTF")]})
        result, summary = self.run_batch(sdk, ("TEST001",))
        self.assertEqual(result.status, "COMPLETED_WITH_GAPS")
        self.assertEqual((summary["no_show_count"], summary["attendance_unknown_count"]), (0, 1))
        rows = json.loads((self.root / "parsed/registrations/patients/000001.json").read_text())
        self.assertIsNone(rows[0]["no_show"])
        self.assertEqual(rows[0]["registered_by"], "OPDTESTF")
        self.assertEqual(result.steps[-1].status, "NO_SAMPLE")
        self.assertNotIn("OPDTESTF", json.dumps([step.details for step in result.steps]))

    def test_failed_login_blocks_every_patient_without_query_attempts(self):
        sdk = BatchSDK(login_error=True)
        result, summary = self.run_batch(sdk)
        self.assertEqual(result.status, "AUTHENTICATION_FAILED")
        self.assertEqual(sdk.calls, [])
        self.assertEqual(len(sdk.auth_calls), 1)
        self.assertEqual(summary["blocked_patient_count"], 3)

    def test_mid_batch_authentication_error_stops_remaining_patients(self):
        sdk = BatchSDK({"TEST002": LoginRejectedError("synthetic rejection")})
        result, summary = self.run_batch(sdk)
        self.assertEqual(result.status, "AUTHENTICATION_FAILED")
        self.assertEqual(sdk.calls, ["TEST001", "TEST002"])
        self.assertEqual(summary["patient_status_counts"], {"OK": 1, "EMPTY": 0, "ERROR": 1, "BLOCKED": 1})

    def test_interruption_preserves_completed_results_and_pending_count(self):
        sdk = BatchSDK({"TEST002": KeyboardInterrupt()})
        with self.assertRaises(KeyboardInterrupt):
            self.run_batch(sdk)
        summary = json.loads((self.root / "parsed/registrations/batch_summary.json").read_text())
        self.assertEqual((summary["status"], summary["completed_patient_count"], summary["pending_patient_count"]),
                         ("INTERRUPTED", 1, 2))
        self.assertTrue((self.root / "parsed/registrations/patients/000001.json").is_file())
        self.assertEqual(sdk.calls, ["TEST001", "TEST002"])

    def test_json_and_retest_preserve_all_batch_inputs(self):
        path = self.root / "config.json"
        path.write_text(json.dumps({"schema_version": 8, "profile": "registrations",
                                    "patient_mrns": ["000001", "TEST002"]}), encoding="utf-8")
        config = resolve_live_test_config(json_values=load_live_test_config(path), environ={})
        recipe = build_retest_config(config.to_safe_dict(), [], {"phase": "AUTHENTICATION"}, "AUTHENTICATION_FAILED")
        self.assertEqual(recipe["profile"], "registrations")
        self.assertEqual(recipe["patient_mrns"], ["000001", "TEST002"])
        self.assertEqual(recipe["login_negative_attempts"], 0)

    def test_cli_text_csv_and_repeated_mrn_inputs(self):
        for name, text in (("list.txt", "000001\nTEST002\n000001\n"),
                           ("list.csv", "mrn\n000001\nTEST002\n000001\n")):
            path = self.root / name
            path.write_text(text, encoding="utf-8-sig")
            args = build_parser().parse_args(["--mrn-file", str(path)])
            values = _namespace_cli_values(args)
            self.assertEqual(values["profile"], "registrations")
            self.assertEqual(values["patient_mrns"], ["000001", "TEST002"])
        args = build_parser().parse_args(["--profile", "registrations", "--patient-mrn", "TEST001",
                                          "--patient-mrn", "TEST002"])
        self.assertEqual(_namespace_cli_values(args)["patient_mrns"], ["TEST001", "TEST002"])

    def test_interactive_paste_deduplicates_and_file_with_spaces_is_supported(self):
        config = LiveTestConfig(profile="registrations")
        with patch("builtins.input", side_effect=["", "TEST001，TEST002", "TEST001 TEST003", ""]), redirect_stdout(io.StringIO()):
            resolved = _interactive_wizard(config)
        self.assertEqual(resolved.patient_mrns, ("TEST001", "TEST002", "TEST003"))
        path = self.root / "patient list.txt"
        path.write_text("TEST004\nTEST005\n", encoding="utf-8")
        with patch("builtins.input", return_value=f'"{path}"'), redirect_stdout(io.StringIO()):
            resolved = _interactive_wizard(config)
        self.assertEqual(resolved.patient_mrns, ("TEST004", "TEST005"))

    def test_zero_argument_start_selects_registration_build_profile(self):
        with (patch("vghks_sdk.live_test_app.build_identity", return_value={"default_profile": "registrations"}),
              patch("vghks_sdk.live_test_app.run_live_test_namespace", return_value=0) as run,
              patch("builtins.input", return_value=""), redirect_stdout(io.StringIO())):
            self.assertEqual(main([]), 0)
        self.assertEqual(run.call_args.args[0].profile, "registrations")
        self.assertFalse(run.call_args.args[0].bundled_round)


if __name__ == "__main__":
    unittest.main()
