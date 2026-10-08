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
from vghks_sdk.core.errors import ConfigurationError, ParseError
from vghks_sdk.core.readiness import make_auth_report, resolve_auth_targets
from vghks_sdk.core.transport import SafeSessionTransport
from vghks_sdk.live.atomic import build_test_plan, run_atomic_test
from vghks_sdk.live.config import LiveTestConfig, resolve_live_test_config
from vghks_sdk.live.profile import LIVE_TEST_MRN
from vghks_sdk.live_test_app import main
from vghks_sdk.local_io import write_bytes_atomic, write_json_atomic
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

    def test_shared_pdf_is_stored_once_and_preserves_per_order_availability(self):
        pdf = PdfAttachmentRef(MRN, f"//nfs01p/EMRU/{MRN}/shared.pdf")
        content = b"%PDF-1.4\nshared synthetic attachment\n%%EOF"
        targets = [order("1"), order("2")]
        self.sdk.orders.get_order_report.side_effect = lambda ref: OrderReport(
            ref, {}, (pdf,), report_data_status="ATTACHMENT_ONLY"
        )
        self.sdk.orders.download_pdf.return_value = BinaryAsset(content, "application/pdf")
        result = collect_order_reports(
            self.sdk, order_sources={"history": targets}, output_dir=self.root
        )
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.counts["pdf_files"], 2)
        self.assertEqual(result.counts["unique_pdf_files"], 1)
        self.assertEqual(result.counts["unique_jpg_files"], 0)
        self.assertEqual(result.counts["downloaded_bytes"], len(content))
        self.sdk.orders.download_pdf.assert_called_once_with(pdf)
        stored = next(self.root.glob("stage3_assets/*.pdf"))
        self.assertEqual(stored.read_bytes(), content)
        audits = [json.loads(p.read_text(encoding="utf-8")) for p in
                  self.root.glob("stage3_assets/*.outcome.json")]
        self.assertEqual(audits[0]["sha256"], self.sdk.orders.download_pdf.return_value.sha256)
        rows = [json.loads(p.read_text(encoding="utf-8")) for p in self.root.glob("stage2_orders/*.json")]
        self.assertEqual(sum(b["reused"] for row in rows for b in row["branches"]), 1)

    def test_runner_result_is_not_rewritten_and_value_only_runner_still_persists(self):
        target = order()
        report = OrderReport(target.report_ref, {}, report_text="synthetic", report_data_status="TEXT_AVAILABLE")

        def runner(_name, operation, arguments, path):
            self.assertEqual(operation, "prq.order_report")
            self.assertEqual(arguments, {"ref": target.report_ref})
            write_json_atomic(path.with_suffix(".input.json"), {"runner_input": True})
            write_json_atomic(path, report)
            return report, None

        def reject_duplicate(path, value):
            if path.name in {"0001-order_report.json", "0001-order_report.input.json"}:
                raise ConfigurationError("duplicate query file write")
            return write_json_atomic(path, value)

        with patch("vghks_sdk.workflows.order_reports.write_json_atomic", side_effect=reject_duplicate):
            result = collect_order_reports(
                self.sdk, order_sources={"history": [target]}, output_dir=self.root,
                query_runner=runner,
            )
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.counts["reports_with_text"], 1)
        fallback = self.root / "fallback"
        result = collect_order_reports(
            self.sdk, order_sources={"history": [target]}, output_dir=fallback,
            query_runner=lambda *_: (report, None),
        )
        self.assertEqual(result.status, "OK")
        self.assertEqual(len(list(fallback.glob("stage2_queries/*.input.json"))), 1)
        self.assertTrue((fallback / "stage2_queries/0001-order_report.json").is_file())

    def test_binary_save_failure_keeps_other_attachment_and_exposes_local_issue(self):
        target = order()
        pdfs = tuple(PdfAttachmentRef(MRN, f"//nfs01p/EMRU/{MRN}/{n}.pdf") for n in range(2))
        content = b"%PDF-1.4\nsynthetic\n%%EOF"
        self.sdk.orders.get_order_report.return_value = OrderReport(
            target.report_ref, {}, pdfs, report_data_status="ATTACHMENT_ONLY"
        )
        self.sdk.orders.download_pdf.return_value = BinaryAsset(content, "application/pdf")
        def store(path, data):
            if path.name.startswith("0002-"):
                raise ConfigurationError("synthetic local failure", code="OUTPUT_WRITE_FAILED")
            return write_bytes_atomic(path, data)

        with patch("vghks_sdk.workflows.order_reports.write_bytes_atomic", side_effect=store):
            result = collect_order_reports(
                self.sdk, order_sources={"history": [target]}, output_dir=self.root
            )
        self.assertEqual(result.status, "INCOMPLETE")
        self.assertEqual(result.counts["query_errors"], 1)
        self.assertEqual(result.counts["unique_pdf_files"], 1)
        self.assertEqual(result.counts["downloaded_bytes"], len(content))
        self.assertEqual(self.sdk.orders.download_pdf.call_count, 2)
        self.assertEqual(len(list(self.root.glob("stage3_assets/*.pdf"))), 1)
        row = json.loads(next(self.root.glob("stage2_orders/*.json")).read_text(encoding="utf-8"))
        self.assertEqual(row["status"], "PARTIAL_ERROR")
        self.assertEqual(row["branches"][1]["issue"]["code"], "OUTPUT_WRITE_FAILED")

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
    def test_dbr_profile_uses_only_historical_orders_and_keeps_references_deduplicated(self):
        target = replace(order(), mrn=LIVE_TEST_MRN)
        target = replace(target, report_ref=replace(target.report_ref, mrn=LIVE_TEST_MRN))
        pdf = PdfAttachmentRef(LIVE_TEST_MRN, f"//nfs01p/EMRU/{LIVE_TEST_MRN}/report.pdf")
        sdk = SimpleNamespace(
            auth=SimpleNamespace(check=Mock(side_effect=lambda *, only: make_auth_report(
                AuthCheckTarget(s.key, (), "", False, 1, 0, s.dependencies, "OK")
                for s in resolve_auth_targets(only)
            ))),
            records=SimpleNamespace(get_visit_cases=Mock(side_effect=AssertionError("case discovery"))),
            orders=SimpleNamespace(
                get_order_history=Mock(return_value=[target, order("2", "Microsonography")]),
                get_order_report=Mock(return_value=OrderReport(
                    target.report_ref, {}, (pdf,), report_data_status="ATTACHMENT_ONLY"
                )),
                download_pdf=Mock(return_value=BinaryAsset(b"%PDF-1.4\n%%EOF", "application/pdf")),
                get_case_orders=Mock(side_effect=AssertionError("case orders")),
            ),
        )
        config = LiveTestConfig(profile="dbr")
        self.assertEqual(config.asset_terms, ("DBR",))
        self.assertEqual(config.max_items, 8)
        self.assertEqual(config.login_negative_attempts, 0)
        self.assertEqual(resolve_live_test_config(json_values=config.to_safe_dict(), environ={}), config)
        plan = build_test_plan(config)
        self.assertEqual(plan["auth_targets"], ["portal", "prq"])
        self.assertEqual({row["key"] for row in plan["operations"]}, {
            "prq.order_history", "prq.order_detail", "prq.order_report", "prq.pacs_study",
            "prq.pdf_attachment", "prq.pacs_image",
        })
        self.assertEqual(plan["ophthalmology_orders"]["entry_path"], "order_history")
        self.assertFalse(plan["ophthalmology_orders"]["case_discovery"])
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(io.StringIO()):
            result = run_atomic_test(sdk, config, output_dir=Path(temporary))
            self.assertEqual(result.status, "OK")
            self.assertEqual(len(list(Path(temporary).rglob("*.pdf"))), 1)
        filters = [call.args[1] for call in sdk.orders.get_order_history.call_args_list]
        self.assertEqual([value.category for value in filters], ["*", "OR"])
        self.assertEqual([value.lookback_days for value in filters], [4000, 4000])
        sdk.orders.download_pdf.assert_called_once_with(pdf)
        sdk.orders.get_order_report.assert_called_once_with(target.report_ref)
        sdk.records.get_visit_cases.assert_not_called()
        sdk.orders.get_case_orders.assert_not_called()

    def test_dbr_missing_sample_cannot_count_as_successful_pdf_download(self):
        for rows in ([], [order(status="未執行")]):
            with self.subTest(rows=len(rows)), tempfile.TemporaryDirectory() as temporary:
                sdk = SimpleNamespace(
                    auth=SimpleNamespace(check=Mock(side_effect=lambda *, only: make_auth_report(
                        AuthCheckTarget(s.key, (), "", False, 1, 0, s.dependencies, "OK")
                        for s in resolve_auth_targets(only)
                    ))),
                    orders=SimpleNamespace(get_order_history=Mock(return_value=rows)),
                )
                with redirect_stdout(io.StringIO()):
                    result = run_atomic_test(sdk, LiveTestConfig(profile="dbr"), output_dir=Path(temporary))
                self.assertEqual(result.status, "COMPLETED_WITH_GAPS")
                step = next(step for step in result.steps if step.name == "dbr.pdf_sample")
                self.assertEqual(step.status, "NO_SAMPLE")

    def test_dbr_history_failure_blocks_pdf_instead_of_claiming_no_sample(self):
        sdk = SimpleNamespace(
            auth=SimpleNamespace(check=Mock(side_effect=lambda *, only: make_auth_report(
                AuthCheckTarget(s.key, (), "", False, 1, 0, s.dependencies, "OK")
                for s in resolve_auth_targets(only)
            ))),
            orders=SimpleNamespace(get_order_history=Mock(side_effect=ParseError("history failed"))),
        )
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(io.StringIO()):
            result = run_atomic_test(sdk, LiveTestConfig(profile="dbr"), output_dir=Path(temporary))
            manifest = json.loads((Path(temporary) / "parsed/workflows/ophthalmology_orders/manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(result.status, "COMPLETED_WITH_ERRORS")
        self.assertEqual(manifest["status"], "BLOCKED")
        self.assertEqual(manifest["blocked_reason"], "ORDER_HISTORY_FAILED")
        self.assertEqual(next(step for step in result.steps if step.name == "dbr.pdf_sample").status, "BLOCKED")
        self.assertFalse(any(step.status == "NO_SAMPLE" for step in result.steps))

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
