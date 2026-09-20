from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from vghks_sdk.core.browser_headers import BROWSER_USER_AGENT, DOCUMENT_ACCEPT
from vghks_sdk.core.config import RequestPolicy
from vghks_sdk.core.errors import ParseError
from vghks_sdk.core.readiness import make_auth_report, resolve_auth_targets
from vghks_sdk.core.transport import SafeSessionTransport
from vghks_sdk.live.atomic import build_test_plan, run_atomic_test
from vghks_sdk.live.config import LiveTestConfig, resolve_live_test_config
from vghks_sdk.live.profile import LIVE_TEST_MRN
from vghks_sdk.live_test_app import main
from vghks_sdk.models import (
    AuthCheckTarget,
    BinaryAsset,
    ClinicalOrder,
    OrderDetailRef,
    OrderReport,
    OrderReportRef,
    PacsImageRef,
    PacsStudy,
    PacsStudyRef,
    PdfAttachmentRef,
    VisitCase,
)
from vghks_sdk.order_status import classify_order_execution
from vghks_sdk.parsing.clinical import (
    parse_clinical_orders,
    parse_order_detail,
    parse_order_report,
    parse_pacs_study,
)
from vghks_sdk.workflows.order_reports import collect_order_reports

MRN = "SYNTHETIC"


def order(number="1", name="DBR", status="完成", **kw):
    return ClinicalOrder(
        MRN,
        "CASE",
        "O",
        name,
        "2026-09-19",
        status=status,
        report_ref=OrderReportRef(MRN, "CASE", "O", number),
        **kw,
    )


class OrderReportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.sdk = SimpleNamespace(
            orders=SimpleNamespace(
                get_order_detail=Mock(),
                get_order_report=Mock(),
                get_pacs_study=Mock(),
                download_pdf=Mock(),
                download_pacs_image=Mock(),
            )
        )

    def test_unexecuted_orders_do_not_consume_each_exam_budget_or_prove_no_data(self):
        skipped = [order(str(n), status="未執行 (申請序號:TEST)") for n in range(20)]
        dbr, micro = order("30"), order("31", "Microsonography")
        self.sdk.orders.get_order_report.side_effect = lambda ref: OrderReport(
            ref, {}, report_text="result", report_data_status="TEXT_AVAILABLE"
        )
        result = collect_order_reports(
            self.sdk,
            order_sources={"history": [*skipped, dbr, micro]},
            output_dir=self.root,
            max_orders_per_term=1,
        )
        self.assertEqual(result.counts["skipped_not_executed"], 20)
        self.assertEqual(result.coverage["DBR"]["selected"], 1)
        self.assertEqual(result.coverage["Microsonography"]["selected"], 1)
        self.assertEqual(self.sdk.orders.get_order_report.call_count, 2)
        statuses = [
            json.loads(p.read_text(encoding="utf-8"))["status"]
            for p in self.root.glob("stage2_orders/*.json")
        ]
        self.assertEqual(statuses.count("SKIPPED_NOT_EXECUTED"), 20)
        self.assertEqual(classify_order_execution("未執行（申請序號:X）"), "NOT_EXECUTED")
        self.assertEqual(classify_order_execution("待簽收"), "UNKNOWN")

    def test_failure_in_pdf_and_one_image_keeps_text_and_other_images(self):
        pdf = PdfAttachmentRef(MRN, f"//nfs01p/EMRU/{MRN}/report.pdf")
        study = PacsStudyRef(MRN, "REQ")
        images = tuple(PacsImageRef(MRN, "REQ", "", "", str(n)) for n in range(3))
        target = order(pacs_ref=study)
        self.sdk.orders.get_order_report.return_value = OrderReport(
            target.report_ref, {}, (pdf,), (study,), "numeric result: 12", "TEXT_AVAILABLE"
        )
        self.sdk.orders.get_pacs_study.return_value = PacsStudy(study, images, "IMAGES_AVAILABLE")
        self.sdk.orders.download_pdf.side_effect = ParseError(
            "invalid PDF", code="PDF_BINARY_INVALID"
        )
        self.sdk.orders.download_pacs_image.side_effect = [
            ParseError("invalid image", code="PACS_JPEG_INVALID"),
            BinaryAsset(b"\xff\xd8test1\xff\xd9", "image/jpeg"),
            BinaryAsset(b"\xff\xd8test2\xff\xd9", "image/jpeg"),
        ]
        result = collect_order_reports(
            self.sdk, order_sources={"history": [target]}, output_dir=self.root
        )
        self.assertEqual(result.status, "INCOMPLETE")
        self.assertEqual(result.counts["reports_with_text"], 1)
        self.assertEqual(result.counts["jpg_files"], 2)
        self.assertEqual(result.counts["query_errors"], 2)
        self.assertEqual(len(list(self.root.glob("stage3_assets/*.jpg"))), 2)
        row = json.loads(next(self.root.glob("stage2_orders/*.json")).read_text(encoding="utf-8"))
        self.assertEqual(row["status"], "PARTIAL_ERROR")
        self.assertIn("12", row["texts"][0]["text"])
        self.sdk.orders.get_pacs_study.assert_called_once()

    def test_empty_jpg_is_successful_empty_query_and_pdf_branch_still_runs(self):
        target = order()
        pdf = PdfAttachmentRef(MRN, f"//nfs01p/EMRU/{MRN}/report.pdf")
        study = PacsStudyRef(MRN, "REQ")
        self.sdk.orders.get_order_report.return_value = OrderReport(
            target.report_ref, {}, (pdf,), (study,), report_data_status="ATTACHMENT_ONLY"
        )
        self.sdk.orders.get_pacs_study.return_value = parse_pacs_study(
            '<div id="pacsContent">查無資料! </div>', reference=study
        )
        self.sdk.orders.download_pdf.return_value = BinaryAsset(
            b"%PDF-1.3\n%%EOF", "application/pdf"
        )
        result = collect_order_reports(
            self.sdk, order_sources={"history": [target]}, output_dir=self.root
        )
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.counts["empty_studies"], 1)
        self.assertEqual(result.counts["pdf_files"], 1)
        self.sdk.orders.download_pacs_image.assert_not_called()
        row = json.loads(next(self.root.glob("stage2_orders/*.json")).read_text(encoding="utf-8"))
        self.assertEqual(row["content_status"], "BINARY_AVAILABLE")
        self.assertFalse(row["texts"])

    def test_detail_failure_does_not_block_direct_report_and_duplicate_views_reuse_queries(self):
        target = order(detail_ref=OrderDetailRef(MRN, "CASE", "O", "1"))
        self.sdk.orders.get_order_detail.side_effect = ParseError("unknown page")
        self.sdk.orders.get_order_report.return_value = OrderReport(
            target.report_ref, {}, report_text="result", report_data_status="TEXT_AVAILABLE"
        )
        result = collect_order_reports(
            self.sdk, order_sources={"history": [target], "case": [target]}, output_dir=self.root
        )
        self.assertEqual(result.counts["matched_orders"], 1)
        self.sdk.orders.get_order_report.assert_called_once_with(target.report_ref)
        row = json.loads(next(self.root.glob("stage2_orders/*.json")).read_text(encoding="utf-8"))
        self.assertEqual(len(row["sources"]), 2)
        self.assertEqual(row["status"], "PARTIAL_ERROR")

    def test_unknown_status_remains_queryable_and_all_skips_are_explicit(self):
        target = order(status="待簽收")
        self.sdk.orders.get_order_report.return_value = OrderReport(
            target.report_ref, {}, report_data_status="EMPTY"
        )
        result = collect_order_reports(
            self.sdk, order_sources={"history": [target]}, output_dir=self.root
        )
        self.assertEqual(result.counts["selected_orders"], 1)
        self.assertEqual(result.counts["no_data"], 1)

    def test_direct_pdf_links_survive_order_detail_and_attachment_only_report_parsing(self):
        pdf_link = f'<a href="/PRQWeb/Page/JSP/showPDF.jsp?fileName=%2F%2Fnfs01p%2FEMRU%2F{MRN}%2Freport.pdf">報告附件</a>'
        report = parse_order_report(pdf_link, reference=OrderReportRef(MRN, "CASE", "O", "1"))
        self.assertEqual(report.report_data_status, "ATTACHMENT_ONLY")
        self.assertEqual(len(report.pdf_refs), 1)
        detail = parse_order_detail(
            "<table><tr><th>醫囑名稱</th><td>DBR</td></tr></table>" + pdf_link,
            reference=OrderDetailRef(MRN, "CASE", "O", "1"),
        )
        self.assertEqual(detail.pdf_refs, report.pdf_refs)
        html = f"""<script>var orderStr='DBR'; var f='//nfs01p/EMRU/{MRN}/report.pdf';
        new KSCase('',orderStr,'2026-09-19','','','完成','報告附件');</script>"""
        self.assertEqual(parse_clinical_orders(html, mrn=MRN)[0].pdf_refs, report.pdf_refs)

    def test_blank_changed_or_login_pacs_pages_never_count_as_empty(self):
        ref = PacsStudyRef(MRN, "REQ")
        for page in (
            "",
            '<form><input type="password"></form>',
            '<div id="pacsContent"></div>',
            '<script>var example="查無資料!";</script><div>error</div>',
        ):
            with self.subTest(page=page), self.assertRaises(ParseError):
                parse_pacs_study(page, reference=ref)
        for page in ('<div id="pacsContent">查無資料！</div>', "<html>查無資料!</html>"):
            self.assertEqual(parse_pacs_study(page, reference=ref).data_status, "EMPTY")


