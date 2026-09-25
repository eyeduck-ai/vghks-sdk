from __future__ import annotations

import os
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urljoin

from test_auth import FakeResponse, ScriptedTransport

from vghks_sdk import PortalCredentials, SDKSettings
from vghks_sdk.adapters.auth import AuthenticationAdapter
from vghks_sdk.adapters.prq_extensions import parse_text_history
from vghks_sdk.core.errors import AuthenticationError, ParseError
from vghks_sdk.live.atomic import _query_inputs
from vghks_sdk.live.config import LiveTestConfig
from vghks_sdk.models import OrderReportRef, OutpatientPatient, VisitCase
from vghks_sdk.offline.analyze import inspect_bundle
from vghks_sdk.offline.bundle import BundleReader
from vghks_sdk.parsing.assets import parse_binary_asset
from vghks_sdk.parsing.clinical import parse_order_report
from vghks_sdk.parsing.prq import parse_opd_patients
from vghks_sdk.queries import QUERY_BY_KEY
from vghks_sdk.workflows.opd_soap import classify_opd_registration, select_registration_visits

DAY = date(2026, 9, 15)
RETURN_NAME = "vghks-live-test-20260919-215328-9fd4db8c-COMPLETED_WITH_ERRORS.zip"
ROOT = Path(__file__).resolve().parents[1]
RETURN = next(
    (p for p in (ROOT / "dist" / RETURN_NAME, ROOT / "data/returns" / RETURN_NAME) if p.is_file()),
    ROOT / "dist" / RETURN_NAME,
)


def auth_for(transport=None):
    return AuthenticationAdapter(
        settings=SDKSettings(),
        credentials=PortalCredentials("SYNTHETIC", "synthetic-only"),
        transport=transport or ScriptedTransport(),
    )


def sso_form(target):
    return (
        '<form action="https://zwmc01p.vghks.gov.tw:4430/OPPLWeb/WPSAutoLogon">'
        '<input name="HID" value="synthetic"><input name="ssID" value="test">'
        '<input name="keyOne" value="1"><input name="keyTwo" value="2">'
        '<input name="keyThree" value="3"><input name="uid" value="SYNTHETIC">'
        f'<input name="targetURL" value="{target}"></form>'
    )


class SeptemberFixTests(unittest.TestCase):
    def test_oppl_relative_target_is_validated_in_app_and_posted_once_unchanged(self):
        class RelativeTransport(ScriptedTransport):
            def request(self, method, url, **kwargs):
                response = super().request(method, url, **kwargs)
                if url.endswith("/ssoFromDn.do"):
                    return FakeResponse(url, sso_form("surgAction.do?method=surg"))
                if url.endswith("/WPSAutoLogon"):
                    return FakeResponse(urljoin(url, kwargs["data"]["targetURL"]), "application")
                return response

        transport = RelativeTransport()
        auth = auth_for(transport)
        auth._portal_authenticated = True
        session = auth.ensure("oppl")
        self.assertTrue(session.landing_url.endswith("/OPPLWeb/surgAction.do?method=surg"))
        posts = [c for c in transport.calls if c[1].endswith("/WPSAutoLogon")]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2]["data"]["targetURL"], "surgAction.do?method=surg")

    def test_relative_sso_support_never_allows_external_or_other_application_targets(self):
        auth = auth_for()
        for target in (
            "//outside.invalid/OPPLWeb/a",
            "http://zwmc01p.vghks.gov.tw:4430/OPPLWeb/a",
            "../SectOrdWeb/a",
            "%2e%2e/SectOrdWeb/a",
            "/unexpected/a",
        ):
            with self.subTest(target=target), self.assertRaises(AuthenticationError):
                auth._parse_sso_form(sso_form(target), auth.settings.profiles["oppl"])

    def test_scanner_pdf_marker_keeps_bytes_but_unknown_tails_and_html_fail(self):
        content = b"%PDF-1.3\nsynthetic\n%%EOF\r%Avision"
        asset = parse_binary_asset(content, media_type="application/pdf")
        self.assertEqual(asset.content, content)
        for body in (
            b"<embed type='application/pdf'>",
            b"%PDF-truncated",
            content + b"extra",
            b"%PDF-x\n%%EOF<script>",
        ):
            with self.subTest(body=body), self.assertRaises(ParseError):
                parse_binary_asset(body, media_type="application/pdf")

    def test_lab_table_and_short_result_are_data_without_capturing_metadata(self):
        reference = OrderReportRef("SYNTHETIC", "CASE", "O", "1")
        metadata = '<table class="Title"><tr><th>報告日期</th><td>2026-09-19</td></tr></table>'
        for body in (
            '<table class="labtable"><tr><th>檢驗項目</th><th>檢驗結果</th><th>單位</th><th>參考區間</th></tr><tr><td>Test</td><td><pre>1.25</pre></td><td>unit</td><td>0-2</td></tr></table>',
            '<div class="cmbSty"><pre>synthetic result</pre></div>',
        ):
            report = parse_order_report(
                f'<div id="data">{metadata}{body}</div>', reference=reference
            )
            self.assertEqual(report.report_data_status, "TEXT_AVAILABLE")
            self.assertNotIn("2026-09-19", report.report_text)
        empty_table = '<div id="data"><table class="labtable"><tr><th>檢驗項目</th><th>檢驗結果</th><th>單位</th><th>參考區間</th></tr></table></div>'
        self.assertEqual(
            parse_order_report(metadata + empty_table, reference=reference).report_data_status,
            "METADATA_ONLY",
        )

    def test_comprehensive_samples_each_report_department_instead_of_only_first_list(self):
        histories = []
        for dept in ("RAD", "CHK"):
            html = "".join(
                f'<a href="QueryResText.do?hhisnum=SYNTHETIC&caseNo=C{i}&caseType=O&seqNo={i}&orDept={dept}">Report</a>'
                for i in range(15)
            )
            histories.append(parse_text_history(html, "SYNTHETIC", dept))
        selected = _query_inputs(
            QUERY_BY_KEY["prq.text_report"],
            LiveTestConfig(profile="comprehensive"),
            {"prq.text_report_history": histories},
        )
        self.assertEqual(len(selected), 16)
        self.assertEqual({r["ref"].department for r in selected}, {"RAD", "CHK"})
        self.assertTrue(any(r["ref"].sequence_no == "14" for r in selected))

    def test_physician_identity_owns_any_section_and_blank_is_never_filled_from_query(self):
        html = "<script>aryOpdSec[0]='V1';aryOpdDoc[0]='DOC1F';aryOpdSec[1]='70';aryOpdDoc[1]='';</script><script>if(aryOpdSec[0]=='V1'){new KSCase('','1','TEST001','Synthetic','F','60');}</script><script>if(aryOpdSec[1]=='70'){new KSCase('','1','TEST002','Synthetic','F','60');}</script>"
        rows = parse_opd_patients(html, visit_date=DAY, doctor_card="DOC1")
        self.assertEqual(
            [classify_opd_registration(r, doctor_card="DOC1") for r in rows],
            ["DEDICATED", "SHARED"],
        )
        self.assertEqual(rows[1].doctor_card, "")
        own = rows[0]
        cases = [
            VisitCase(own.mrn, DAY, "O", "V1-CASE", "V1", "Synthetic"),
            VisitCase(own.mrn, DAY, "O", "OTHER-CASE", "70", "Synthetic"),
        ]
        selected = select_registration_visits(rows, cases, doctor_card="DOC1")
        self.assertEqual([c.case_no for c in selected], ["V1-CASE"])
        alias_case = VisitCase(
            "OLD001", DAY, "O", "ALIAS-CASE", "V1", "Synthetic", lookup_mrn=own.mrn
        )
        self.assertEqual(
            [c.case_no for c in select_registration_visits([own], [alias_case], doctor_card="DOC1")],
            ["ALIAS-CASE"],
        )
        self.assertEqual(classify_opd_registration(own, doctor_card="DOC2"), "UNCLASSIFIED")
        unknown = OutpatientPatient("UNKNOWN", "Synthetic", DAY, section_code="70")
        self.assertEqual(classify_opd_registration(unknown, doctor_card="DOC1"), "UNCLASSIFIED")
        explicit_71 = OutpatientPatient(
            "SYNTHETIC",
            "Synthetic",
            DAY,
            section_code="71",
            doctor_card="DOC1F",
            doctor_label_present=True,
        )
        self.assertEqual(classify_opd_registration(explicit_71, doctor_card="DOC1"), "DEDICATED")


