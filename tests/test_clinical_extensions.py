from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from vghks_sdk import SDKSettings
from vghks_sdk.adapters.oppl import OpplAdapter
from vghks_sdk.adapters.prq import PrqAdapter
from vghks_sdk.cli import (
    _patient_records_visit_filter,
    _validate_cli_arguments,
    build_parser,
)
from vghks_sdk.core.errors import AuthenticationError, ConfigurationError, ParseError
from vghks_sdk.core.jsliteral import evaluated_string_assignments, static_document_writes
from vghks_sdk.models import (
    BinaryAsset,
    ClinicalOrder,
    MedicationHistoryFilter,
    NumericHistoryFilter,
    NumericHistoryReport,
    NumericReport,
    OrderDetailRef,
    OrderHistoryFilter,
    OrderReport,
    OrderReportRef,
    PacsStudyRef,
    PdfAttachmentRef,
    SoapRecord,
    SurgeryHistoryFilter,
    VisitCase,
    VisitFilter,
    to_jsonable,
)
from vghks_sdk.parsing.clinical import (
    parse_clinical_orders,
    parse_consults,
    parse_numeric_history,
    parse_order_report,
    parse_pacs_study,
    parse_surgery_history,
    parse_treatments,
)
from vghks_sdk.workflows.patient_records import export_patient_records, select_asset_orders

MRN = "0000000"


