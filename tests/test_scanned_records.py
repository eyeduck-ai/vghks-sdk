"""Synthetic scan references; no hospital response body is committed."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from datetime import date
from unittest.mock import Mock, patch

from vghks_sdk import PdfAttachmentRef, ScannedRecord, VisitCase
from vghks_sdk.adapters.prq_extensions import parse_upload_history
from vghks_sdk.core.errors import ParseError
from vghks_sdk.live.atomic import _query_inputs, build_test_plan
from vghks_sdk.live.config import LiveTestConfig
from vghks_sdk.live.presets import SCAN_RECORD_QUERIES, scan_record_round
from vghks_sdk.live_test_app import main
from vghks_sdk.parsing.scanned_records import parse_case_scanned_pdf_refs
from vghks_sdk.parsing.soap import parse_soap
from vghks_sdk.queries import query_spec
from vghks_sdk.services.records import RecordsService
from vghks_sdk.workflows import collect_ophthalmology_scans

MRN = "SYN001"
ROOT = "//HFS01_3A0.vghks.gov.tw/EMRU/8"


def scan_script(name: str, subtype: str) -> str:
    return (
        "<script>var filepath = encodeURIComponent('"
        + f"{ROOT}/{MRN}/{name}.pdf"
        + "'); var subtype = '"
        + subtype
        + "'; document.write('showPDF.jsp?fileName=' + encodeURIComponent(filepath));</script>"
    )


class ScannedRecordTests(unittest.TestCase):
    def test_workflow_joins_eye_case_links_to_record_history_without_guessing(self) -> None:
        history = parse_upload_history(
            '<table id="tbObj"><tr><th>Category</th></tr></table>'
            + scan_script("case", "RECORD")
            + scan_script("unclassified", "RECORD")
            + scan_script("eye", "OPG"),
            MRN,
        )
        first = VisitCase(MRN, date(2026, 9, 21), "O", "C1", "70", "眼科")
        second = VisitCase(MRN, date(2026, 9, 20), "O", "C2", "70", "眼科")
        other = VisitCase(MRN, date(2026, 9, 19), "O", "C3", "61", "皮膚科")
        case_ref = PdfAttachmentRef(MRN, f"{ROOT}/{MRN}/case.pdf")
        extra_ref = PdfAttachmentRef(MRN, f"{ROOT}/{MRN}/case-only.pdf")
        records = Mock(
            get_upload_history=Mock(return_value=history),
            get_visit_cases=Mock(return_value=[other, second, first]),
            get_case_scanned_records=Mock(
                side_effect=lambda case: (
                    ScannedRecord(None, case_ref),
                    ScannedRecord(None, case_ref),
                ) if case.case_no == "C1" else (ScannedRecord(None, extra_ref),)
            ),
        )
        result = collect_ophthalmology_scans(Mock(records=records), MRN)
        self.assertTrue(result.complete)
        self.assertEqual(result.eye_case_count, 2)
        self.assertEqual(result.checked_case_count, 2)
        self.assertEqual([row.record_type for row in result.scans], ["RECORD", "OPG", None])
        self.assertEqual(len(result.case_links), 2)
        self.assertEqual(len(result.unclassified_history), 1)
        self.assertEqual(result.unclassified_history[0].record_type, "RECORD")
        self.assertEqual(records.get_case_scanned_records.call_count, 2)

    def test_workflow_reports_partial_case_failure_and_bounded_sampling(self) -> None:
        history = parse_upload_history(
            '<table id="tbObj"><tr><th>Category</th></tr></table>'
            + scan_script("eye", "OPG"),
            MRN,
        )
        first = VisitCase(MRN, date(2026, 9, 21), "O", "C1", "70", "眼科")
        second = VisitCase(MRN, date(2026, 9, 20), "O", "C2", "70", "眼科")
        records = Mock(
            get_upload_history=Mock(return_value=history),
            get_visit_cases=Mock(return_value=[first, second]),
            get_case_scanned_records=Mock(
                side_effect=[ParseError("synthetic failure", code="SOAP_SCHEMA_CHANGED"), ()]
            ),
        )
        result = collect_ophthalmology_scans(Mock(records=records), MRN)
        self.assertFalse(result.complete)
        self.assertEqual(result.checked_case_count, 1)
        self.assertEqual([issue.error_code for issue in result.case_issues], ["SOAP_SCHEMA_CHANGED"])
        self.assertEqual([row.record_type for row in result.scans], ["OPG"])
        records.get_case_scanned_records.side_effect = lambda case: ()
        bounded = collect_ophthalmology_scans(Mock(records=records), MRN, max_cases=1)
        self.assertFalse(bounded.complete)
        self.assertEqual(bounded.eye_case_count, 2)
        self.assertEqual(bounded.checked_case_count, 1)

    def test_one_exe_default_uses_only_the_four_scan_queries(self) -> None:
        with (
            patch(
                "vghks_sdk.live_test_app.build_identity",
                return_value={"default_profile": "scans"},
            ),
            patch("vghks_sdk.live_test_app.run_live_test_namespace", return_value=0) as runner,
            patch("builtins.input", return_value=""),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main([]), 0)
        self.assertEqual(runner.call_args.args[0].profile, "scans")
        self.assertTrue(runner.call_args.args[0].bundled_scans)
        config = LiveTestConfig(**scan_record_round(), test_mrn=MRN)
        with patch("requests.sessions.Session.request") as network:
            plan = build_test_plan(config)
        self.assertEqual([row["key"] for row in plan["operations"]], list(SCAN_RECORD_QUERIES))
        self.assertEqual(plan["auth_targets"], ["prq"])
        self.assertEqual(plan["max_cases"], 6)
        self.assertEqual(plan["max_items_per_operation"], 4)
        self.assertEqual(
            plan["operations"][-1]["dependencies"], ["prq.upload_history", "prq.soap"]
        )
        network.assert_not_called()

    def test_case_soap_exposes_scanned_pdf_and_service_projection(self) -> None:
        case = VisitCase(MRN, date(2026, 9, 21), "O", "123", "70", "Eye")
        html = (
            '<div id="data"><div class="soap"><pre>S: synthetic note</pre></div></div>'
            + scan_script("case", "RECORD    ")
        )
        soap = parse_soap(html, case)
        self.assertEqual(len(soap.scanned_pdf_refs), 1)
        self.assertEqual(soap.scanned_pdf_refs[0].mrn, MRN)
        service = RecordsService(Mock(get_soap=Mock(return_value=soap)))
        self.assertEqual(
            service.get_case_scanned_records(case),
            (ScannedRecord(None, soap.scanned_pdf_refs[0]),),
        )
        inputs = _query_inputs(
            query_spec("prq.pdf_attachment"),
            LiveTestConfig(profile="atomic", only_operations=("prq.pdf_attachment",)),
            {"prq.soap": [soap]},
        )
        self.assertEqual(inputs, [{"ref": soap.scanned_pdf_refs[0]}])

    def test_history_keeps_source_types_and_selects_opg(self) -> None:
        html = (
            '<table id="tbObj"><tr><th>Category</th></tr></table>'
            + scan_script("ordinary", "RECORD    ")
            + scan_script("eye", "OPG       ")
        )
        history = parse_upload_history(html, MRN)
        self.assertEqual(len(history.pdf_refs), 2)
        self.assertEqual(
            [row.record_type for row in history.scanned_records], ["RECORD", "OPG"]
        )
        adapter = Mock(get_upload_history=Mock(return_value=history))
        service = RecordsService(adapter)
        selected = service.get_ophthalmology_scan_history(MRN)
        self.assertEqual(selected, history.scanned_records[1:])
        adapter.get_upload_history.assert_called_once_with(MRN, "", "*")
        case = VisitCase(MRN, date(2026, 9, 21), "O", "123", "70", "Eye")
        soap = parse_soap(
            '<div id="data"><div class="soap"><pre>S: test</pre></div></div>'
            + scan_script("case", "RECORD"),
            case,
        )
        inputs = _query_inputs(
            query_spec("prq.pdf_attachment"),
            LiveTestConfig(
                profile="atomic",
                only_operations=("prq.soap", "prq.upload_history", "prq.pdf_attachment"),
                max_items=2,
            ),
            {"prq.upload_history": [history], "prq.soap": [soap]},
        )
        self.assertEqual(
            inputs,
            [{"ref": history.scanned_records[1].pdf_ref}, {"ref": soap.scanned_pdf_refs[0]}],
        )
        scans = _query_inputs(
            query_spec("prq.pdf_attachment"),
            LiveTestConfig(profile="scans", test_mrn=MRN, max_items=2),
            {"prq.upload_history": [history], "prq.soap": [soap]},
        )
        self.assertEqual(scans, inputs)

    def test_empty_history_is_distinct_from_unknown_structure(self) -> None:
        empty = parse_upload_history('<table id="tbObj"><tr><th>Category</th></tr></table>', MRN)
        self.assertEqual(empty.scanned_records, ())
        self.assertEqual(empty.pdf_refs, ())
        with self.assertRaises(ParseError) as caught:
            parse_upload_history("<html>unrecognized page</html>", MRN)
        self.assertEqual(caught.exception.info.code, "UPLOAD_HISTORY_STRUCTURE_MISSING")

    def test_pdf_sources_remain_bound_to_patient_and_allowed_host(self) -> None:
        for source, expected in (
            (f"{ROOT}/OTHER001/scan.pdf", "PDF_REF_MRN_MISMATCH"),
            ("//outside.example/EMRU/8/SYN001/scan.pdf", "PDF_REF_EXTERNAL"),
        ):
            with self.subTest(expected=expected):
                html = (
                    '<div id="data"></div><script>var filepath = encodeURIComponent("'
                    + source
                    + '"); document.write("showPDF.jsp");</script>'
                )
                with self.assertRaises(ParseError) as caught:
                    parse_case_scanned_pdf_refs(html, MRN)
                self.assertEqual(caught.exception.info.code, expected)

    def test_comment_does_not_create_a_scan_reference(self) -> None:
        html = (
            '<div id="data"></div><script>// var filepath = encodeURIComponent("'
            + f"{ROOT}/{MRN}/fake.pdf"
            + '");\n document.write("showPDF.jsp");</script>'
        )
        self.assertEqual(parse_case_scanned_pdf_refs(html, MRN), ())
        self.assertIsInstance(PdfAttachmentRef(MRN, f"{ROOT}/{MRN}/valid.pdf"), PdfAttachmentRef)

    def test_string_example_is_ignored_and_callback_path_fails_closed(self) -> None:
        example = (
            '<div id="data"></div><script>var example = "var filepath = '
            'encodeURIComponent(\\\'fake.pdf\\\')"; document.write("showPDF.jsp");</script>'
        )
        self.assertEqual(parse_case_scanned_pdf_refs(example, MRN), ())
        callback = (
            '<div id="data"></div><script>function later() {'
            + f"var filepath = encodeURIComponent('{ROOT}/{MRN}/hidden.pdf');"
            + '} document.write("showPDF.jsp");</script>'
        )
        with self.assertRaises(ParseError) as caught:
            parse_case_scanned_pdf_refs(callback, MRN)
        self.assertEqual(caught.exception.info.code, "SCAN_PATH_UNSUPPORTED")

    def test_dynamic_pdf_expression_is_not_treated_as_empty(self) -> None:
        html = (
            '<div id="data"></div><script>var filepath = encodeURIComponent(pathFromServer);'
            'document.write("showPDF.jsp");</script>'
        )
        with self.assertRaises(ParseError) as caught:
            parse_case_scanned_pdf_refs(html, MRN)
        self.assertEqual(caught.exception.info.code, "SCAN_PATH_UNSUPPORTED")


if __name__ == "__main__":
    unittest.main()