class OphthalmologyLiveTests(unittest.TestCase):
    def test_focused_profile_keeps_history_and_target_old_visit_despite_discovery_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = replace(order(), mrn=LIVE_TEST_MRN)
            target = replace(target, report_ref=replace(target.report_ref, mrn=LIVE_TEST_MRN))
            cases = [
                VisitCase(
                    LIVE_TEST_MRN, date(2026, 9, 19) - timedelta(days=n), "O", f"C{n}", "70", "眼科"
                )
                for n in range(8)
            ]
            old_case = VisitCase(LIVE_TEST_MRN, date(2023, 1, 1), "O", "CASE", "70", "眼科")

            def ready(*, only):
                return make_auth_report(
                    AuthCheckTarget(s.key, (), "", False, 1, 0, s.dependencies, "OK")
                    for s in resolve_auth_targets(only)
                )

            sdk = SimpleNamespace(
                auth=SimpleNamespace(check=Mock(side_effect=ready)),
                records=SimpleNamespace(get_visit_cases=Mock(return_value=[*cases, old_case])),
                orders=SimpleNamespace(
                    get_order_history=Mock(
                        side_effect=[[target], ParseError("second selector failed")]
                    ),
                    get_case_orders=Mock(return_value=[target]),
                    get_order_report=Mock(
                        return_value=OrderReport(
                            target.report_ref,
                            {},
                            report_text="data",
                            report_data_status="TEXT_AVAILABLE",
                        )
                    ),
                ),
            )
            config = LiveTestConfig(profile="ophthalmology", max_cases=1)
            self.assertEqual(
                resolve_live_test_config(json_values=config.to_safe_dict(), environ={}), config
            )
            plan = build_test_plan(config)
            self.assertEqual(plan["auth_targets"], ["portal", "prq"])
            self.assertFalse(plan["weekly_opd_soap"]["enabled"])
            with redirect_stdout(io.StringIO()):
                result = run_atomic_test(sdk, config, output_dir=root)
            self.assertEqual(result.status, "COMPLETED_WITH_ERRORS")
            sdk.orders.get_case_orders.assert_called_once_with(old_case)
            sdk.orders.get_order_report.assert_called_once_with(target.report_ref)
            self.assertTrue(
                (root / "parsed/workflows/ophthalmology_orders/manifest.json").is_file()
            )
            self.assertEqual(
                [c.kwargs["only"] for c in sdk.auth.check.call_args_list], [("portal",), ("prq",)]
            )

    def test_double_click_defaults_to_combined_round(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("vghks_sdk.live_test_app.Path.cwd", return_value=Path(temporary)),
            patch.dict("os.environ", {}, clear=True),
            patch("vghks_sdk.live_test_app.run_live_test_namespace", return_value=0) as execute,
            patch("builtins.input", return_value=""),
        ):
            self.assertEqual(main([]), 0)
        self.assertEqual(execute.call_args.args[0].profile, "comprehensive")

    def test_real_requests_defaults_become_browser_headers_and_custom_headers_survive(self):
        with requests.Session() as session:
            SafeSessionTransport(policy=RequestPolicy(), session=session)
            prepared = session.prepare_request(requests.Request("GET", "https://example.invalid/"))
            self.assertEqual(prepared.headers["User-Agent"], BROWSER_USER_AGENT)
            self.assertEqual(prepared.headers["Accept"], DOCUMENT_ACCEPT)
            self.assertNotIn("Cookie", prepared.headers)
            session.headers.update({"User-Agent": "custom-agent", "Accept": "application/json"})
            SafeSessionTransport(policy=RequestPolicy(), session=session)
            self.assertEqual(session.headers["User-Agent"], "custom-agent")
            self.assertEqual(session.headers["Accept"], "application/json")


if __name__ == "__main__":
    unittest.main()