class ClinicalParserTests(unittest.TestCase):
    def test_treatment_table_keeps_all_nine_columns_and_rejects_unknown_rows(self):
        case = VisitCase(MRN, date(2026, 1, 1), "O", "C1", "70", "Eye")
        headers = (
            "常規 治療項目",
            "頻次",
            "開立時間",
            "開立者",
            "停止時間",
            "現況",
            "說明",
            "PDA",
            "附檔",
        )
        values = (
            "Synthetic treatment",
            "Daily",
            "2026-01-01 10:00",
            "Test",
            "",
            "Active",
            "",
            "",
            "",
        )
        source = '<div id="orderList"><table><tr>' + "".join(f"<th>{v}</th>" for v in headers)
        source += (
            "</tr><tr>"
            + "".join(f"<td>{v}</td>" for v in values)
            + '</tr></table><div id="agrfileDiv"></div></div>'
        )
        rows = parse_treatments(source, case=case)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].fields, dict(zip(headers, values)))
        self.assertEqual(rows[0].case, case)
        with self.assertRaises(ParseError):
            parse_treatments(source.replace("<td></td>", "", 1), case=case)
        with self.assertRaises(ParseError):
            parse_treatments(source.replace("頻次", "Unknown"), case=case)

    def test_numeric_tables_survive_literal_narrative_but_dynamic_code_is_rejected(self):
        source = r"""<script>
        var resVal = "Synthetic line one\nline two";
        while(resVal.indexOf("\n")>0){ resVal=resVal.replace("\n","<br/>"); }
        document.write("<pre>"+resVal+"</pre>");
        </script><table id="resnumTable0"><tr><th>Date</th><th>Value</th></tr>
        <tr><td>2026-01-01</td><td>18</td></tr></table>"""
        result = parse_numeric_history(source, mrn=MRN)
        self.assertEqual(result.tables[0].rows, (("2026-01-01", "18"),))
        with self.assertRaises(ParseError):
            parse_numeric_history(
                source.replace('"Synthetic line one\\nline two"', "danger()"), mrn=MRN
            )

    def test_explicit_current_patient_no_report_is_empty_not_a_parser_failure(self):
        from vghks_sdk.live.atomic import _classify
        from vghks_sdk.offline.replay import replay_response

        reference = OrderReportRef(MRN, "C1", "O", "1")
        source = f'<div id="rptDiv"></div><font>！{MRN}該病患本次查詢無報告資料，請確認</font>'
        value = parse_order_report(source, reference=reference)
        self.assertEqual(value.fields, {})
        self.assertEqual(_classify(value), "EMPTY")
        replayed = replay_response(
            "prq.order_report",
            source.encode(),
            {"hhisnum": MRN, "caseNo": "C1", "caseType": "O", "seqNo": "1"},
            mime="text/html; charset=utf-8",
        )
        self.assertEqual(replayed["status"], "EMPTY")
        for bad in (
            '<div id="rptDiv"></div>',
            source.replace(MRN, "9999999"),
            source.replace("</div>", '<iframe src="unknown"></iframe></div>'),
        ):
            with self.subTest(source=bad), self.assertRaises(ParseError):
                parse_order_report(bad, reference=reference)

    def test_surgery_search_uses_card_not_doctor_display_name(self):
        from unittest.mock import Mock

        runtime = SimpleNamespace(
            settings=SDKSettings(),
            auth=SimpleNamespace(hid_for=lambda app: "TEST-HID"),
            request_text=Mock(return_value="Synthetic Doctor"),
            request_json=Mock(return_value={"surgs": []}),
            execute=lambda spec, callback, **kwargs: callback(),
        )
        result = OpplAdapter(runtime).get_schedule("T001", date(2026, 1, 1), date(2026, 1, 2))
        self.assertEqual(result, [])
        self.assertEqual(
            runtime.request_text.call_args.kwargs["data"],
            {"cardno": "T001", "hid": "TEST-HID", "method": "searchDr"},
        )
        self.assertEqual(runtime.request_json.call_args.kwargs["data"]["drno"], "T001")
        self.assertEqual(
            runtime.request_text.call_args.kwargs["headers"],
            runtime.request_json.call_args.kwargs["headers"],
        )
        self.assertEqual(
            runtime.request_json.call_args.kwargs["headers"]["X-Requested-With"], "XMLHttpRequest"
        )

    def test_constant_branch_is_selected_and_unknown_branch_fails_closed(self) -> None:
        source = """
        <script>
        var orderStr='<span urlstr="/PRQWeb/QueryOrderDetail.do?source=order&hhisnum=0000000&caseType=O&caseNo=C1&seqNo=1&orRsType=&orStep=72"></span>';
        var mydate='2026-06-05'; var rcpDt='2026-06-06'; var qrcodeStr='';
        if(false){ aryCase[0]=new KSCase('',orderStr+'Old',mydate,rcpDt,'Dr','Done',qrcodeStr); }
        else { aryCase[0]=new KSCase('',orderStr+'New',mydate,rcpDt,'Dr','Done',qrcodeStr); }
        </script>
        """
        rows = parse_clinical_orders(source, mrn=MRN)
        self.assertEqual([row.name for row in rows], ["New"])
        unsafe = source.replace("if(false)", "if(danger())")
        with self.assertRaises(ParseError) as caught:
            parse_clinical_orders(unsafe, mrn=MRN)
        self.assertEqual(caught.exception.info.code, "JS_BRANCH_UNSUPPORTED")

    def test_allowlisted_assignments_and_document_writes_fail_closed(self) -> None:
        with self.assertRaises(ParseError) as branch:
            evaluated_string_assignments(
                "var orderStr='safe'; if (danger()) { orderStr='unsafe'; }",
                {"orderStr"},
            )
        self.assertEqual(branch.exception.info.code, "JS_BRANCH_UNSUPPORTED")
        with self.assertRaises(ParseError) as expression:
            evaluated_string_assignments("var orderStr=danger();", {"orderStr"})
        self.assertEqual(expression.exception.info.code, "JS_EXPRESSION_UNSUPPORTED")
        with self.assertRaises(ParseError) as document_write:
            static_document_writes("document.write('<td>'+unknown+'</td>');")
        self.assertEqual(
            document_write.exception.info.code,
            "JS_DOCUMENT_WRITE_UNSUPPORTED",
        )

    def test_numeric_standard_and_eye_tables_and_surgery(self) -> None:
        numeric = """
        <div id="data"><table id="resnumTable0">
          <tr><script>document.write('<th>Date</th><th>GOT</th>');</script></tr>
          <tr><td>2026-01-01</td><td>18</td></tr>
        </table>
        <table class="eTable"><tr><th>Eye</th><th>IOP</th></tr><tr><td>OD</td><td>15</td></tr></table>
        </div>
        """
        report = parse_numeric_history(numeric, mrn=MRN)
        self.assertEqual(len(report.tables), 2)
        self.assertEqual(report.tables[0].rows[0], ("2026-01-01", "18"))

        surgery = """
        <table><tr><th>Date</th><th>Procedure</th><th>A</th><th>B</th><th>C</th>
        <th>D</th><th>E</th><th>F</th></tr>
        <tr><td>2026-01-02</td><td>Procedure</td><td><a>x</a></td><td>-</td>
        <td></td><td>Y</td><td></td><td></td></tr></table>
        """
        rows = parse_surgery_history(surgery, mrn=MRN)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].surgery_record_available)
        self.assertTrue(rows[0].preoperative_record_available)

    def test_empty_consult_treatment_and_nonempty_fail_closed(self) -> None:
        case = VisitCase(MRN, date(2026, 1, 1), "O", "C1", "70", "Eye")
        self.assertEqual(
            parse_consults('<div id="clorderList"><div id="orwordsDiv"></div></div>', case=case),
            [],
        )
        self.assertEqual(
            parse_treatments('<div id="orderList"><div id="agrfileDiv"></div></div>', case=case),
            [],
        )
        with self.assertRaises(ParseError) as caught:
            parse_consults(
                '<div id="clorderList"><div id="orwordsDiv"></div><table><tr><td>x</td></tr></table></div>',
                case=case,
            )
        self.assertEqual(caught.exception.info.code, "PRQ_CONSULT_NONEMPTY_UNSUPPORTED")

    def test_two_pdf_refs_and_seven_pacs_images(self) -> None:
        report_html = """
        <table><tr><th>Report</th><td>OK</td></tr></table>
        <script>
        var a=encodeURIComponent(encodeURIComponent('//hfs02_1A0/REPORT/74/0000000/a.pdf'));
        var b=encodeURIComponent(encodeURIComponent('//hfs02_1A0/REPORT/74/0000000/b.pdf'));
        </script>
        """
        report = parse_order_report(
            report_html,
            reference=OrderReportRef(MRN, "C1", "O", "1"),
        )
        self.assertEqual(len(report.pdf_refs), 2)

        images = "".join(
            f'<img class="pacsJpg" src="/PRQWeb/Page/JSP/showPACSPic.jsp?hhisnum={MRN}&reqno=R1&uid=1.2.{index}&STUDY_UID=&SERIES_UID=">'
            for index in range(7)
        )
        study = parse_pacs_study(images, reference=PacsStudyRef(MRN, "R1"))
        self.assertEqual(len(study.images), 7)

    def test_binary_json_omits_bytes_and_refs_reject_tampering(self) -> None:
        asset = BinaryAsset(b"%PDF-test\n%%EOF", "application/pdf")
        payload = to_jsonable(asset)
        self.assertEqual(set(payload), {"media_type", "size", "sha256"})
        self.assertNotIn("content", json.dumps(payload))
        self.assertNotIn("%PDF", repr(asset))
        with self.assertRaises(ConfigurationError):
            PdfAttachmentRef(MRN, "https://outside.invalid/a.pdf")
        with self.assertRaises(ConfigurationError):
            PdfAttachmentRef(MRN, "//hfs02_1A0/REPORT/74/9999999/a.pdf")
        with self.assertRaises(ConfigurationError):
            OrderDetailRef(MRN, "../case", "O", "1")
        with self.assertRaises(ConfigurationError):
            PacsStudyRef(MRN, "")

    def test_order_parser_preserves_direct_pacs_reference(self) -> None:
        source = f"""
        <script>
        var orderStr='<span urlstr="/PRQWeb/QueryOrderDetail.do?source=order&hhisnum={MRN}&caseType=O&caseNo=C1&seqNo=1&orRsType=&orStep=72"></span>';
        orderStr += '<span urlstr="/PRQWeb/Adm_QueryPACS.do?hhisnum={MRN}&reqno=R1"></span>Microsonography';
        aryCase[0]=new KSCase('',orderStr,'2026-01-01','','','','');
        </script>
        """
        rows = parse_clinical_orders(source, mrn=MRN)
        self.assertEqual(rows[0].pacs_ref, PacsStudyRef(MRN, "R1"))

    def test_history_all_mappings_and_departmental_code(self) -> None:
        self.assertEqual(OrderHistoryFilter(category="DEPARTMENTAL").lookback_days, 4000)
        self.assertEqual(OrderHistoryFilter(category="DEPARTMENTAL").category, "OR")
        self.assertEqual(MedicationHistoryFilter().lookback_days, 2555)
        self.assertEqual(NumericHistoryFilter().lookback_days, 3650)
        self.assertEqual(SurgeryHistoryFilter().lookback_days, 20000)


