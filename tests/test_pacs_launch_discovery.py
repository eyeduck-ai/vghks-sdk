"""Synthetic incremental discovery; no hospital, localhost or native execution."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from vghks_sdk.core.errors import AuthenticationError, ConfigurationError, ParseError
from vghks_sdk.live.pacs_launch_discovery import PacsLaunchDiscovery, select_pacs_launch_candidate
from vghks_sdk.local_io import write_json_atomic
from vghks_sdk.models.assets import PacsStudyRef
from vghks_sdk.models.orders import (
    ClinicalOrder,
    OrderDetail,
    OrderDetailRef,
    OrderReport,
    OrderReportRef,
)
from vghks_sdk.models.pacs_launch import PacsLaunchRef

MRN = "0000000"


def order(sequence="1", *, when="2026-10-01", name="Synthetic examination"):
    return ClinicalOrder(MRN, "CASE", "O", name, order_date=when,
                         detail_ref=OrderDetailRef(MRN, "CASE", "O", sequence))


def report_ref(sequence="1", **changes):
    return replace(OrderReportRef(MRN, "CASE", "O", sequence, department="SYNTHETIC-UNIT"),
                   **changes)


class PacsLaunchDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sdk = MagicMock()
        self.progress = []
        self.sdk.orders.get_order_history.return_value = []
        blocker = patch("socket.socket", side_effect=AssertionError("network prohibited"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def discovery(self):
        return PacsLaunchDiscovery(self.sdk, MRN, output_dir=self.root,
                                   on_progress=self.progress.append)

    def history(self, all_rows, departmental=()):
        self.sdk.orders.get_order_history.side_effect = lambda mrn, filter: (
            list(all_rows) if filter.category == "*" else list(departmental))

    def read(self, name):
        return json.loads((self.root / "private/pacs_discovery" / name).read_text(encoding="utf-8"))

    def test_incremental_latest_first_groups_variants_and_caches_refs(self):
        old, new = order("1", when="2020-01-01"), order("2", when="115/10/01")
        native = PacsLaunchRef(MRN, "SYNTHETIC-REQUEST")
        self.history([old, new], [new])
        self.sdk.orders.get_order_detail.side_effect = lambda ref: OrderDetail(
            ref, {"科別": "Synthetic department"}, pacs_launch_refs=(native,) if ref == new.detail_ref else ())
        discovery = self.discovery()
        self.sdk.orders.get_order_history.assert_not_called()
        first = discovery.scan_next(batch_size=1)
        self.assertEqual((first.summary["scanned"], first.summary["pending"], first.summary["total"]), (1, 1, 2))
        self.assertEqual(first.summary["status"], "PARTIAL")
        self.assertEqual(first.summary["lookback_days"], 4000)
        self.assertEqual(first.summary["source_rows"], 3)
        self.assertEqual(first.candidates[0].reference, native)
        self.assertEqual(first.candidates[0].department, "Synthetic department")
        self.assertEqual(len(first.candidates[0].sources), 2)
        self.sdk.orders.get_order_detail.assert_called_once_with(new.detail_ref)
        second = discovery.scan_next(batch_size=1)
        self.assertTrue(second.summary["complete"])
        self.assertEqual(second.summary["status"], "COMPLETE")
        self.assertEqual(self.sdk.orders.get_order_history.call_count, 2)
        self.assertEqual([call.args[1].category for call in self.sdk.orders.get_order_history.call_args_list], ["*", "OR"])
        self.assertTrue(all(call.args[1].lookback_days == 4000
                            for call in self.sdk.orders.get_order_history.call_args_list))
        discovery.scan_next()
        self.assertEqual(self.sdk.orders.get_order_detail.call_count, 2)
        for secret in (MRN, native.request_no, new.name, "Synthetic department"):
            self.assertNotIn(secret, json.dumps(self.progress))
        self.assertEqual(len(self.read("candidates.json")), 1)
        self.assertEqual(self.read("orders/000001.json")["status"], "SCANNED")

    def test_native_button_skips_unneeded_pages_and_preserves_all_refs_on_one_page(self):
        direct = replace(order("1"), pacs_launch_ref=PacsLaunchRef(MRN, "DIRECT"))
        detailed = replace(order("2"), report_ref=report_ref("2"))
        refs = (PacsLaunchRef(MRN, "ONE"), PacsLaunchRef(MRN, "TWO"))
        self.history([direct, detailed])
        self.sdk.orders.get_order_detail.return_value = OrderDetail(detailed.detail_ref, {},
                                                                   report_refs=(detailed.report_ref,),
                                                                   pacs_launch_refs=refs)
        result = self.discovery().scan_next()
        self.assertEqual([candidate.reference.request_no for candidate in result.candidates], ["DIRECT", "ONE", "TWO"])
        self.sdk.orders.get_order_detail.assert_called_once_with(detailed.detail_ref)
        self.sdk.orders.get_order_report.assert_not_called()
        self.sdk.orders.prepare_pacs_launch.assert_not_called()
        self.sdk.orders.get_pacs_study.assert_not_called()
        self.sdk.orders.download_pacs_image.assert_not_called()
        self.sdk.records.get_soap.assert_not_called()
        self.sdk.auth.login.assert_not_called()

    def test_jpeg_and_sequence_are_never_native_candidates(self):
        row = replace(order(), pacs_ref=PacsStudyRef(MRN, "JPEG-ONLY"))
        no_link = replace(order("2", name="Unknown status"), detail_ref=None, status="UNKNOWN")
        self.history([row, no_link])
        self.sdk.orders.get_order_detail.return_value = OrderDetail(
            row.detail_ref, {}, pacs_refs=(PacsStudyRef(MRN, "JPEG-ONLY"),))
        result = self.discovery().scan_next()
        self.assertEqual(result.candidates, ())
        self.assertEqual(result.summary["no_followup_count"], 1)
        self.assertIn("NO_FOLLOWUP_REFERENCE", self.read("orders/000002.json")["notes"])

    def test_explicit_unexecuted_skips_pages_but_native_button_and_unknown_remain_queryable(self):
        pending = replace(order("1"), status="未執行")
        native = replace(order("2"), status="未執行", pacs_launch_ref=PacsLaunchRef(MRN, "NATIVE"))
        unknown = replace(order("3"), status="UNKNOWN")
        self.history([pending, native, unknown])
        self.sdk.orders.get_order_detail.return_value = OrderDetail(unknown.detail_ref, {})
        result = self.discovery().scan_next()
        self.sdk.orders.get_order_detail.assert_called_once_with(unknown.detail_ref)
        self.assertEqual(result.summary["not_executed_count"], 1)
        self.assertIn("SKIPPED_NOT_EXECUTED", self.read("orders/000001.json")["notes"])
        self.assertEqual(result.candidates[0].reference, native.pacs_launch_ref)

    def test_detail_report_refs_are_bound_to_same_order_and_report_partials_are_kept(self):
        row = order()
        valid = report_ref()
        wrongs = (report_ref(mrn="1111111"), report_ref(case_no="OTHER"),
                  report_ref(case_type="A"), report_ref("2"))
        self.history([row])
        self.sdk.orders.get_order_detail.return_value = OrderDetail(row.detail_ref, {},
                                                                   report_refs=(*wrongs, valid))
        self.sdk.orders.get_order_report.return_value = OrderReport(
            valid, {}, report_data_status="PARTIAL", text_extraction_notes=("UNSUPPORTED_TEMPLATE",),
            pacs_launch_refs=(PacsLaunchRef(MRN, "FROM-REPORT"),))
        result = self.discovery().scan_next()
        self.sdk.orders.get_order_report.assert_called_once_with(valid)
        self.assertEqual(result.summary["error_count"], 4)
        self.assertEqual(result.summary["gap_count"], 1)
        self.assertEqual(result.summary["status"], "PARTIAL")
        self.assertFalse(result.summary["complete"])
        self.assertEqual(result.candidates[0].department, valid.department)
        self.assertIs(select_pacs_launch_candidate(result, 1), result.candidates[0])

    def test_cross_patient_order_and_native_refs_and_wrong_response_are_rejected(self):
        first, second, third, fourth = [order(str(n), name=f"Synthetic {n}") for n in range(1, 5)]
        first = replace(first, mrn="1111111", pacs_launch_ref=PacsLaunchRef(MRN, "UNTRUSTED"))
        self.history([first, second, third, fourth])
        self.sdk.orders.get_order_detail.side_effect = [
            OrderDetail(second.detail_ref, {}, pacs_launch_refs=(PacsLaunchRef("1111111", "WRONG-PATIENT"),)),
            OrderDetail(fourth.detail_ref, {}, pacs_launch_refs=(PacsLaunchRef(MRN, "WRONG-RESPONSE"),)),
            OrderDetail(fourth.detail_ref, {}, pacs_launch_refs=(PacsLaunchRef(MRN, "VALID"),)),
        ]
        result = self.discovery().scan_next()
        self.assertEqual([candidate.reference.request_no for candidate in result.candidates], ["VALID"])
        self.assertEqual(result.summary["error_count"], 3)
        self.assertEqual(result.summary["scanned"], 4)
        self.assertEqual(self.sdk.orders.get_order_detail.call_count, 3)

    def test_single_page_error_continues_and_is_cached_without_retry(self):
        first = order(name="First")
        duplicate = replace(first, name="Second")
        good = order("2", name="Third")
        self.history([first, duplicate, good])
        self.sdk.orders.get_order_detail.side_effect = [
            ParseError("sensitive exception payload"),
            OrderDetail(good.detail_ref, {}, pacs_launch_refs=(PacsLaunchRef(MRN, "GOOD"),)),
        ]
        result = self.discovery().scan_next()
        self.assertEqual(self.sdk.orders.get_order_detail.call_count, 2)
        self.assertEqual(result.summary["error_count"], 2)
        self.assertEqual(result.summary["candidate_count"], 1)
        self.assertEqual(result.summary["status"], "PARTIAL")
        self.assertNotIn("sensitive exception payload", json.dumps(self.read("issues.json")))

    def test_authentication_and_keyboard_interrupt_stop_keep_completed_and_block_selection(self):
        for failure, status in [(AuthenticationError("synthetic"), "AUTHENTICATION_FAILED"),
                                (KeyboardInterrupt(), "INTERRUPTED")]:
            with self.subTest(status=status):
                self.sdk.reset_mock()
                rows = [order(str(n), name=f"Synthetic {n}") for n in range(1, 4)]
                self.history(rows)
                self.sdk.orders.get_order_detail.side_effect = [
                    OrderDetail(rows[0].detail_ref, {}, pacs_launch_refs=(PacsLaunchRef(MRN, "FIRST"),)),
                    failure,
                ]
                discovery = self.discovery()
                result = discovery.scan_next()
                self.assertEqual(result.summary["status"], status)
                self.assertEqual(result.summary["pending"], 1)
                self.assertEqual(len(result.candidates), 1)
                self.assertEqual(self.read("summary.json")["status"], status)
                self.assertEqual(self.read("orders/000001.json")["status"], "SCANNED")
                self.assertEqual(self.read("orders/000002.json")["status"], status)
                discovery.scan_next()
                self.assertEqual(self.sdk.orders.get_order_detail.call_count, 2)
                with self.assertRaises(ConfigurationError):
                    select_pacs_launch_candidate(result, 1)

    def test_history_auth_stops_but_other_category_failure_can_continue(self):
        self.sdk.orders.get_order_history.side_effect = AuthenticationError("synthetic")
        result = self.discovery().scan_next()
        self.assertEqual(result.summary["status"], "AUTHENTICATION_FAILED")
        self.sdk.orders.get_order_history.assert_called_once()
        self.sdk.orders.get_order_detail.assert_not_called()
        row = replace(order(), pacs_launch_ref=PacsLaunchRef(MRN, "DEPARTMENTAL"))
        self.sdk.orders.get_order_history.side_effect = [ParseError("synthetic"), [row]]
        result = self.discovery().scan_next()
        self.assertEqual(result.summary["candidate_count"], 1)
        self.assertEqual(result.summary["error_count"], 1)
        self.assertEqual(result.summary["status"], "PARTIAL")

    def test_failed_source_persistence_stops_queries_and_blocks_selection(self):
        direct = replace(order("1"), pacs_launch_ref=PacsLaunchRef(MRN, "COMPLETED"))
        other = replace(order("2"), report_ref=report_ref("2"))
        later = order("3")
        self.history([direct, other, later])
        self.sdk.orders.get_order_detail.return_value = OrderDetail(other.detail_ref, {})

        def fail_page(path, value):
            if path.name == "000001.json" and path.parent.name == "pages":
                raise ConfigurationError("synthetic output failure", code="OUTPUT_WRITE_FAILED")
            return write_json_atomic(path, value)

        discovery = self.discovery()
        with patch("vghks_sdk.live.pacs_launch_discovery.write_json_atomic", side_effect=fail_page):
            result = discovery.scan_next()
        self.assertEqual(result.summary["status"], "ERROR")
        self.assertEqual(len(result.candidates), 1)
        self.sdk.orders.get_order_detail.assert_called_once_with(other.detail_ref)
        self.sdk.orders.get_order_report.assert_not_called()
        with self.assertRaises(ConfigurationError):
            select_pacs_launch_candidate(result, 1)
        discovery.scan_next()
        self.assertEqual(self.sdk.orders.get_order_detail.call_count, 1)

    def test_progress_interrupt_after_history_prevents_all_detail_queries(self):
        self.history([order()])
        discovery = PacsLaunchDiscovery(self.sdk, MRN, output_dir=self.root,
                                       on_progress=MagicMock(side_effect=KeyboardInterrupt()))
        result = discovery.scan_next()
        self.assertEqual(result.summary["status"], "INTERRUPTED")
        self.sdk.orders.get_order_detail.assert_not_called()
        self.assertEqual(self.read("summary.json")["status"], "INTERRUPTED")

    def test_empty_history_and_validation_and_exact_candidate_selection(self):
        result = self.discovery().scan_next()
        self.assertTrue(result.summary["complete"])
        self.assertEqual(result.summary["candidate_count"], 0)
        with self.assertRaises(ConfigurationError):
            PacsLaunchDiscovery(self.sdk, "../invalid", output_dir=self.root)
        for batch_size in (0, -1, True, "30"):
            with self.subTest(batch_size=batch_size), self.assertRaises(ConfigurationError):
                self.discovery().scan_next(batch_size)
        native = PacsLaunchRef(MRN, "SELECTED")
        self.history([replace(order(), pacs_launch_ref=native)])
        result = self.discovery().scan_next()
        for index in (0, -1, 2, True, "SELECTED", native):
            with self.subTest(index=index), self.assertRaises(ConfigurationError):
                select_pacs_launch_candidate(result, index)
        self.assertIs(select_pacs_launch_candidate(result, 1).reference, native)


if __name__ == "__main__":
    unittest.main()
