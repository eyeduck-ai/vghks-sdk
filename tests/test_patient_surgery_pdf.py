from __future__ import annotations

import html
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, quote, unquote, urlsplit

import requests
from bs4 import BeautifulSoup

from vghks_sdk.adapters.prq import PrqAdapter
from vghks_sdk.core.errors import ConfigurationError, ParseError
from vghks_sdk.core.readiness import make_auth_report
from vghks_sdk.live.atomic import _classify, _counts, _query_inputs, run_atomic_test
from vghks_sdk.live.config import LiveTestConfig
from vghks_sdk.models import AuthCheckTarget, BinaryAsset, PdfAttachmentRef
from vghks_sdk.parsing.clinical import parse_surgery_history
from vghks_sdk.queries import query_spec
from vghks_sdk.services.records import RecordsService

ROOT = Path(__file__).resolve().parents[1]
MRN = "SYNTHETIC"
PDF = b"%PDF-1.4\nsynthetic test only\n%%EOF"
PATH = r"\\hfs01_1A0.vghks.gov.tw\\OPG\4\87\SYNTHETIC\TEST-REQUEST\20260920\opnote.pdf"


def button(path=PATH):
    expression = (
        "window.open('/PRQWeb/Page/JSP/showPDF.jsp?fileName='+"
        f"encodeURIComponent(encodeURIComponent({json.dumps(path)})), 'opNoteWin')"
    )
    return f'<input type="button" name="OPNoteBtn" onclick="{html.escape(expression, quote=True)}">'


def row(buttons, *, other="", day="2026-09-01"):
    return (
        f"<tr><td>{day}</td><td>Synthetic procedure</td><td>{buttons}</td>"
        f"<td>{other}</td><td></td><td></td><td></td><td></td></tr>"
    )


def page(*rows):
    return "<table><tr>" + "<th>field</th>" * 8 + "</tr>" + "".join(rows) + "</table>"