class FakeAuth:
    generation = 1

    def hid_for(self, _app: str) -> str:
        return "CURRENT-HID"


class RecordingRuntime:
    def __init__(self) -> None:
        self.auth = FakeAuth()
        self.settings = SimpleNamespace(prq_base_url="https://internal.test/PRQWeb")
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute(self, _spec, operation, *, operation_name):
        return operation()

    def request_text(self, spec, _url, **kwargs):
        self.calls.append((spec.key, kwargs))
        if spec.key in {"prq.order_history", "prq.case_orders"}:
            return "<script>var aryCase=[];</script>"
        return "<html>OK</html>"

    def request_binary(self, spec, _url, *, max_bytes, **kwargs):
        self.calls.append((spec.key, kwargs))
        return b"not-a-pdf", "text/html"


class AdapterTests(unittest.TestCase):
    def test_history_sequence_use_filters_and_current_hid(self) -> None:
        runtime = RecordingRuntime()
        adapter = PrqAdapter(runtime)  # type: ignore[arg-type]
        rows = adapter.get_order_history(MRN, OrderHistoryFilter(category="DEPARTMENTAL"))
        self.assertEqual(rows, [])
        self.assertEqual(
            [key for key, _ in runtime.calls],
            [
                "prq.patient_history_context",
                "prq.order_history_page",
                "prq.order_history_select",
                "prq.order_history",
            ],
        )
        data = runtime.calls[-1][1]["data"]
        self.assertEqual(data["Use"], "Dur")
        self.assertEqual(data["ordertype"], "OR")
        self.assertEqual(data["date"], "4000")
        self.assertEqual(data["hid"], "CURRENT-HID")

    def test_case_scope_uses_case_and_invalid_pdf_magic_is_rejected(self) -> None:
        runtime = RecordingRuntime()
        adapter = PrqAdapter(runtime)  # type: ignore[arg-type]
        case = VisitCase(MRN, date(2026, 1, 1), "O", "C1", "70", "Eye")
        with (
            patch.object(adapter, "_get_case_detail_raw"),
            patch.object(adapter, "_prime_key_raw"),
        ):
            self.assertEqual(adapter.get_case_orders(case), [])
        data = next(kwargs["data"] for key, kwargs in runtime.calls if key == "prq.case_orders")
        self.assertEqual(data["Use"], "Case")
        reference = PdfAttachmentRef(MRN, f"//hfs02_1A0/REPORT/74/{MRN}/a.pdf")
        with self.assertRaises(ParseError) as caught:
            adapter.download_pdf(reference)
        self.assertEqual(caught.exception.info.code, "PDF_BINARY_INVALID")