@unittest.skipUnless(
    os.getenv("VGHKS_RUN_HAR_CONTRACT") == "1" and RETURN.is_file(),
    "Requires authorized local September return",
)
class SeptemberLiveRegression(unittest.TestCase):
    def test_recorded_return_reclassifies_without_network_or_rewriting_original_results(self):
        with patch("requests.sessions.Session.request") as network, BundleReader(RETURN) as reader:
            report, _ = inspect_bundle(reader)
            auth = auth_for()
            form = auth._parse_sso_form(
                reader.read("responses/000040.html").decode("utf-8"), auth.settings.profiles["oppl"]
            )
            self.assertEqual(form.payload["targetURL"], "surgAction.do?method=surg")
            histories = [
                parse_text_history(
                    (o := reader.json(f"parsed/atomic/prq.text_report_history/{i:04d}.json"))[
                        "document"
                    ]["html"],
                    o["mrn"],
                    o["department"],
                )
                for i in (2, 3)
            ]
            selected = _query_inputs(
                QUERY_BY_KEY["prq.text_report"],
                LiveTestConfig(profile="comprehensive"),
                {"prq.text_report_history": histories},
            )
            self.assertIn(histories[1].report_refs[6], [r["ref"] for r in selected])
        network.assert_not_called()
        pdf = next(r for r in report["operations"] if r["operation"] == "prq.pdf_attachment")
        self.assertEqual(pdf["live_status"], "FAILED")
        self.assertEqual(pdf["replay_parsed"], 8)
        self.assertEqual(pdf["replay_errors"], 0)
        self.assertEqual(report["report_content"]["live_steps"]["TEXT_AVAILABLE"], 10)
        self.assertEqual(report["report_content"]["offline_responses"]["TEXT_AVAILABLE"], 11)
        self.assertEqual(report["report_content"]["offline_responses"]["METADATA_ONLY"], 1)
        review = report["weekly_physician_review"]
        self.assertEqual(review["counts"], {"DEDICATED": 93, "SHARED": 262})
        self.assertEqual(review["dedicated_section_counts"], {"70": 84, "V1": 9})
        self.assertEqual(review["additional_registrations"], 9)
        self.assertEqual(review["additional_patients"], 9)
        self.assertEqual(review["dedicated_missing_mrn"], 3)
        self.assertEqual(report["weekly_opd_workflow"]["counts"]["dedicated_registrations"], 84)
        self.assertEqual(report["weekly_opd_workflow"]["counts"]["matches"], 12)


if __name__ == "__main__":
    unittest.main()