class PatientSurgeryPdfTests(unittest.TestCase):
    def test_multiple_buttons_and_same_date_procedure_do_not_drop_records(self):
        second = PATH.replace("TEST-REQUEST", "TEST-SECOND")
        rows = parse_surgery_history(
            page(row(button() + button(second) + button()), row(button(second))), mrn=MRN
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual([r.file_path for r in rows[0].surgery_record_refs], [PATH, second])
        self.assertEqual(rows[1].surgery_record_refs[0].file_path, second)
        self.assertFalse(rows[0].surgery_record_issues)

    def test_no_button_and_other_document_columns_are_not_operation_pdfs(self):
        rows = parse_surgery_history(page(row("-", other=button())), mrn=MRN)
        self.assertFalse(rows[0].surgery_record_available)
        self.assertTrue(rows[0].anesthesia_record_available)
        self.assertFalse(rows[0].surgery_record_refs)
        self.assertFalse(rows[0].surgery_record_issues)
        self.assertEqual(parse_surgery_history(page(), mrn=MRN), [])

    def test_bad_or_unknown_button_is_reported_and_valid_buttons_survive(self):
        source = page(
            row(
                button(PATH.replace(MRN, "WRONG"))
                + button("http://[")
                + '<input type="button" onclick="openDynamicReport()">'
                + button()
            )
        )
        rows = parse_surgery_history(source, mrn=MRN)
        self.assertEqual(len(rows[0].surgery_record_refs), 1)
        self.assertEqual(
            set(rows[0].surgery_record_issues),
            {"PDF_REF_MRN_MISMATCH", "SURGERY_PDF_BUTTON_UNSUPPORTED"},
        )
        self.assertEqual(_counts(rows)["surgery_pdf_issue_count"], 2)
        self.assertEqual(_classify(rows), "ERROR")

    def test_reference_validation_and_encoded_direct_link(self):
        for value in (
            PATH.replace("hfs01_1A0.vghks.gov.tw", "unrecorded.test"),
            PATH.replace("OPG", "UNKNOWN"),
            PATH.replace("opnote.pdf", "..\\opnote.pdf"),
            PATH.replace(MRN, "OTHER"),
        ):
            with self.assertRaises(ConfigurationError):
                PdfAttachmentRef(MRN, value)
        link = "/PRQWeb/Page/JSP/showPDF.jsp?fileName=" + quote(quote(PATH, safe=""), safe="")
        rows = parse_surgery_history(page(row(f'<a href="{link}">PDF</a>')), mrn=MRN)
        self.assertEqual(rows[0].surgery_record_refs, (PdfAttachmentRef(MRN, PATH),))

    def test_download_reuses_prq_current_hid_and_recorded_double_encoding(self):
        runtime = SimpleNamespace(
            auth=SimpleNamespace(hid_for=Mock(return_value="CURRENT-HID")),
            settings=SimpleNamespace(prq_base_url="https://internal.test/PRQWeb"),
            execute=lambda _spec, action, **_kwargs: action(),
            request_binary=Mock(return_value=(PDF, "application/pdf")),
        )
        service = RecordsService(PrqAdapter(runtime))
        ref = parse_surgery_history(page(row(button())), mrn=MRN)[0].surgery_record_refs[0]
        self.assertEqual(service.download_surgery_record(ref).content, PDF)
        args, kwargs = runtime.request_binary.call_args
        self.assertEqual(args[0].key, "prq.pdf_attachment")
        self.assertTrue(args[1].endswith("/PRQWeb/Page/JSP/showPDF.jsp"))
        prepared = requests.Request("GET", args[1], params=kwargs["params"]).prepare()
        query = parse_qs(urlsplit(prepared.url).query)
        self.assertEqual(query["hid"], ["CURRENT-HID"])
        self.assertEqual(query["hhisnum"], [MRN])
        self.assertEqual(query["fileName"], [quote(PATH, safe="")])
        self.assertEqual(unquote(query["fileName"][0]), PATH)
        runtime.request_binary.return_value = (b"<html>viewer error</html>", "text/html")
        with self.assertRaises(ParseError):
            service.download_surgery_record(ref)

    def test_pdf_discovery_keeps_history_refs_when_other_sources_fail(self):
        rows = parse_surgery_history(page(row(button())), mrn=MRN)
        config = LiveTestConfig(
            profile="atomic", only_operations=("prq.surgery_history", "prq.pdf_attachment")
        )
        inputs = _query_inputs(
            query_spec("prq.pdf_attachment"),
            config,
            {"prq.surgery_history": [rows, rows]},
        )
        self.assertEqual(inputs, [{"ref": rows[0].surgery_record_refs[0]}])

    def test_live_query_keeps_partial_history_and_downloads_remaining_pdfs(self):
        rows = parse_surgery_history(
            page(
                row(
                    button()
                    + button(PATH.replace("TEST-REQUEST", "TEST-SECOND"))
                    + '<input type="button" onclick="unknown()">'
                )
            ),
            mrn=MRN,
        )
        auth = make_auth_report(
            tuple(AuthCheckTarget(k, (), "", False, 1, 0, (), "OK") for k in ("portal", "prq"))
        )
        sdk = SimpleNamespace(
            auth=SimpleNamespace(check=Mock(return_value=auth)),
            records=SimpleNamespace(get_surgery_history=Mock(return_value=rows)),
            orders=SimpleNamespace(
                download_pdf=Mock(
                    side_effect=[
                        ParseError("synthetic download error"),
                        BinaryAsset(PDF, "application/pdf"),
                    ]
                )
            ),
        )
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            result = run_atomic_test(
                sdk,
                LiveTestConfig(
                    profile="atomic",
                    only_operations=("prq.surgery_history", "prq.pdf_attachment"),
                    max_items=8,
                ),
                output_dir=Path(directory),
            )
            history = next(s for s in result.steps if s.operation == "prq.surgery_history")
            self.assertEqual(history.issue.code, "SURGERY_PDF_LINKS_INCOMPLETE")
            self.assertEqual(sdk.orders.download_pdf.call_count, 2)
            self.assertEqual(
                [s.status for s in result.steps if s.operation == "prq.pdf_attachment"],
                ["ERROR", "OK"],
            )
            self.assertTrue(
                (Path(directory) / "parsed/atomic/prq.pdf_attachment/0002.pdf").exists()
            )


@unittest.skipUnless(os.environ.get("VGHKS_RUN_HAR_CONTRACT") == "1", "opt-in HAR fixtures")
class PatientSurgeryHarTests(unittest.TestCase):
    def test_original_patient_har_contains_nine_usable_button_references(self):
        source = ROOT / "data/recordings/2026-09-19/進入病人檔案中的各種資料查詢.har"
        if not source.exists():
            self.skipTest("local HAR unavailable")
        entries = json.loads(source.read_text(encoding="utf-8-sig"))["log"]["entries"]
        counts = []
        with patch("requests.sessions.Session.request", side_effect=AssertionError("offline only")):
            for entry in entries:
                url = urlsplit(entry["request"]["url"])
                if url.path != "/PRQWeb/QueryOpNote.do":
                    continue
                mrn = parse_qs(url.query)["hhisnum"][0]
                source = entry["response"]["content"]["text"]
                rows = parse_surgery_history(source, mrn=mrn)
                expected = len(
                    BeautifulSoup(source, "html.parser").select('input[name="OPNoteBtn"]')
                )
                actual = sum(len(r.surgery_record_refs) for r in rows)
                self.assertEqual(actual, expected)
                self.assertTrue(all(not r.surgery_record_issues for r in rows))
                counts.append((len(rows), actual))
        self.assertEqual(counts, [(9, 9)])


if __name__ == "__main__":
    unittest.main()
