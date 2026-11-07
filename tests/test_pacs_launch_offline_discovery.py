"""Synthetic menu evidence integrity and pending-vs-error distinctions."""

from __future__ import annotations

import json
import unittest
from unittest.mock import Mock
from urllib.parse import urlencode

from tests import test_pacs_launch_offline as fixtures
from tests.test_pacs_launch_parsing import MRN, REQUEST_NO
from vghks_sdk.live.pacs_launch_discovery import PacsLaunchDiscovery
from vghks_sdk.models import to_jsonable
from vghks_sdk.parsing.clinical import parse_clinical_orders, parse_order_detail


class PacsLaunchOfflineDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PacsLaunchOfflineTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.make_bundle()
        self.root = self.fixture.input
        self.write = self.fixture.write_json
        self.read = self.fixture.read_json
        self.serial = 100000

    def record(self, method, path, parameters, body):
        self.serial += 1
        name = f"responses/{self.serial}.html"
        data = body.encode("utf-8")
        (self.root / name).write_bytes(data)
        request = {"method": method, "url": "https://example.invalid" + path}
        if method == "GET":
            request["url"] += "?" + urlencode(parameters)
        else:
            request["body"] = {"value": parameters}
        row = {"capture_id": str(self.serial), "kind": "HTTP_EXCHANGE", "request": request,
               "response_file": name, "response": {"status_code": 200, "body_length": len(data),
                                                       "headers": [["Content-Type", "text/html; charset=utf-8"]]}}
        with (self.root / "capture_manifest.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")

    def menu(self, *, batch=30):
        sdk = Mock()
        body = "<script>var aryCase=[];</script>"
        for number in (1, 2):
            query = urlencode({"hhisnum": MRN, "caseType": "O", "caseNo": "CASE", "seqNo": str(number)})
            body += (f'<script>var orderStr=\'<a href="/PRQWeb/QueryOrderDetail.do?{query}">'
                     f'Synthetic {number}</a>\';new KSCase(\'\',orderStr,\'2026-11-0{3-number}\','
                     "'','','完成','');</script>")

        def history(mrn, filter):
            self.record("POST", "/PRQWeb/QueryOrderResult.do", {
                "Use": "Dur", "date": "4000", "hhisnum": mrn, "hid": "HID",
                "ordersubtype": "*", "ordertype": filter.category, "orstepc": "*",
            }, body)
            return parse_clinical_orders(body, mrn=mrn)

        def detail(reference):
            html = "<table><tr><td>科別</td><td>Synthetic unit</td></tr></table>"
            if reference.sequence_no == "1":
                html += f'<button id="pacsBtn" hhisnum="{MRN}" reqno="{REQUEST_NO}">PACS</button>'
            self.record("GET", "/PRQWeb/QueryOrderDetail.do", {
                "hhisnum": MRN, "hid": "HID", "caseType": "O", "caseNo": "CASE",
                "seqNo": reference.sequence_no, "orRsType": "", "orStep": "", "source": "",
            }, html)
            return parse_order_detail(html, reference=reference)

        sdk.orders.get_order_history.side_effect = history
        sdk.orders.get_order_detail.side_effect = detail
        result = PacsLaunchDiscovery(sdk, MRN, output_dir=self.root).scan_next(batch)
        self.assertEqual(len(result.candidates), 1)
        summary = self.read("run_summary.json")
        summary.update(discovery=result.summary, selection="SELECTED", selection_index=1)
        self.write("run_summary.json", summary)
        self.write("private/selected_candidate.json", to_jsonable(result.candidates[0]))
        config = self.read("run_config.json")
        config.update(selection_mode="DISCOVER_AND_SELECT", request_no=None)
        self.write("run_config.json", config)
        return result

    def inspect(self):
        return self.fixture.inspect()

    def test_complete_menu_replays_sources_and_preserves_two_variants(self):
        self.menu()
        report = self.inspect()
        self.assertEqual(report["analysis_status"], "OK")
        discovery = report["pacs_launch_test"]["discovery"]
        self.assertEqual((discovery["total"], discovery["scanned"], discovery["pending"]), (2, 2, 0))
        self.assertEqual((discovery["candidate_count"], discovery["candidate_source_count"]), (1, 2))

    def test_pending_menu_is_gap_without_changing_handoff_success(self):
        self.menu(batch=1)
        report = self.inspect()
        self.assertEqual(report["analysis_status"], "COMPLETED_WITH_GAPS")
        self.assertEqual(report["bundle_status"], "HANDOFF_SENT")
        self.assertIsNone(report["root_cause"])
        self.assertEqual(report["pacs_launch_test"]["discovery"]["pending"], 1)

    def test_missing_candidate_source_file_cannot_pass_from_saved_candidates(self):
        self.menu()
        (self.root / "private/pacs_discovery/pages/000001.json").unlink()
        self.assertEqual(self.inspect()["analysis_status"], "NEEDS_ATTENTION")

    def test_saved_history_mismatch_is_separate_from_native_success(self):
        self.menu()
        history = self.read("private/pacs_discovery/history_1.json")
        history[0]["name"] = "Unverified replacement"
        self.write("private/pacs_discovery/history_1.json", history)
        report = self.inspect()
        self.assertEqual(report["analysis_status"], "NEEDS_ATTENTION")
        self.assertEqual(report["pacs_launch_test"]["native"]["status"], "VERIFIED")

    def test_cross_order_source_reference_is_rejected(self):
        self.menu()
        candidates = self.read("private/pacs_discovery/candidates.json")
        candidates[0]["sources"][0]["source_ref"]["sequence_no"] = "2"
        self.write("private/pacs_discovery/candidates.json", candidates)
        self.assertEqual(self.inspect()["analysis_status"], "NEEDS_ATTENTION")

    def test_unknown_discovery_row_shape_becomes_incomplete_not_exception(self):
        self.menu()
        self.write("private/pacs_discovery/candidates.json", ["unknown"])
        report = self.inspect()
        self.assertEqual(report["analysis_status"], "NEEDS_ATTENTION")
        self.assertEqual(report["pacs_launch_test"]["discovery"]["status"], "INCOMPLETE")

    def test_summary_cannot_claim_unscanned_rows_complete(self):
        self.menu(batch=1)
        discovery = self.read("private/pacs_discovery/summary.json")
        discovery.update(pending=0, complete=True)
        self.write("private/pacs_discovery/summary.json", discovery)
        summary = self.read("run_summary.json")
        summary["discovery"] = discovery
        self.write("run_summary.json", summary)
        self.assertEqual(self.inspect()["analysis_status"], "NEEDS_ATTENTION")

    def test_legacy_missing_index_uses_unique_exact_saved_choice_and_keeps_gap(self):
        self.menu()
        summary = self.read("run_summary.json")
        summary.update(sdk_version="0.24.0", selection_index=None, bridge_availability=None)
        self.write("run_summary.json", summary)
        report = self.inspect()
        self.assertEqual(report["analysis_status"], "COMPLETED_WITH_GAPS")
        self.assertIn("PACS_SELECTION_INDEX_NOT_RECORDED", report["pacs_launch_test"]["gaps"])
        selected = self.read("private/selected_candidate.json")
        selected["reference"]["request_no"] = "OTHER"
        self.write("private/selected_candidate.json", selected)
        self.assertEqual(self.inspect()["analysis_status"], "NEEDS_ATTENTION")

    def test_cancel_requires_no_preparation_availability_or_native(self):
        self.menu()
        summary = self.read("run_summary.json")
        summary.update(status="CANCELLED", selection="CANCELLED", selection_index=None,
                       prepare="NOT_RUN", handoff="NOT_REQUESTED", native_calls=0,
                       native_result=None, manual_observation="NOT_CHECKED", bridge_availability=None)
        self.write("run_summary.json", summary)
        path = self.root / "capture_manifest.jsonl"
        rows = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
        rows = [row for row in rows if "SmartWonderRAD" not in (row.get("request") or {}).get("url", "")]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), "utf-8")
        for file in (self.root / "private/native").iterdir():
            file.unlink()
        report = self.inspect()
        self.assertEqual(report["analysis_status"], "COMPLETED_WITH_GAPS")
        self.assertEqual(report["pacs_launch_test"]["availability"]["status"], "NOT_REQUIRED")
        self.assertEqual(report["pacs_launch_test"]["native"]["status"], "NOT_RUN")


if __name__ == "__main__":
    unittest.main()
