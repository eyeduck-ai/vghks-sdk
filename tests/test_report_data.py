from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from vghks_sdk.core.errors import ErrorInfo, ParseError
from vghks_sdk.live.atomic import _counts, _write_coverage, build_test_plan
from vghks_sdk.live.config import LiveTestConfig
from vghks_sdk.live.profile import LiveTestStep
from vghks_sdk.models import OrderReportRef
from vghks_sdk.offline.analyze import _report_content_summary
from vghks_sdk.offline.replay import replay_response
from vghks_sdk.parsing.clinical import parse_order_report

REFERENCE = OrderReportRef("SYNTHETIC", "C1", "O", "1")
PDF = '<script>var p="//nfs01p/EMRU/SYNTHETIC/report.pdf";</script>'


class ReportDataTests(unittest.TestCase):
    def test_text_and_tables_are_usable_without_pdf(self):
        report = parse_order_report(
            "<table><tr><th>檢查項目</th><td>Synthetic</td></tr><tr><th>報告內容 JPG 報告歷程</th><td><pre>Findings: synthetic result</pre><table><tr><td>OD</td><td>1.25</td></tr></table></td></tr></table>",
            reference=REFERENCE,
        )
        self.assertEqual(report.report_data_status, "TEXT_AVAILABLE")
        self.assertIn("synthetic result", report.report_text)
        self.assertIn("1.25", report.report_text)
        self.assertFalse(report.pdf_refs)

    def test_metadata_pdf_links_and_viewer_controls_are_not_report_data(self):
        for body, expected in (
            (
                "<table><tr><th>檢查項目</th><td>Synthetic</td></tr><tr><th>報告日期</th><td>2026-09-19</td></tr></table>",
                "METADATA_ONLY",
            ),
            (
                '<table><tr><th>報告內容</th><td><pre></pre><a href="#">DBR PDF</a><button>Download</button>'
                + PDF
                + "</td></tr></table>",
                "ATTACHMENT_ONLY",
            ),
            (
                "<table><tr><th>報告內容</th><td>詳見附件" + PDF + "</td></tr></table>",
                "ATTACHMENT_ONLY",
            ),
        ):
            report = parse_order_report(body, reference=REFERENCE)
            self.assertEqual(report.report_data_status, expected)
            self.assertEqual(report.report_text, "")
        with self.assertRaises(ParseError):
            parse_order_report(
                '<html><embed type="application/pdf" src="about:blank"></html>', reference=REFERENCE
            )

    def test_known_literal_text_is_preserved_and_dynamic_code_is_not_executed(self):
        prefix = "<table><tr><th>報告內容</th><td>"
        suffix = "</td></tr></table>"
        report = parse_order_report(
            prefix + '<script>document.write("<pre>synthetic\\nresult</pre>");</script>' + suffix,
            reference=REFERENCE,
        )
        self.assertEqual(report.report_data_status, "TEXT_AVAILABLE")
        self.assertIn("synthetic", report.report_text)
        unknown = parse_order_report(
            prefix + "<script>document.write(fetchSecret());</script>" + suffix, reference=REFERENCE
        )
        self.assertEqual(unknown.report_data_status, "METADATA_ONLY")
        self.assertTrue(unknown.text_extraction_notes)

    def test_sequential_reports_do_not_inherit_text_from_reused_view(self):
        first = parse_order_report(
            "<table><tr><th>Report</th><td>First result</td></tr></table>", reference=REFERENCE
        )
        second_ref = OrderReportRef("SYNTHETIC", "C1", "O", "2")
        second = parse_order_report(
            "<table><tr><th>報告內容</th><td>" + PDF + "</td></tr></table>", reference=second_ref
        )
        self.assertEqual(first.report_data_status, "TEXT_AVAILABLE")
        self.assertEqual(second.report_data_status, "ATTACHMENT_ONLY")
        self.assertEqual(second.reference, second_ref)
        self.assertEqual(second.report_text, "")

    def test_attachment_error_does_not_revoke_available_text_in_live_and_offline_summaries(self):
        report = parse_order_report(
            "<table><tr><th>Report</th><td>SECRET_REPORT_TEXT</td></tr></table>" + PDF,
            reference=REFERENCE,
        )
        steps = [
            LiveTestStep(
                "prq.order_report.0001", "OK", operation="prq.order_report", details=_counts(report)
            ),
            LiveTestStep(
                "prq.pdf_attachment.0001",
                "ERROR",
                operation="prq.pdf_attachment",
                issue=ErrorInfo("PDF_BINARY_INVALID", "PARSE"),
            ),
        ]
        plan = build_test_plan(
            LiveTestConfig(profile="atomic", only_operations=("prq.order_report",))
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _write_coverage(root, plan, steps, "COMPLETED_WITH_ERRORS")
            coverage = json.loads((root / "coverage.json").read_text())
            self.assertEqual(coverage["report_data"][0]["data_status"], "TEXT_AVAILABLE")
            self.assertNotIn("SECRET_REPORT_TEXT", (root / "RESULTS.txt").read_text())
        summary = _report_content_summary(
            [
                {"operation": "prq.order_report", "status": "OK", "details": _counts(report)},
                {"operation": "prq.pdf_attachment", "status": "ERROR"},
            ],
            [],
        )
        self.assertEqual(summary["live_steps"]["TEXT_AVAILABLE"], 1)

    def test_offline_replay_counts_text_without_exporting_clinical_content(self):
        result = replay_response(
            "prq.text_report",
            b"<table><tr><th>Report</th><td>SECRET_REPORT_TEXT</td></tr></table>",
            {"hhisnum": "SYNTHETIC", "caseNo": "C1", "caseType": "O", "seqNo": "1"},
            mime="text/html; charset=utf-8",
        )
        self.assertEqual(result["report_data_status"], "TEXT_AVAILABLE")
        self.assertNotIn("SECRET_REPORT_TEXT", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
