from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from test_raw_capture import PreparedFakeSession

from vghks_sdk import RequestPolicy
from vghks_sdk.core.config import PortalCredentials
from vghks_sdk.core.errors import ConfigurationError
from vghks_sdk.core.transport import SafeSessionTransport
from vghks_sdk.live.bundle import (
    INCOMPLETE_MARKER,
    LiveTestBundleManager,
    pack_incomplete_run,
)
from vghks_sdk.live.capture import RawCaptureRecorder
from vghks_sdk.live.config import (
    LiveTestConfig,
    load_live_test_config,
    resolve_live_test_config,
)
from vghks_sdk.live.runner import execute_live_test, package_bootstrap_failure
from vghks_sdk.live_test_app import (
    build_parser,
    run_live_test_namespace,
    run_self_check,
)
from vghks_sdk.offline.bundle import BundleReader


class LiveConfigTests(unittest.TestCase):
    def test_live_fallback_default_and_explicit_opt_out_roundtrip(self):
        self.assertTrue(LiveTestConfig(profile="comprehensive").allow_unverified_tls)
        config = resolve_live_test_config(
            json_values={"profile": "comprehensive", "allow_unverified_tls": False}, environ={}
        )
        restored = resolve_live_test_config(json_values=config.to_safe_dict(), environ={})
        self.assertFalse(restored.allow_unverified_tls)
        for invalid in ("false", 0, None):
            with self.subTest(invalid=invalid), self.assertRaises(ConfigurationError):
                resolve_live_test_config(json_values={"allow_unverified_tls": invalid}, environ={})

    def test_precedence_is_cli_json_environment_defaults(self) -> None:
        config = resolve_live_test_config(
            cli_values={
                "profile": "full",
                "doctor_card": "D001",
                "opd_date": date(2026, 7, 1),
                "request_policy": {"max_attempts": 1},
            },
            json_values={
                "profile": "core",
                "visit_filter": {"section_codes": ["70"], "section_name_contains": []},
                "request_policy": {"max_attempts": 2, "read_timeout_seconds": 22},
            },
            environ={"VGHKS_LIVE_PROFILE": "core", "VGHKS_DELAY_MIN": "0.8"},
        )
        self.assertEqual(config.profile, "full")
        self.assertEqual(config.visit_filter.section_codes, ("70",))
        self.assertEqual(config.request_policy.max_attempts, 1)
        self.assertEqual(config.request_policy.read_timeout_seconds, 22)
        self.assertEqual(config.request_policy.min_delay_seconds, 0.8)
        self.assertEqual(config.range_start, date(2026, 7, 1))
        config.validate_for_execution()

    def test_json_rejects_credentials_unknown_fields_and_resolves_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bad = root / "bad.json"
            bad.write_text(
                json.dumps({"schema_version": 1, "nested": {"password": "SECRET"}}),
                encoding="utf-8",
            )
            with self.assertRaises(ConfigurationError):
                load_live_test_config(bad)

            good = root / "good.json"
            good.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "output_root": "results",
                        "ca_bundle": "internal.pem",
                    }
                ),
                encoding="utf-8",
            )
            loaded = load_live_test_config(good)
            self.assertEqual(Path(loaded["output_root"]), root / "results")
            self.assertEqual(Path(loaded["ca_bundle"]), root / "internal.pem")

    def test_full_and_optional_profiles_validate_before_execution(self) -> None:
        self.assertEqual(
            LiveTestConfig(profile="full").validate_for_execution().profile,
            "full",
        )
        with self.assertRaises(ConfigurationError):
            LiveTestConfig(include_surgery=True).validate_for_execution()

    def test_zero_argument_wizard_uses_quick_full_defaults(self) -> None:
        args = build_parser().parse_args([])
        answers = iter(("TEST-USER",))
        with (
            patch("builtins.input", side_effect=lambda *_: next(answers)),
            patch("getpass.getpass", return_value="TEST-PASSWORD"),
            patch.dict("os.environ", {"VGHKS_TEST_MRN": "0000000"}, clear=True),
            patch(
                "vghks_sdk.live_test_app.create_live_test_bundle",
                return_value=MagicMock(),
            ),
            patch(
                "vghks_sdk.live_test_app.execute_live_test",
                return_value=SimpleNamespace(exit_code=0),
            ) as execute,
        ):
            self.assertEqual(
                run_live_test_namespace(args, force_interactive=True),
                0,
            )
        config = execute.call_args.args[0]
        credentials = execute.call_args.args[1]
        self.assertEqual(config.profile, "full")
        self.assertEqual(config.visit_filter.section_name_contains, ("眼科",))
        self.assertIsNone(config.soap_search)
        self.assertTrue(config.download_assets)
        self.assertEqual(config.asset_terms, ("Microsonography", "DBR"))
        self.assertEqual(credentials.username, "TEST-USER")

    def test_default_live_config_is_full(self) -> None:
        config = resolve_live_test_config(environ={})
        self.assertEqual(config.profile, "full")

    def test_environment_credentials_skip_username_and_password_prompts(self) -> None:
        args = build_parser().parse_args([])
        with (
            patch("builtins.input", return_value="YES") as prompt,
            patch("getpass.getpass") as password_prompt,
            patch.dict(
                "os.environ",
                {"VGHKS_TEST_MRN": "0000000", "VGHKS_USERNAME": "TEST-USER", "VGHKS_PASSWORD": "TEST-PASSWORD"},
                clear=True,
            ),
            patch(
                "vghks_sdk.live_test_app.create_live_test_bundle",
                return_value=MagicMock(),
            ),
            patch(
                "vghks_sdk.live_test_app.execute_live_test",
                return_value=SimpleNamespace(exit_code=0),
            ) as execute,
        ):
            self.assertEqual(run_live_test_namespace(args, force_interactive=True), 0)
        prompt.assert_not_called()
        password_prompt.assert_not_called()
        credentials = execute.call_args.args[1]
        self.assertEqual(credentials.username, "TEST-USER")


