"""Login tester invariants: bounded failures, no simulator sockets, honest evidence."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from vghks_sdk import PortalCredentials, RequestPolicy, SDKSettings, VghksSDK
from vghks_sdk.core.errors import ConfigurationError, SDKError
from vghks_sdk.core.transport import SafeSessionTransport
from vghks_sdk.live.atomic import build_test_plan
from vghks_sdk.live.capture import RawCaptureRecorder
from vghks_sdk.live.config import LiveTestConfig, resolve_live_test_config
from vghks_sdk.live.login import (
    _personnel_option_value,
    expected_rejection,
    negative_login,
    password_post_budget,
)
from vghks_sdk.live.login_simulation import SCENARIOS, _MemoryHTTP, run_scenario
from vghks_sdk.live.profile import _run_step
from vghks_sdk.live_test_app import _interactive_wizard, _namespace_cli_values, build_parser, main
from vghks_sdk.local_io import write_json_atomic
from vghks_sdk.models import PersonnelOption, to_jsonable
from vghks_sdk.offline.analyze import inspect_bundle
from vghks_sdk.offline.bundle import BundleReader


def simulated_sdk(scenario, *, capture=None):
    session = requests.Session()
    adapter = _MemoryHTTP(scenario)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.trust_env = False
    policy = RequestPolicy(min_delay_seconds=0, max_delay_seconds=0)
    sdk = VghksSDK(
        settings=SDKSettings(portal_base_url="https://simulation.invalid", request_policy=policy),
        credentials=PortalCredentials("TST1", "synthetic-only"),
        transport=SafeSessionTransport(policy=policy, session=session, raw_capture=capture),
        raw_capture=capture,
    )
    return sdk, adapter


class LoginProfileTests(unittest.TestCase):
    def test_personnel_display_name_resolves_form_code_without_losing_hyphens(self):
        for value, displayed, option_label in (
            ("T1", "Synthetic clinic", "T1 - Synthetic clinic"),
            ("T1", "Synthetic clinic", "T1-Synthetic clinic"),
            ("T2", "Synthetic clinic - branch", "T2 - Synthetic clinic - branch"),
            ("D", "Synthetic title", "Synthetic title"),
        ):
            with self.subTest(option_label=option_label):
                self.assertEqual(
                    _personnel_option_value(displayed, (PersonnelOption(value, option_label),)),
                    value,
                )

    def test_personnel_option_matching_does_not_guess_or_choose_ambiguous_names(self):
        for label, options in (
            ("", (PersonnelOption("T1", "T1 - "),)),
            ("Synthetic", (PersonnelOption("T1", "T2 - Synthetic"),)),
            ("Synthetic", (PersonnelOption("T1", "T1 - Synthetic clinic"),)),
            ("Synthetic", (PersonnelOption("", "Synthetic"),)),
            (
                "Synthetic",
                (PersonnelOption("T1", "Synthetic"), PersonnelOption("T2", "T2 - Synthetic")),
            ),
        ):
            with self.subTest(label=label, options=options):
                self.assertIsNone(_personnel_option_value(label, options))

    def test_simulator_cannot_open_network_and_all_cases_pass(self):
        with patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
            for scenario in SCENARIOS:
                with self.subTest(scenario=scenario.name):
                    result = run_scenario(scenario)
                    self.assertTrue(result["passed"], result)
                    self.assertEqual(result["evidence"], "SIMULATED")

    def test_second_prepared_password_post_is_blocked_before_send(self):
        scenario = next(s for s in SCENARIOS if s.name == "wrong_password")
        sdk, adapter = simulated_sdk(scenario)
        with sdk:
            session = sdk._runtime.transport.session
            original = session.send
            with password_post_budget(session) as counts:
                session.post("https://simulation.invalid/login.do", data={"mpassword": "test"})
                with self.assertRaises(SDKError) as error:
                    session.post("https://simulation.invalid/login.do", data={"mpassword": "test"})
                self.assertEqual(error.exception.info.code, "LOGIN_TEST_POST_LIMIT")
            self.assertEqual(adapter.password_posts, 1)
            self.assertEqual(counts, {"password_posts": 1, "blocked_posts": 1})
            self.assertEqual(session.send, original)

    def test_requests_307_redirect_cannot_repost_to_another_path(self):
        scenario = next(s for s in SCENARIOS if s.name == "redirect_307")
        sdk, adapter = simulated_sdk(scenario)
        with sdk, password_post_budget(sdk._runtime.transport.session) as counts:
            with self.assertRaises(SDKError):
                sdk._runtime.transport.session.post(
                    "https://simulation.invalid/login.do",
                    data={"mpassword": "test"},
                )
            self.assertEqual(counts, {"password_posts": 1, "blocked_posts": 1})
            self.assertEqual(len(adapter.calls), 1)

    def test_unknown_login_response_is_an_error_and_keeps_post_counts(self):
        scenario = next(s for s in SCENARIOS if s.name == "empty_response")
        sdk, _ = simulated_sdk(scenario)
        with tempfile.TemporaryDirectory() as temporary, sdk:
            path = Path(temporary) / "counts.json"
            with self.assertRaises(SDKError) as caught:
                negative_login(sdk, lazy=False, audit_path=path)
            self.assertEqual(caught.exception.info.code, "PORTAL_LOGIN_RESPONSE_EMPTY")
            self.assertEqual(json.loads(path.read_text())["password_posts"], 1)

    def test_config_limits_and_plan_no_patient_or_writes(self):
        for bad in (-1, 3, True, "2"):
            with self.subTest(value=bad), self.assertRaises(ConfigurationError):
                LiveTestConfig(profile="login", login_negative_attempts=bad)
        config = resolve_live_test_config(
            json_values={"profile": "login", "login_negative_attempts": 0},
            environ={},
        )
        self.assertEqual(config.to_safe_dict()["login_negative_attempts"], 0)
        plan = build_test_plan(config)
        self.assertEqual(plan["negative_password_post_limit"], 0)
        self.assertTrue(plan["negative_tests_last"])
        self.assertTrue(
            all(row["scope"] in {"catalog", "doctor_personnel"} for row in plan["operations"])
        )
        self.assertTrue(plan["excluded_write_operations"])
        args = build_parser().parse_args(["--profile", "login", "--login-negative-attempts", "1"])
        self.assertEqual(_namespace_cli_values(args)["login_negative_attempts"], 1)

    def test_login_wizard_needs_no_patient_or_secondary_credentials(self):
        config = LiveTestConfig(profile="login")
        with (
            patch("builtins.input", side_effect=AssertionError("unexpected prompt")),
            redirect_stdout(io.StringIO()),
        ):
            self.assertIs(_interactive_wizard(config, quick=True), config)

    def test_zero_argument_uses_login_build_and_does_not_load_old_combined_preset(self):
        with (
            patch(
                "vghks_sdk.live_test_app.build_identity", return_value={"default_profile": "login"}
            ),
            patch("vghks_sdk.live_test_app.run_live_test_namespace", return_value=0) as run,
            patch("builtins.input", return_value=""),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main([]), 0)
        args = run.call_args.args[0]
        self.assertTrue(args.bundled_login)
        self.assertFalse(args.bundled_round)

    def test_expected_rejection_requires_completed_live_evidence(self):
        step = {
            "name": "login.negative.1",
            "status": "OK",
            "details": {
                "evidence": "LIVE",
                "expected_failure": True,
                "observed_code": "PORTAL_LOGIN_REJECTED",
                "password_posts": 1,
                "blocked_posts": 0,
            },
        }
        self.assertTrue(expected_rejection(step))
        self.assertFalse(expected_rejection({**step, "status": "ERROR"}))
        self.assertFalse(
            expected_rejection({**step, "details": {**step["details"], "password_posts": 2}})
        )
        self.assertFalse(expected_rejection({**step, "name": "login.normal"}))

    def test_offline_expected_rejection_does_not_hide_other_login_failures(self):
        for label in ("login.negative.1", "login.normal"):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "run"
                steps = []
                with RawCaptureRecorder(root) as capture:
                    scenario = next(s for s in SCENARIOS if s.name == "wrong_password")
                    sdk, _ = simulated_sdk(scenario, capture=capture)
                    with sdk, redirect_stdout(io.StringIO()):
                        _run_step(
                            steps,
                            name=label,
                            root=root,
                            raw_capture=capture,
                            operation=lambda sdk=sdk: negative_login(sdk, lazy=False),
                            output_path=root / "parsed/result.json",
                            summarize=lambda v: v,
                        )
                write_json_atomic(
                    root / "run_summary.json",
                    {"schema_version": 6, "status": "OK", "steps": to_jsonable(steps)},
                )
                write_json_atomic(
                    root / "run_config.json", {"profile": "login", "login_negative_attempts": 2}
                )
                with BundleReader(root) as reader:
                    report, retest = inspect_bundle(reader)
                self.assertEqual(retest["profile"], "login")
                self.assertEqual(
                    report["analysis_status"], "OK" if label.endswith(".1") else "NEEDS_ATTENTION"
                )
                self.assertEqual(
                    len(report["expected_login_rejections"]), int(label.endswith(".1"))
                )

    def test_bad_login_cannot_pass_when_guard_blocks_retry(self):
        scenario = next(s for s in SCENARIOS if s.name == "wrong_password")
        sdk, _ = simulated_sdk(scenario)
        with sdk:
            session = sdk._runtime.transport.session

            def replay():
                for _ in range(2):
                    session.post("https://simulation.invalid/login.do", data={"mpassword": "test"})

            sdk.auth = SimpleNamespace(login=Mock(side_effect=replay))
            with self.assertRaises(SDKError) as caught:
                negative_login(sdk, lazy=False)
            self.assertEqual(caught.exception.info.code, "LOGIN_TEST_POST_LIMIT")


if __name__ == "__main__":
    unittest.main()
