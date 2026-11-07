from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from vghks_sdk.core.errors import AuthenticationError, ConfigurationError, ParseError
from vghks_sdk.integrations.smartiris import SmartIrisAvailability
from vghks_sdk.live.pacs_launch import (
    NativeCapture,
    PacsLaunchCapture,
    _loopback_settings,
    build_plan,
    main,
    run_pacs_launch_test,
)
from vghks_sdk.models import ClinicalOrder
from vghks_sdk.models.pacs_launch import PacsLaunchRef


class PacsLaunchLiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.reference = PacsLaunchRef("0000000", "SYNTHETIC-ORDER")
        self.sdk = Mock()
        self.sdk.orders.prepare_pacs_launch.return_value = {"status": "READY", "study_count": 1}

    def run_test(self, **kwargs):
        return run_pacs_launch_test(self.sdk, self.reference, output_dir=self.root, **kwargs)

    @staticmethod
    def ready_client():
        client = Mock()
        client.check_availability.return_value = SmartIrisAvailability(
            status="AVAILABLE", installation="PRESENT", process="RUNNING",
            listener="LISTENING", owner="MATCH_EXPECTED_PATH", port=15516, issues=(),
        )
        return client

    def test_default_only_prepares_and_preserves_no_identifiers_in_summary(self):
        factory = Mock()
        summary = self.run_test(client_factory=factory)
        self.assertEqual(summary["status"], "PREPARED")
        self.sdk.auth.login.assert_called_once_with()
        self.sdk.orders.prepare_pacs_launch.assert_called_once_with(self.reference)
        factory.assert_not_called()
        self.assertNotIn(self.reference.mrn, json.dumps(summary))
        self.assertNotIn(self.reference.request_no, json.dumps(summary))
        self.assertEqual(build_plan()["native_handoff_calls"], 0)

    def test_login_and_prepare_failures_never_dispatch_native(self):
        for stage, error in (("login", AuthenticationError("synthetic")),
                             ("prepare_pacs_launch", ParseError("synthetic"))):
            with self.subTest(stage=stage):
                self.sdk.reset_mock(side_effect=True)
                factory = Mock(return_value=self.ready_client())
                target = self.sdk.auth if stage == "login" else self.sdk.orders
                getattr(target, stage).side_effect = error
                summary = self.run_test(open_viewer=True, client_factory=factory)
                self.assertEqual(summary["status"], "ERROR")
                self.assertEqual(summary["native_calls"], 0)
                if stage == "login":
                    factory.assert_not_called()
                factory.return_value.open_study.assert_not_called()

    def test_handoff_and_human_observation_are_distinct(self):
        from vghks_sdk.integrations.smartiris import SmartIrisLaunchResult
        result = SmartIrisLaunchResult(status="HANDOFF_SENT", steps=())
        client = self.ready_client()
        client.open_study.return_value = result
        summary = self.run_test(open_viewer=True, client_factory=Mock(return_value=client),
                                observe=lambda: "MATCHED")
        self.assertEqual(summary["handoff"], "HANDOFF_SENT")
        self.assertEqual(summary["manual_observation"], "MATCHED")
        self.assertIsNone(summary["native_result"]["viewer_loaded"])
        client.open_study.assert_called_once()
        client.close.assert_called_once()

    def test_failed_handoff_never_prompts_or_retries(self):
        from vghks_sdk.integrations.smartiris import SmartIrisLaunchResult
        client, observation = self.ready_client(), Mock()
        client.open_study.return_value = SmartIrisLaunchResult(status="OUTCOME_UNKNOWN", steps=())
        summary = self.run_test(open_viewer=True, client_factory=Mock(return_value=client),
                                observe=observation)
        self.assertEqual(summary["status"], "ERROR")
        self.assertEqual(summary["native_calls"], 1)
        self.assertEqual(summary["handoff"], "OUTCOME_UNKNOWN")
        observation.assert_not_called()
        client.open_study.assert_called_once()

    def test_wrong_study_observation_is_failure_not_success(self):
        from vghks_sdk.integrations.smartiris import SmartIrisLaunchResult
        client = self.ready_client()
        client.open_study.return_value = SmartIrisLaunchResult(status="HANDOFF_SENT", steps=())
        summary = self.run_test(open_viewer=True, client_factory=Mock(return_value=client),
                                observe=lambda: "WRONG_STUDY")
        self.assertEqual(summary["status"], "OBSERVATION_FAILED")
        self.assertEqual(summary["exit_code"], 2)

    def test_interrupt_saves_last_native_result(self):
        from vghks_sdk.integrations.smartiris import SmartIrisLaunchResult
        client = self.ready_client()
        client.open_study.side_effect = KeyboardInterrupt
        client.last_result = SmartIrisLaunchResult(status="OUTCOME_UNKNOWN", steps=())
        summary = self.run_test(open_viewer=True, client_factory=Mock(return_value=client))
        self.assertEqual(summary["status"], "INTERRUPTED")
        self.assertEqual(summary["exit_code"], 130)
        self.assertEqual(summary["handoff"], "OUTCOME_UNKNOWN")
        saved = json.loads((self.root / "run_summary.json").read_text())
        self.assertEqual(saved, summary)

    def test_native_capture_keeps_exact_body_only_in_private_files(self):
        capture = NativeCapture(self.root / "private/native")
        capture(SimpleNamespace(
            endpoint_path="/login", method="GET", request_url="http://127.0.0.1:15516/login?id=FAKE",
            http_status=200, response_headers={"Content-Type": "text/plain"},
            body=b"ok", complete=True, error_code="",
        ))
        self.assertEqual((self.root / "private/native/001.body").read_bytes(), b"ok")
        self.assertEqual(json.loads((self.root / "private/native/001.json").read_text())["body_file"],
                         "001.body")

    def test_capture_filters_password_post_and_retains_initial_response(self):
        response = requests.Response()
        response.status_code = 200
        response._content = b"synthetic response"
        response.headers["Content-Type"] = "text/html"
        common = dict(attempt=1, max_attempts=1, throttle_delay_seconds=0,
                      elapsed_seconds=0, will_retry=False)
        with PacsLaunchCapture(self.root / "raw") as capture:
            response.url = "https://localhost/login.do"
            response.request = requests.Request("POST", response.url,
                                                data={"mpassword": "DO-NOT-SAVE"}).prepare()
            capture.record_response(response=response, **common)
            self.assertEqual(capture.capture_count, 0)
            response.url = "http://localhost/SmartWonderRAD/Query/qryExternalAP.jsp"
            response.request = requests.Request("GET", response.url).prepare()
            capture.record_response(response=response, **common)
            self.assertEqual(capture.capture_count, 1)
        for path in (self.root / "raw").rglob("*"):
            if path.is_file():
                self.assertNotIn(b"DO-NOT-SAVE", path.read_bytes())

    def test_loopback_config_cannot_leave_production_endpoint(self):
        from dataclasses import fields

        from vghks_sdk import SDKSettings
        endpoints = {item.name: "http://127.0.0.1:1234/disabled" for item in fields(SDKSettings)
                     if item.name.endswith("_base_url")}
        endpoints["portal_base_url"] = "https://portal.vghks.gov.tw"
        path = self.root / "config.json"
        path.write_text(json.dumps({"endpoint_overrides": endpoints,
                                    "bridge_url": "http://127.0.0.1:1235", "ca_bundle": None}))
        with self.assertRaisesRegex(Exception, "loopback"), patch("socket.socket") as socket:
            _loopback_settings(path)
        socket.assert_not_called()

    def test_double_click_selects_from_orders_then_saves_and_waits_without_reqno_prompt(self):
        from vghks_sdk.integrations.smartiris import SmartIrisLaunchResult

        order = ClinicalOrder("0000000", "SYNTHETIC-CASE", "O", "Synthetic image",
                              order_date="2026-01-01", pacs_launch_ref=self.reference)
        self.sdk.orders.get_order_history.return_value = [order]
        client = self.ready_client()
        client.open_study.return_value = SmartIrisLaunchResult(status="HANDOFF_SENT", steps=())
        output = io.StringIO()
        with (patch("vghks_sdk.live.pacs_launch.Path.cwd", return_value=self.root),
              patch("vghks_sdk.live.pacs_launch.sys.stdin.isatty", return_value=True),
              patch.dict("os.environ", {}, clear=True),
              patch("vghks_sdk.live.pacs_launch.VghksSDK", return_value=self.sdk),
              patch("vghks_sdk.integrations.smartiris.SmartIrisClient", return_value=client),
              patch("vghks_sdk.live.pacs_launch.getpass", return_value="SYNTHETIC-PASSWORD"),
              patch("builtins.input", side_effect=["TEST", "0000000", "1", "1", ""]) as prompt,
              patch("sys.stdout", output)):
            code = main([])
        self.assertEqual(code, 0, output.getvalue())
        self.sdk.auth.login.assert_called_once_with()
        self.sdk.orders.prepare_pacs_launch.assert_called_once_with(self.reference)
        client.open_study.assert_called_once()
        self.assertEqual(prompt.call_count, 5)
        self.assertIn("關閉視窗", str(prompt.call_args))
        self.assertNotIn("reqno", " ".join(str(call) for call in prompt.call_args_list))
        summaries = list((self.root / "pacs-launch-results/runs").glob("*/run_summary.json"))
        self.assertEqual(len(summaries), 1)
        saved = json.loads(summaries[0].read_text(encoding="utf-8"))
        self.assertEqual(saved["manual_observation"], "MATCHED")
        self.assertEqual(saved["selection"], "SELECTED")
        self.assertEqual(saved["native_calls"], 1)
        self.assertEqual(len(list((self.root / "pacs-launch-results/archives").glob("*.zip"))), 1)
        for path in summaries[0].parent.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"SYNTHETIC-PASSWORD", path.read_bytes())

    def test_no_native_or_prepare_when_selection_is_cancelled_or_empty(self):
        order = ClinicalOrder("0000000", "CASE", "O", "Synthetic image",
                              pacs_launch_ref=self.reference)
        for orders, expected in (([order], "CANCELLED"), ([], "NO_SAMPLE")):
            with self.subTest(expected=expected):
                self.sdk.reset_mock()
                self.sdk.orders.get_order_history.return_value = orders
                client = Mock()
                summary = run_pacs_launch_test(
                    self.sdk, mrn="0000000", output_dir=self.root / expected,
                    choose=lambda _: "0", open_viewer=True, client_factory=client,
                )
                self.assertEqual(summary["status"], expected)
                self.sdk.orders.prepare_pacs_launch.assert_not_called()
                client.assert_not_called()

    def test_selection_interrupt_saves_discovery_and_never_starts_native(self):
        self.sdk.orders.get_order_history.return_value = [
            ClinicalOrder("0000000", "CASE", "O", "Synthetic image", pacs_launch_ref=self.reference),
        ]
        client = self.ready_client()
        summary = run_pacs_launch_test(
            self.sdk, mrn="0000000", output_dir=self.root,
            choose=Mock(side_effect=KeyboardInterrupt), open_viewer=True, client_factory=client,
        )
        self.assertEqual(summary["status"], "INTERRUPTED")
        self.assertIsNotNone(summary["discovery"])
        self.sdk.orders.prepare_pacs_launch.assert_not_called()
        client.assert_not_called()
        self.assertTrue((self.root / "run_summary.json").is_file())

    def test_noninteractive_missing_selection_fails_before_sdk_creation(self):
        with (patch("vghks_sdk.live.pacs_launch.VghksSDK") as factory,
              patch("sys.stdout", io.StringIO())):
            code = main(["--non-interactive", "--mrn", "0000000", "--output-root", str(self.root)])
        self.assertEqual(code, 2)
        factory.assert_not_called()

    def test_unwritable_output_keeps_interactive_window_visible(self):
        with (patch("vghks_sdk.live.pacs_launch.LiveTestBundleManager.create_auto",
                    side_effect=PermissionError("synthetic output denied")),
              patch("builtins.input", return_value="") as prompt,
              patch("sys.stdout", io.StringIO()) as output):
            code = main(["--interactive"])
        self.assertEqual(code, 2)
        prompt.assert_called_once()
        self.assertNotIn("結果已保存", output.getvalue())

    def test_explicit_reference_keeps_prepare_default_even_in_interactive_terminal(self):
        with (patch("sys.stdout", io.StringIO()) as output,
              patch("vghks_sdk.live.pacs_launch.sys.stdin.isatty", return_value=True)):
            code = main(["--plan", "--request-no", "SYNTHETIC-ORDER"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["native_handoff_calls"], 0)

    def test_incomplete_source_is_not_reported_as_empty(self):
        result = SimpleNamespace(candidates=(), summary={
            "status": "PARTIAL", "pending": 0, "error_count": 0, "gap_count": 1,
            "complete": False,
        })
        with patch("vghks_sdk.live.pacs_launch.PacsLaunchDiscovery") as discovery:
            discovery.return_value.scan_next.return_value = result
            summary = run_pacs_launch_test(self.sdk, mrn="0000000", output_dir=self.root,
                                           choose=lambda _: "0", open_viewer=True)
        self.assertEqual(summary["status"], "DISCOVERY_INCOMPLETE")
        self.sdk.orders.prepare_pacs_launch.assert_not_called()

    def test_packaging_failure_preserves_handoff_evidence_separately(self):
        from vghks_sdk.integrations.smartiris import SmartIrisLaunchResult

        client = self.ready_client()
        client.open_study.return_value = SmartIrisLaunchResult(status="HANDOFF_SENT", steps=())
        with (patch("vghks_sdk.live.pacs_launch.VghksSDK", return_value=self.sdk),
              patch("vghks_sdk.integrations.smartiris.SmartIrisClient", return_value=client),
              patch.dict("os.environ", {"VGHKS_USERNAME": "TEST", "VGHKS_PASSWORD": "SYNTHETIC"}),
              patch("vghks_sdk.live.bundle.LiveTestBundleManager._create_archive",
                    side_effect=PermissionError("synthetic archive denied")),
              patch("sys.stdout", io.StringIO()) as output):
            code = main(["--non-interactive", "--mrn", "0000000", "--request-no", "SYNTHETIC-ORDER",
                         "--open-viewer", "--output-root", str(self.root)])
        self.assertEqual(code, 2)
        files = list((self.root / "runs").glob("*/parsed/pacs_launch_execution.json"))
        self.assertEqual(len(files), 1)
        saved = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(saved["handoff"], "HANDOFF_SENT")
        self.assertEqual(saved["native_calls"], 1)
        client.open_study.assert_called_once()
        self.assertIn("HANDOFF_SENT", output.getvalue())
        self.assertIn("原始結果目錄", output.getvalue())

    def test_persistent_summary_write_failure_does_not_erase_known_native_result(self):
        from vghks_sdk.integrations.smartiris import SmartIrisLaunchResult
        from vghks_sdk.local_io import write_json_atomic

        client = self.ready_client()
        client.open_study.return_value = SmartIrisLaunchResult(status="HANDOFF_SENT", steps=())

        def write(path, value):
            if path.name == "run_summary.json" and value.get("native_result") is not None:
                raise ConfigurationError("synthetic write denied", code="OUTPUT_WRITE_FAILED")
            return write_json_atomic(path, value)

        with patch("vghks_sdk.live.pacs_launch.write_json_atomic", side_effect=write):
            summary = self.run_test(open_viewer=True, client_factory=Mock(return_value=client))
        self.assertEqual(summary["status"], "OUTPUT_WRITE_FAILED")
        self.assertEqual(summary["handoff"], "HANDOFF_SENT")
        self.assertEqual(summary["native_result"]["status"], "HANDOFF_SENT")
        self.assertEqual(summary["persistence_issue"]["code"], "OUTPUT_WRITE_FAILED")
        client.open_study.assert_called_once()

    def test_unavailable_or_unknown_bridge_blocks_prepare_and_native(self):
        for status in ("UNAVAILABLE", "UNKNOWN", "UNSUPPORTED"):
            with self.subTest(status=status):
                self.sdk.reset_mock()
                client = self.ready_client()
                client.check_availability.return_value = SmartIrisAvailability(
                    status=status, installation="UNKNOWN", process="UNKNOWN",
                    listener="UNKNOWN", owner="UNKNOWN", port=15516, issues=("SYNTHETIC_CHECK",),
                )
                result = self.run_test(open_viewer=True, client_factory=Mock(return_value=client))
                self.assertEqual(result["status"], "BRIDGE_UNAVAILABLE" if status == "UNAVAILABLE"
                                 else "BRIDGE_UNCONFIRMED")
                self.assertEqual(result["bridge_availability"]["installation"], "UNKNOWN")
                self.assertEqual(result["native_calls"], 0)
                self.sdk.orders.prepare_pacs_launch.assert_not_called()
                client.open_study.assert_not_called()
                client.close.assert_called_once()

    def test_success_records_availability_and_reuses_checked_client(self):
        from vghks_sdk.integrations.smartiris import SmartIrisLaunchResult

        client = self.ready_client()
        client.open_study.return_value = SmartIrisLaunchResult(status="HANDOFF_SENT", steps=())
        calls = Mock()
        calls.attach_mock(client.check_availability, "check")
        calls.attach_mock(self.sdk.orders.prepare_pacs_launch, "prepare")
        calls.attach_mock(client.open_study, "open")
        summary = self.run_test(open_viewer=True, client_factory=Mock(return_value=client))
        self.assertEqual(summary["bridge_availability"]["status"], "AVAILABLE")
        self.assertEqual([call[0] for call in calls.mock_calls], ["check", "prepare", "open"])

    def test_selection_records_pending_scope_separately_from_success(self):
        from vghks_sdk.live.pacs_launch_discovery import (
            PacsLaunchCandidate,
            PacsLaunchDiscoveryResult,
        )

        candidate = PacsLaunchCandidate(self.reference, "2026-01-01", "", "Synthetic", "", ())
        found = PacsLaunchDiscoveryResult((candidate,), {
            "status": "PARTIAL", "pending": 82, "scanned": 30, "total": 112,
            "error_count": 0, "complete": False,
        })
        with patch("vghks_sdk.live.pacs_launch.PacsLaunchDiscovery") as discovery:
            discovery.return_value.scan_next.return_value = found
            result = run_pacs_launch_test(self.sdk, mrn="0000000", output_dir=self.root,
                                          choose=lambda _: "1", open_viewer=False)
        self.assertEqual(result["status"], "PREPARED")
        self.assertEqual(result["selection_index"], 1)
        self.assertEqual(result["discovery_stop_reason"], "USER_SELECTED_BEFORE_END")
        self.assertEqual(result["discovery"]["pending"], 82)


if __name__ == "__main__":
    unittest.main()
