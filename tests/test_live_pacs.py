"""Synthetic sampling, partial failures and evidence for both PACS modes."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from test_pacs_img_url import image_url

from vghks_sdk.core.config import PortalCredentials
from vghks_sdk.core.errors import ConfigurationError, ParseError
from vghks_sdk.core.readiness import make_auth_report, resolve_auth_targets
from vghks_sdk.live.atomic import build_test_plan, run_atomic_test
from vghks_sdk.live.bundle import LiveTestBundleManager, pack_incomplete_run
from vghks_sdk.live.config import LiveTestConfig, resolve_live_test_config
from vghks_sdk.live.defaults import SYNTHETIC_MRN
from vghks_sdk.live.pacs import select_pacs_orders
from vghks_sdk.live.pacs_compare import execute_pacs_comparison
from vghks_sdk.live.runner import LiveTestExecution
from vghks_sdk.live_test_app import build_parser, main, run_live_test_namespace
from vghks_sdk.models import (
    AuthCheckTarget,
    BinaryAsset,
    ClinicalOrder,
    OrderReport,
    OrderReportRef,
    PacsImageRef,
    PacsStudy,
    PacsStudyRef,
)
from vghks_sdk.offline.retest import build_retest_config

MRN = SYNTHETIC_MRN
STUDY = PacsStudyRef(MRN, "SYNTHETIC-PACS")
IMG = PacsImageRef(MRN, STUDY.request_no, img_url=image_url())
UID = PacsImageRef(MRN, STUDY.request_no, "SERIES", "STUDY", "1.2.3")


def order(sequence="1", **values):
    return ClinicalOrder(MRN, "CASE", "O", "Synthetic CT examination", "2026-09-21",
                         status="完成", report_ref=OrderReportRef(MRN, "CASE", "O", sequence), **values)


def ready(*, only):
    return make_auth_report(AuthCheckTarget(s.key, (), "", False, 1, 0, s.dependencies, "OK")
                            for s in resolve_auth_targets(only))


def fake_sdk(images=(IMG, UID)):
    return SimpleNamespace(
        auth=SimpleNamespace(check=Mock(side_effect=ready)),
        orders=SimpleNamespace(
            get_order_history=Mock(return_value=[order()]),
            get_order_report=Mock(side_effect=lambda ref: OrderReport(
                ref, {}, pacs_refs=(STUDY,), report_text="Synthetic text", report_data_status="TEXT_AVAILABLE")),
            get_pacs_study=Mock(return_value=PacsStudy(STUDY, images)),
            download_pacs_image=Mock(return_value=BinaryAsset(b"\xff\xd8synthetic\xff\xd9", "image/jpeg")),
        ),
    )


class PacsLiveTests(unittest.TestCase):
    def run_profile(self, sdk, **values):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        with redirect_stdout(io.StringIO()):
            result = run_atomic_test(sdk, LiveTestConfig(profile="pacs", **values), output_dir=root)
        evidence = json.loads((root / "parsed/workflows/ophthalmology_orders/pacs_verification.json")
                              .read_text(encoding="utf-8"))
        return result, evidence, root

    def test_plan_configuration_and_retest_keep_the_bounded_patient_scope(self):
        config = LiveTestConfig(profile="pacs")
        self.assertEqual(config.max_items, 8)
        self.assertEqual(config.login_negative_attempts, 0)
        self.assertFalse(config.weekly_opd_soap)
        self.assertEqual(resolve_live_test_config(json_values=config.to_safe_dict(), environ={}), config)
        plan = build_test_plan(config)
        self.assertEqual(plan["auth_targets"], ["portal", "prq"])
        self.assertEqual({row["key"] for row in plan["operations"]}, {
            "prq.order_history", "prq.order_detail", "prq.order_report", "prq.pacs_study",
            "prq.pdf_attachment", "prq.pacs_image",
        })
        self.assertEqual(plan["pacs_verification"]["modes"], ["imgUrl", "uid"])
        self.assertTrue(plan["pacs_verification"]["all_images_of_selected_studies"])
        self.assertFalse(plan["ophthalmology_orders"]["case_discovery"])
        self.assertIn("attendance.punch", plan["excluded_write_operations"])
        recipe = build_retest_config(config.to_safe_dict(), [], None, "COMPLETED_WITH_GAPS")
        self.assertEqual(recipe["profile"], "pacs")
        self.assertTrue(recipe["pacs_single_account"])
        self.assertEqual(recipe["max_items"], 8)
        for options in ({"login_negative_attempts": 1}, {"include_surgery": True}):
            with self.subTest(options=options), self.assertRaises(ConfigurationError):
                LiveTestConfig(profile="pacs", **options)
        with self.assertRaises(ConfigurationError):
            LiveTestConfig(profile="pacs", pacs_single_account="true")

    def test_selection_prefers_old_image_references_and_preserves_variants_without_spending_budget_on_unexecuted(self):
        old = replace(order("OLD", pacs_ref=STUDY), order_date="2017-01-01")
        skipped = replace(order("SKIP", pacs_ref=STUDY), status="未執行")
        labelled = order("LABEL", attachment="JPG")
        selected, evidence = select_pacs_orders(
            {"all": [order(), skipped, old, labelled], "departmental": [old]}, terms=("DBR",), limit=1)
        self.assertEqual(selected, {"all": [old], "departmental": [old]})
        self.assertEqual(evidence["selected_orders"], 1)
        self.assertEqual(evidence["discovered_orders"], 4)
        self.assertEqual(evidence["orders"][0]["reason"], "PACS_REFERENCE")
        self.assertTrue(any(row["reason"] == "NOT_EXECUTED" for row in evidence["orders"]))

    def test_both_modes_download_all_images_once_and_preserve_text_and_files(self):
        images = tuple(PacsImageRef(MRN, STUDY.request_no, img_url=image_url(f"1.2.3.{n}")) for n in range(59))
        sdk = fake_sdk((*images, UID))
        result, evidence, root = self.run_profile(sdk)
        self.assertEqual(result.status, "OK")
        self.assertEqual(evidence["modes"]["imgUrl"]["references"], 59)
        self.assertEqual(evidence["modes"]["imgUrl"]["downloaded_files"], 59)
        self.assertEqual(evidence["modes"]["uid"]["downloaded_files"], 1)
        self.assertEqual(len(list(root.rglob("*.jpg"))), 60)
        self.assertEqual(sdk.orders.get_order_history.call_count, 2)
        sdk.orders.get_order_report.assert_called_once()
        sdk.orders.get_pacs_study.assert_called_once_with(STUDY)
        self.assertEqual(sdk.orders.download_pacs_image.call_count, 60)

    def test_failed_image_does_not_stop_other_images_or_other_mode(self):
        other = PacsImageRef(MRN, STUDY.request_no, img_url=image_url("1.2.3.4"))
        sdk = fake_sdk((IMG, other, UID))

        def download(ref):
            if ref == IMG:
                raise ParseError("synthetic invalid image", code="JPG_BINARY_INVALID")
            return BinaryAsset(b"\xff\xd8synthetic\xff\xd9", "image/jpeg")

        sdk.orders.download_pacs_image.side_effect = download
        result, evidence, _ = self.run_profile(sdk)
        self.assertEqual(result.status, "COMPLETED_WITH_ERRORS")
        self.assertEqual(evidence["modes"]["imgUrl"]["status"], "ERROR")
        self.assertEqual(evidence["modes"]["imgUrl"]["download_errors"], 1)
        self.assertEqual(evidence["modes"]["imgUrl"]["downloaded_files"], 1)
        self.assertEqual(evidence["modes"]["uid"]["status"], "OK")
        self.assertEqual(sdk.orders.download_pacs_image.call_count, 3)

    def test_absent_mode_is_no_sample_and_never_reported_as_pass(self):
        for images, present, missing in (((IMG,), "imgUrl", "uid"), ((UID,), "uid", "imgUrl")):
            with self.subTest(present=present):
                result, evidence, _ = self.run_profile(fake_sdk(images))
                self.assertEqual(result.status, "COMPLETED_WITH_GAPS")
                self.assertEqual(evidence["modes"][present]["status"], "OK")
                self.assertEqual(evidence["modes"][missing]["status"], "NO_SAMPLE")
                self.assertEqual(evidence["modes"][missing]["downloaded_files"], 0)

    def test_upstream_error_blocks_sampling_without_hiding_original_error(self):
        sdk = fake_sdk()
        sdk.orders.get_order_history.side_effect = ParseError("synthetic history failure", code="HISTORY_FAILED")
        result, evidence, _ = self.run_profile(sdk)
        self.assertEqual(result.status, "COMPLETED_WITH_ERRORS")
        self.assertEqual({row["status"] for row in evidence["modes"].values()}, {"BLOCKED"})
        self.assertTrue(any(step.issue and step.issue.code == "HISTORY_FAILED" for step in result.steps))
        sdk.orders.download_pacs_image.assert_not_called()

    def test_reference_only_run_does_not_claim_download_success(self):
        sdk = fake_sdk()
        _, evidence, _ = self.run_profile(sdk, download_assets=False)
        self.assertEqual({row["status"] for row in evidence["modes"].values()}, {"SKIPPED"})
        sdk.orders.download_pacs_image.assert_not_called()

    def test_built_profile_is_selected_for_double_click(self):
        with (patch("vghks_sdk.live_test_app.build_identity", return_value={"default_profile": "pacs"}),
              patch("vghks_sdk.live_test_app.run_live_test_namespace", return_value=0) as execute,
              patch("builtins.input", return_value="")):
            self.assertEqual(main([]), 0)
        self.assertEqual(execute.call_args.args[0].profile, "pacs")


class PacsComparisonTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manager = LiveTestBundleManager(
            self.root / "union-run", archive_directory=self.root, archive_label="union",
        )
        self.addCleanup(self.manager.close_console)
        self.credentials = (PortalCredentials("UNION-SYNTHETIC", "UNION-SECRET"),
                            PortalCredentials("VGHKS-SYNTHETIC", "VGHKS-SECRET"))
        self.config = LiveTestConfig(profile="pacs", test_mrn="0000001")
        for target in ("socket.create_connection", "socket.socket.connect", "socket.getaddrinfo"):
            patcher = patch(target, side_effect=AssertionError("network disabled"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_comparison(self, *, first_error=False, no_uid=False, partial_text=False):
        sdks = []
        scope = []

        def make_sdk(**kwargs):
            index = len(sdks)
            mrn = ("0000001", "0000002")[index]
            study = PacsStudyRef(mrn, "SYNTHETIC-PACS")
            images = ((PacsImageRef(mrn, study.request_no, img_url=image_url()),) if index == 0 else
                      () if no_uid else (PacsImageRef(mrn, study.request_no, "SERIES", "STUDY", "1.2.3"),))
            sdk = fake_sdk(images)
            ref = OrderReportRef(mrn, "CASE", "O", "1")
            row = replace(order(), mrn=mrn, report_ref=ref)

            expected_mrn = mrn

            def history(mrn, *_args, **_kwargs):
                self.assertEqual(mrn, expected_mrn)
                scope.append(index)
                if index == 0 and first_error:
                    raise ParseError("synthetic history failure", code="HISTORY_FAILED")
                return [row]

            sdk.orders.get_order_history.side_effect = history
            sdk.orders.get_order_report.side_effect = lambda actual_ref: OrderReport(
                actual_ref, {}, pacs_refs=(study,), report_data_status="ATTACHMENT_ONLY",
                text_extraction_notes=("JS_DOCUMENT_WRITE_UNSUPPORTED",) if partial_text and index == 0 else (),
            )
            sdk.orders.get_pacs_study.return_value = PacsStudy(
                study, images, data_status="IMAGES_AVAILABLE" if images else "EMPTY",
                empty_reason="" if images else "NO_IMAGES")
            sdks.append((sdk, kwargs))
            context = MagicMock()
            context.__enter__.return_value = sdk
            return context

        with (patch("vghks_sdk.live.runner.VghksSDK", side_effect=make_sdk),
              patch("vghks_sdk.live.ophthalmology.run_network_checks"),
              redirect_stdout(io.StringIO())):
            result = execute_pacs_comparison(
                self.config, *self.credentials, vghks_mrn="0000002",
                bundle_manager=self.manager, executable_directory=self.root,
            )
        return result, json.loads(result.report_path.read_text(encoding="utf-8")), sdks, scope

    def test_sequential_accounts_keep_separate_mrns_sessions_bundles_and_expected_modes(self):
        result, report, sdks, scope = self.run_comparison()
        self.assertEqual((result.status, result.exit_code), ("OK", 0))
        self.assertEqual(scope, [0, 0, 1, 1])
        self.assertIsNot(sdks[0][0], sdks[1][0])
        self.assertEqual([item[1]["credentials"] for item in sdks], list(self.credentials))
        for index, (execution, hospital, mode, missing) in enumerate(zip(
                result.executions, ("union", "vghks"), ("imgUrl", "uid"), ("uid", "imgUrl")), 1):
            self.assertIn(f"-{hospital}-", execution.archive.archive_path.name)
            row = report["accounts"][index - 1]
            self.assertEqual(row["expected_mode_status"], "OK")
            self.assertEqual(row["modes"][missing]["status"], "NO_SAMPLE")
            with zipfile.ZipFile(execution.archive.archive_path) as bundle:
                self.assertEqual(json.loads(bundle.read("pacs_account.json"))["hospital"], hospital)
                self.assertEqual(json.loads(bundle.read("run_summary.json"))["pacs_account"]["expected_mode"], mode)
                config = bundle.read("run_config.json").decode()
                diagnostics = "".join(bundle.read(name).decode() for name in bundle.namelist()
                                      if name.startswith("diagnostics/") and not name.endswith("/"))
                for credentials in self.credentials:
                    self.assertNotIn(credentials.password, config + diagnostics + json.dumps(report))

    def test_union_failure_does_not_stop_vghks_or_hide_first_error(self):
        result, report, sdks, scope = self.run_comparison(first_error=True)
        self.assertEqual((result.status, result.exit_code), ("COMPLETED_WITH_ERRORS", 1))
        self.assertEqual(scope, [0, 0, 1, 1])
        self.assertEqual(report["accounts"][0]["expected_mode_status"], "BLOCKED")
        self.assertEqual(report["accounts"][1]["expected_mode_status"], "OK")
        self.assertTrue(any(step.error_code == "HISTORY_FAILED" for step in result.executions[0].result.steps))
        sdks[1][0].orders.download_pacs_image.assert_called_once()

    def test_missing_vghks_uid_stays_unverified(self):
        result, report, _, _ = self.run_comparison(no_uid=True)
        self.assertEqual((result.status, result.exit_code), ("COMPLETED_WITH_GAPS", 0))
        self.assertEqual(report["accounts"][1]["expected_mode_status"], "NO_SAMPLE")

    def test_successful_images_do_not_hide_partial_report_text(self):
        result, report, _, _ = self.run_comparison(partial_text=True)
        self.assertEqual((result.status, result.exit_code), ("COMPLETED_WITH_GAPS", 0))
        self.assertEqual(report["accounts"][0]["expected_mode_status"], "OK")
        self.assertEqual(report["accounts"][0]["partial_report_steps"], 1)

    def test_interrupt_preserves_completed_account_and_does_not_login_to_second(self):
        interrupted = LiveTestExecution("INTERRUPTED", 130, self.manager.run_directory, None, None)
        with (patch("vghks_sdk.live.pacs_compare.execute_live_test", return_value=interrupted) as execute,
              redirect_stdout(io.StringIO())):
            result = execute_pacs_comparison(self.config, *self.credentials, vghks_mrn="0000002",
                                             bundle_manager=self.manager, executable_directory=self.root)
        self.assertEqual(result.exit_code, 130)
        execute.assert_called_once()
        report = json.loads(result.report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["accounts"][1]["status"], "NOT_RUN")

    def test_console_collects_all_inputs_before_running_either_account(self):
        args = build_parser().parse_args(["--profile", "pacs"])
        prompts = []

        def password(prompt):
            prompts.append(prompt)
            execute.assert_not_called()
            return ("UNION-SECRET", "VGHKS-SECRET")[len(prompts) - 1]

        with (patch.dict(os.environ, {}, clear=True),
              patch("builtins.input", side_effect=["0000001", "UNION-SYNTHETIC", "0000002", "VGHKS-SYNTHETIC"]),
              patch("vghks_sdk.live_test_app.getpass.getpass", side_effect=password),
              patch("vghks_sdk.live_test_app._offer_incomplete_packaging"),
              patch("vghks_sdk.live_test_app.create_live_test_bundle", return_value=self.manager),
              patch("vghks_sdk.live.pacs_compare.execute_pacs_comparison", return_value=SimpleNamespace(exit_code=0)) as execute,
              redirect_stdout(io.StringIO())):
            self.assertEqual(run_live_test_namespace(args, force_interactive=True), 0)
        self.assertEqual(len(prompts), 2)
        self.assertEqual(execute.call_args.args[0].test_mrn, "0000001")
        self.assertEqual(execute.call_args.args[1:3], self.credentials)
        self.assertEqual(execute.call_args.kwargs["vghks_mrn"], "0000002")

    def test_missing_second_password_packages_bootstrap_without_starting_first(self):
        args = build_parser().parse_args(["--profile", "pacs", "--test-mrn", "0000001",
                                          "--comparison-mrn", "0000002", "--non-interactive"])
        with (patch.dict(os.environ, {"VGHKS_USERNAME": "UNION", "VGHKS_PASSWORD": "SECRET",
                                      "VGHKS_COMPARISON_USERNAME": "VGHKS"}, clear=True),
              patch("vghks_sdk.live_test_app.create_live_test_bundle", return_value=self.manager),
              patch("vghks_sdk.live.pacs_compare.execute_pacs_comparison") as execute,
              redirect_stdout(io.StringIO())):
            self.assertEqual(run_live_test_namespace(args), 2)
        execute.assert_not_called()
        summary = json.loads((self.manager.run_directory / "run_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["status"], "BOOTSTRAP_FAILED")

    def test_recovered_incomplete_bundle_retains_hospital_filename(self):
        self.manager.close_console()
        archive = pack_incomplete_run(self.manager.run_directory, archive_directory=self.root)
        self.assertIn("-union-", archive.archive_path.name)

    def test_single_account_retest_config_does_not_request_second_credentials(self):
        recipe = build_retest_config(self.config.to_safe_dict(), [], None, "COMPLETED_WITH_GAPS")
        recipe["test_mrn"] = "0000001"
        path = self.root / "retest.json"
        path.write_text(json.dumps(recipe), encoding="utf-8")
        args = build_parser().parse_args(["--config", str(path), "--non-interactive"])
        with (patch.dict(os.environ, {"VGHKS_USERNAME": "SYNTHETIC", "VGHKS_PASSWORD": "SYNTHETIC-ONLY"}, clear=True),
              patch("vghks_sdk.live_test_app.create_live_test_bundle", return_value=self.manager),
              patch("vghks_sdk.live_test_app.execute_live_test", return_value=SimpleNamespace(exit_code=0)) as single,
              patch("vghks_sdk.live.pacs_compare.execute_pacs_comparison") as dual,
              redirect_stdout(io.StringIO())):
            self.assertEqual(run_live_test_namespace(args), 0)
        single.assert_called_once()
        dual.assert_not_called()
        self.assertTrue(single.call_args.args[0].pacs_single_account)


if __name__ == "__main__":
    unittest.main()