class RecordWorkflowTests(unittest.TestCase):
    @staticmethod
    def _orders() -> list[ClinicalOrder]:
        def order(name: str, opened: str, seq: str) -> ClinicalOrder:
            return ClinicalOrder(
                MRN,
                "C1",
                "O",
                name,
                order_date=opened,
                report_ref=OrderReportRef(MRN, "C1", "O", seq),
            )

        return [
            order("Ｍicrosonography old", "2025-01-01", "1"),
            order("Microsonography latest", "2026-01-01", "2"),
            order("DBR examination", "2026-02-01", "3"),
        ]

    def test_latest_per_term_and_partial_asset_failure(self) -> None:
        orders = self._orders()
        selected = select_asset_orders(orders, terms=("microsonography", "dbr"))
        self.assertEqual([item.name for item in selected], [orders[1].name, orders[2].name])
        selected_all = select_asset_orders(
            orders,
            terms=("microsonography", "dbr"),
            all_matching=True,
        )
        self.assertEqual(
            [item.name for item in selected_all],
            [orders[1].name, orders[0].name, orders[2].name],
        )

        good = PdfAttachmentRef(MRN, f"//hfs02_1A0/REPORT/74/{MRN}/good.pdf")
        bad = PdfAttachmentRef(MRN, f"//hfs02_1A0/REPORT/74/{MRN}/bad.pdf")

        class FakeSDK:
            def __init__(self):
                self.orders = self
                self.medications = self
                self.records = self

            def get_order_history(self, _mrn, _filter):
                return orders

            def get_medication_history(self, _mrn, _filter):
                return []

            def get_numeric_history(self, _mrn, _filter):
                return NumericHistoryReport(MRN, ())

            def get_surgery_history(self, _mrn, _filter):
                return []

            def get_order_report(self, ref):
                return OrderReport(ref, {"report": "ok"}, (good, bad), ())

            def download_pdf(self, ref):
                if ref == bad:
                    raise ParseError("fixture", code="PDF_BINARY_INVALID")
                return BinaryAsset(b"%PDF-ok\n%%EOF", "application/pdf")

        with tempfile.TemporaryDirectory() as temp_dir:
            result = export_patient_records(
                FakeSDK(),  # type: ignore[arg-type]
                mrns=(MRN,),
                output_dir=Path(temp_dir) / "records",
                download_assets=True,
            )
            self.assertEqual(result.asset_count, 1)
            self.assertGreaterEqual(result.error_count, 1)
            assets = json.loads(result.asset_manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(assets["assets"][0]["filename"], "asset-000001.pdf")
            self.assertNotIn("content", json.dumps(assets))

    def test_visit_only_ignores_history_lookback_and_dates_remain_separate(self) -> None:
        case = VisitCase(MRN, date(2026, 1, 2), "O", "C1", "70", "Eye")

        class VisitSDK:
            def __init__(self):
                self.records = self
                self.orders = self
                self.medications = self

            def get_visit_cases(self, _mrn):
                return [case]

            def get_soap(self, selected):
                return SoapRecord(selected, ())

            def get_numeric_report(self, selected):
                return NumericReport(selected, ())

            def get_case_orders(self, _case):
                return []

            def get_case_medications(self, _case):
                return []

            def get_consults(self, _case):
                return []

            def get_treatments(self, _case):
                return []

        with tempfile.TemporaryDirectory() as temp_dir:
            result = export_patient_records(
                VisitSDK(),  # type: ignore[arg-type]
                mrns=(MRN,),
                output_dir=Path(temp_dir) / "records",
                include="visits",
                lookback=999999,
                visit_filter=VisitFilter(all_sections=True),
                visit_date=date(2026, 1, 2),
                order_date=date(2026, 1, 1),
            )
        self.assertEqual(result.case_count, 1)
        self.assertIsNone(result.history_path)

    def test_cli_visit_and_order_dates_are_independent(self) -> None:
        args = build_parser().parse_args(
            [
                "patient-records",
                "--mrn",
                MRN,
                "--output",
                "records",
                "--include",
                "all",
                "--visit-date",
                "2026-01-02",
                "--order-date",
                "2026-01-01",
                "--download-assets",
                "--all-matching-orders",
            ]
        )
        _validate_cli_arguments(args)
        visit_filter = _patient_records_visit_filter(args)
        self.assertTrue(visit_filter.all_sections)
        self.assertEqual(visit_filter.start_date, date(2026, 1, 2))
        self.assertEqual(args.order_date, date(2026, 1, 1))

    def test_order_date_is_not_applied_to_medication_history(self) -> None:
        captured = {}

        class HistorySDK:
            def __init__(self):
                self.records = self
                self.orders = self
                self.medications = self

            def get_order_history(self, _mrn, history_filter):
                captured["order"] = history_filter
                return []

            def get_medication_history(self, _mrn, history_filter):
                captured["medication"] = history_filter
                return []

            def get_numeric_history(self, _mrn, _history_filter):
                return NumericHistoryReport(MRN, ())

            def get_surgery_history(self, _mrn, _history_filter):
                return []

        selected_date = date(2026, 1, 1)
        with tempfile.TemporaryDirectory() as temp_dir:
            export_patient_records(
                HistorySDK(),  # type: ignore[arg-type]
                mrns=(MRN,),
                output_dir=Path(temp_dir) / "records",
                order_date=selected_date,
            )
        self.assertEqual(captured["order"].order_date, selected_date)
        self.assertIsNone(captured["medication"].order_date)

    def test_authentication_failure_stops_asset_batch(self) -> None:
        order = self._orders()[1]
        pdf = PdfAttachmentRef(MRN, f"//hfs02_1A0/REPORT/74/{MRN}/a.pdf")

        class FakeSDK:
            orders = None
            medications = None
            records = None

            def __init__(self):
                self.orders = self
                self.medications = self
                self.records = self

            def get_order_history(self, *_):
                return [order]

            def get_medication_history(self, *_):
                return []

            def get_numeric_history(self, *_):
                return NumericHistoryReport(MRN, ())

            def get_surgery_history(self, *_):
                return []

            def get_order_report(self, ref):
                return OrderReport(ref, {"x": "y"}, (pdf,), ())

            def download_pdf(self, _ref):
                raise AuthenticationError("expired")

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            self.assertRaises(AuthenticationError),
        ):
            export_patient_records(
                FakeSDK(),  # type: ignore[arg-type]
                mrns=(MRN,),
                output_dir=Path(temp_dir) / "records",
                download_assets=True,
                asset_terms=("microsonography",),
            )


if __name__ == "__main__":
    unittest.main()