class BundleTests(unittest.TestCase):
    def test_archive_in_exe_parent_keeps_all_captures_and_uses_export_time(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            exe_directory = Path(temp_dir)
            manager = LiveTestBundleManager.create_auto(
                exe_directory / "live-test-results",
                run_id="19990101-000000-old-start",
                archive_directory=exe_directory,
            )
            RawCaptureRecorder(manager.run_directory, prepare_root=False).close()
            manager.write_config(LiveTestConfig().to_safe_dict())
            (manager.responses_directory / "sample.html").write_text(
                "synthetic response", encoding="utf-8"
            )
            (manager.parsed_directory / "sample.json").write_text(
                '{"sample": true}', encoding="utf-8"
            )
            with patch("vghks_sdk.live.bundle.datetime") as clock:
                clock.now.return_value = datetime(2026, 9, 19, 15, 30, 45)
                archive = manager.finalize(
                    status="OK", summary={"schema_version": 6, "status": "OK"}
                )
            self.assertEqual(archive.archive_path.parent, exe_directory)
            self.assertFalse(list(exe_directory.glob("*.sha256")))
            self.assertRegex(
                archive.archive_path.name, r"^vghks-live-test-20260919-153045-[0-9a-f]{8}-OK\.zip$"
            )
            with BundleReader(archive.archive_path) as reader:
                self.assertEqual(reader.read("responses/sample.html"), b"synthetic response")
                self.assertTrue(reader.json("parsed/sample.json")["sample"])
                self.assertNotIn("files_manifest.json", reader.names)

    def test_bootstrap_failure_zip_also_goes_next_to_exe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            exe_directory = Path(temp_dir)
            execution = package_bootstrap_failure(
                ConfigurationError("synthetic setup error"), executable_directory=exe_directory
            )
            self.assertEqual(execution.status, "BOOTSTRAP_FAILED")
            self.assertEqual(execution.archive.archive_path.parent, exe_directory)
            with BundleReader(execution.archive.archive_path) as reader:
                self.assertEqual(reader.json("run_summary.json")["status"], "BOOTSTRAP_FAILED")

    def test_incomplete_recovery_can_write_zip_next_to_exe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            exe_directory = Path(temp_dir)
            manager = LiveTestBundleManager.create_auto(
                exe_directory / "live-test-results", archive_directory=exe_directory
            )
            manager.close_console()
            with patch("requests.sessions.Session.request") as network:
                archive = pack_incomplete_run(
                    manager.run_directory, archive_directory=exe_directory
                )
            network.assert_not_called()
            self.assertEqual(archive.archive_path.parent, exe_directory)
            with BundleReader(archive.archive_path) as reader:
                self.assertEqual(reader.json("run_summary.json")["status"], "INTERRUPTED")

    def test_bundle_is_one_plain_zip_without_checksum_files_or_hash_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = LiveTestBundleManager(root / "run", run_id="unit-run")
            manager.write_config(LiveTestConfig().to_safe_dict())
            (manager.parsed_directory / "value.json").write_text(
                '{"value":"完整資料"}\n', encoding="utf-8"
            )
            archive = manager.finalize(
                status="OK",
                summary={"schema_version": 4, "run_id": "unit-run", "status": "OK"},
            )
            self.assertTrue(archive.archive_path.is_file())
            self.assertFalse(list(root.rglob("*.sha256")))
            with zipfile.ZipFile(archive.archive_path) as zipped:
                names = set(zipped.namelist())
                self.assertTrue(all(not item.flag_bits & 1 for item in zipped.infolist()))
                self.assertEqual(
                    json.loads(zipped.read("parsed/value.json")), {"value": "完整資料"}
                )
            self.assertIn("run_summary.json", names)
            self.assertIn("README_RETURN.txt", names)
            self.assertNotIn("files_manifest.json", names)
            self.assertFalse(any(name.endswith(".zip") for name in names))

    def test_incomplete_run_can_be_packed_offline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = LiveTestBundleManager(Path(temp_dir) / "runs" / "interrupted")
            run_directory = manager.run_directory
            manager.log("partial", echo=False)
            manager.close_console()
            self.assertTrue((run_directory / INCOMPLETE_MARKER).is_file())
            with patch("requests.sessions.Session.request") as network:
                archive = pack_incomplete_run(run_directory)
            network.assert_not_called()
            self.assertTrue(archive.archive_path.is_file())
            summary = json.loads((run_directory / "run_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["status"], "INTERRUPTED")

    def test_raw_capture_schema_three_maps_step_operation_and_retry_group(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "run"
            recorder = RawCaptureRecorder(root, run_id="mapped-run")
            recorder.set_live_step("latest_soap")
            operation_id = recorder.start_operation(name="get_soap", app_key="prq")
            transport = SafeSessionTransport(
                policy=RequestPolicy(min_delay_seconds=0, max_delay_seconds=0, max_attempts=1),
                session=PreparedFakeSession(body=b"SOAP"),  # type: ignore[arg-type]
                sleeper=lambda _: None,
                raw_capture=recorder,
            )
            transport.request("GET", "https://example.test/soap")
            recorder.finish_operation(operation_id)
            recorder.close()
            row = json.loads((root / "capture_manifest.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(row["schema_version"], 3)
            self.assertEqual(row["run_id"], "mapped-run")
            self.assertEqual(row["live_test_step"], "latest_soap")
            self.assertEqual(row["sdk_operation"], "get_soap")
            self.assertEqual(row["app_key"], "prq")
            self.assertTrue(row["request_group_id"].startswith("req-"))

    def test_self_check_is_offline(self) -> None:
        with patch("requests.sessions.Session.request") as network:
            self.assertEqual(run_self_check(), 0)
        network.assert_not_called()

    def test_bootstrap_failure_still_returns_summary_diagnostics_capture_and_zip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = LiveTestConfig(output_root=Path(temp_dir))
            with (
                patch.object(
                    LiveTestConfig,
                    "build_settings",
                    side_effect=ConfigurationError("bad CA"),
                ),
                patch("vghks_sdk.live.runner.VghksSDK") as sdk_factory,
            ):
                execution = execute_live_test(
                    config,
                    PortalCredentials("TEST", "PASSWORD"),
                )
            sdk_factory.assert_not_called()
            self.assertEqual(execution.status, "BOOTSTRAP_FAILED")
            self.assertEqual(execution.exit_code, 2)
            self.assertIsNotNone(execution.archive)
            self.assertTrue((execution.run_directory / "capture_manifest.jsonl").is_file())
            self.assertTrue((execution.run_directory / "diagnostics" / "summary.json").is_file())
            summary = json.loads(
                (execution.run_directory / "run_summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["status"], "BOOTSTRAP_FAILED")


if __name__ == "__main__":
    unittest.main()
