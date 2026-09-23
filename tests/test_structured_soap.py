from __future__ import annotations

import json
import os
import unittest
from dataclasses import replace
from datetime import date
from html import escape
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bs4 import BeautifulSoup

import vghks_sdk
from vghks_sdk.adapters.prq import PrqAdapter
from vghks_sdk.contracts.har import load_har
from vghks_sdk.core.errors import ConfigurationError, ParseError
from vghks_sdk.models import SoapRecord, VisitCase, to_jsonable
from vghks_sdk.offline.replay import replay_response
from vghks_sdk.parsing.prq import parse_soap
from vghks_sdk.services.records import RecordsService

CASE = VisitCase("00000000", date(2026, 1, 2), "O", "SYNTHETIC", "00", "測試科")
MEDICATION_HEADER = "藥" + "\u3000" * 12 + "  名\u3000劑量\u3000 單位 途徑 頻次   天數  總發藥量"
ORDER_HEADER = "檢查驗項目" + " " * 26 + "數量"


def pre(text: str) -> str:
    return f'<div class="soap"><pre>{escape(text)}</pre></div>'


def medication(name: str = "SYNTHETIC DROP", *, chinese: bool = False) -> str:
    # Public synthetic data, using the displayed column widths seen in HAR.
    padding = 32 - len(name) - (sum(ord(c) > 127 for c in name) if chinese else 0)
    return name + " " * padding + f"{'1':<7}{'DROP':<5}{'OU':<5}{'BID':<7}{'7':<6}1.00"


def order(name: str, quantity: str) -> str:
    return f"{name:<36}{quantity}"


def page() -> str:
    return (
        '<div class="soap"><pre>OUTSIDE_SENTINEL</pre></div><div id="data"><table>'
        f"<tr><td>{pre('synthetic vital signs')}</td></tr><tr><td><table>"
        f"<tr><td>S:</td><td>{pre('synthetic complaint')}</td></tr>"
        f"<tr><td>O:</td><td>{pre('synthetic examination')}</td></tr>"
        f'<tr><td rowspan="3">A+P:</td><td>{pre("synthetic assessment")}</td></tr>'
        f"<tr><td>{pre('synthetic plan')}</td></tr><tr><td>{pre('')}</td></tr>"
        "</table></td></tr><tr><td>"
        + pre("ICD碼：Z00.00   Synthetic encounter\n       V00.0    Synthetic second diagnosis")
        + "</td></tr><tr><td>"
        + pre(MEDICATION_HEADER + "\n" + medication())
        + "</td></tr><tr><td>"
        + pre(
            ORDER_HEADER
            + "  "
            + ORDER_HEADER
            + "\n"
            + order("SYNTHETIC IMAGE A", "1.00")
            + " "
            + order("SYNTHETIC IMAGE B", "2.00")
        )
        + '</td></tr></table><script>var ignored = "SCRIPT_SENTINEL";</script></div>'
    )


class StructuredSoapTests(unittest.TestCase):
    def test_labelled_page_preserves_raw_and_extracts_every_summary(self):
        record = parse_soap(page(), CASE)
        self.assertEqual(record.subjective, "synthetic complaint")
        self.assertEqual(record.objective, "synthetic examination")
        self.assertEqual(record.assessment_plan, "synthetic assessment\n\nsynthetic plan")
        self.assertIsNone(record.assessment)
        self.assertIsNone(record.plan)
        self.assertEqual(
            record.present_sections, ("S", "O", "AP", "DIAGNOSES", "MEDICATIONS", "ORDERS")
        )
        self.assertEqual(
            [(d.code, d.name, d.coding_system) for d in record.diagnoses],
            [
                ("Z00.00", "Synthetic encounter", "ICD"),
                ("V00.0", "Synthetic second diagnosis", "ICD"),
            ],
        )
        self.assertEqual(
            [(o.name, o.quantity) for o in record.orders],
            [
                ("SYNTHETIC IMAGE A", "1.00"),
                ("SYNTHETIC IMAGE B", "2.00"),
            ],
        )
        drug = record.medications[0]
        self.assertEqual(
            (
                drug.name,
                drug.dose,
                drug.unit,
                drug.route,
                drug.frequency,
                drug.days,
                drug.total_quantity,
            ),
            ("SYNTHETIC DROP", "1", "DROP", "OU", "BID", "7", "1.00"),
        )
        self.assertEqual(drug.raw_text, medication())
        self.assertEqual(record.unclassified_blocks, ("synthetic vital signs",))
        self.assertEqual(record.parsing_issues, ())
        self.assertEqual(len(record.blocks), 8)
        self.assertEqual(record.full_text, "\n\n".join(record.blocks))
        self.assertNotIn("SENTINEL", record.full_text)

    def test_medication_table_beneath_prescription_notice_is_not_lost(self):
        notice = (
            "慢性病連續處方箋處方　服藥期限：2026/09/21 ∼ 2026/12/14  "
            "◆健保號碼：SYNTHETIC"
        )
        source = '<div id="data">' + pre(
            notice + "\n" + MEDICATION_HEADER + "\n" + medication()
        ) + "</div>"
        record = parse_soap(source, CASE)
        self.assertEqual(len(record.medications), 1)
        self.assertIn("MEDICATIONS", record.present_sections)
        self.assertEqual(record.unclassified_blocks, (notice,))
        self.assertEqual(record.parsing_issues, ())
        self.assertIn(notice, record.blocks[0])
        period = record.chronic_prescription_periods[0]
        self.assertEqual((period.start_date, period.end_date), (date(2026, 9, 21), date(2026, 12, 14)))
        self.assertEqual((period.source_block_index, period.source_label), (0, "服藥期限"))
        self.assertEqual(period.raw_text, notice)
        self.assertIsInstance(period, vghks_sdk.SoapChronicPrescriptionPeriod)
        self.assertEqual(to_jsonable(period)["start_date"], "2026-09-21")

    def test_multiple_chronic_prescription_tables_keep_source_block(self):
        notices = (
            "慢性病連續處方箋處方 服藥期限:2026-09-21~2026-10-20",
            "慢性病連續處方箋處方 服藥期限:2026-10-21至2026-11-20",
        )
        source = '<div id="data">' + pre("synthetic note")
        source += "".join(pre(line + "\n" + MEDICATION_HEADER + "\n" + medication()) for line in notices)
        record = parse_soap(source + "</div>", CASE)
        self.assertEqual([period.source_block_index for period in record.chronic_prescription_periods], [1, 2])
        self.assertEqual([period.start_date for period in record.chronic_prescription_periods], [date(2026, 9, 21), date(2026, 10, 21)])
        self.assertEqual(len(record.medications), 2)
        self.assertEqual(record.parsing_issues, ())

    def test_malformed_chronic_dates_are_reported_without_losing_medications(self):
        notices = (
            "慢性病連續處方箋處方 服藥期限:2026/09/21 ∼ 2026/13/14",
            "慢性病連續處方箋處方 服藥期限:2026/12/14 ∼ 2026/09/21",
            "慢性病連續處方箋處方 服藥期限:unrecognized",
        )
        for notice in notices:
            with self.subTest(notice=notice):
                source = '<div id="data">' + pre(
                    notice + "\n" + MEDICATION_HEADER + "\n" + medication()
                ) + "</div>"
                record = parse_soap(source, CASE)
                self.assertEqual(len(record.medications), 1)
                self.assertEqual(record.chronic_prescription_periods, ())
                self.assertEqual(record.unclassified_blocks, (notice,))
                self.assertEqual(record.parsing_issues, (
                    "SOAP_CHRONIC_PRESCRIPTION_PERIOD_UNRECOGNIZED",
                ))

    def test_labels_can_be_reordered_empty_or_missing_without_shifting(self):
        source = '<div id="data"><table><tbody>'
        source += "<tr><th> Ａ ＋ Ｐ ： </th><td>" + pre("plan first") + "</td></tr>"
        source += "<tr><th>S:</th><td>" + pre("\r\n  \r\n") + "</td></tr>"
        source += "<tr><td>Unrecognised:</td><td>" + pre("kept verbatim") + "</td></tr>"
        source += "</tbody></table></div>"
        record = parse_soap(source, CASE)
        self.assertEqual(record.subjective, "")
        self.assertIsNone(record.objective)
        self.assertEqual(record.assessment_plan, "plan first")
        self.assertEqual(record.unclassified_blocks, ("kept verbatim",))
        self.assertEqual(record.present_sections, ("AP", "S"))

    def test_rowspan_stops_at_its_own_boundary_and_nested_tables(self):
        source = (
            '<div id="data"><table><tr><td rowspan="2">A+P:</td><td>'
            + pre("first")
            + "</td></tr><tr><td>"
            + pre("second")
            + "<table><tr><td>O:</td><td>"
            + pre("nested examination")
            + "</td></tr></table></td></tr><tr><td>"
            + pre("unlabelled tail")
            + "</td></tr></table></div>"
        )
        record = parse_soap(source, CASE)
        self.assertEqual(record.assessment_plan, "first\n\nsecond")
        self.assertEqual(record.objective, "nested examination")
        self.assertEqual(record.unclassified_blocks, ("unlabelled tail",))

    def test_standalone_assessment_and_plan_do_not_invent_combined_label(self):
        source = (
            '<div id="data">'
            + pre("A: assessment")
            + pre("P: plan\nS: quoted within plan")
            + "</div>"
        )
        record = parse_soap(source, CASE)
        self.assertEqual(record.assessment, "assessment")
        self.assertEqual(record.plan, "plan\nS: quoted within plan")
        self.assertIsNone(record.assessment_plan)
        self.assertIsNone(record.subjective)

    def test_repeated_sections_are_retained_in_source_order(self):
        record = parse_soap('<div id="data">' + pre("S: first") + pre("S: second") + "</div>", CASE)
        self.assertEqual(record.subjective, "first\n\nsecond")
        self.assertEqual(record.present_sections, ("S",))

    def test_diagnosis_versions_wrapping_duplicates_and_unknown_rows(self):
        text = "ICD-10-CM：Z00.00 Synthetic\n         wrapped description\nZ00.00 Duplicate\n??? unknown\n999.0 Later"
        record = parse_soap('<div id="data">' + pre(text) + "</div>", CASE)
        self.assertEqual([d.code for d in record.diagnoses], ["Z00.00", "Z00.00", "999.0"])
        self.assertEqual(record.diagnoses[0].name, "Synthetic\nwrapped description")
        self.assertEqual(record.diagnoses[0].coding_system, "ICD-10-CM")
        self.assertEqual(record.parsing_issues, ("SOAP_DIAGNOSIS_ROW_UNRECOGNIZED",))
        self.assertIn("??? unknown", record.full_text)

    def test_unknown_indented_code_does_not_become_previous_diagnosis_name(self):
        record = parse_soap(
            '<div id="data">' + pre("ICD碼：Z00.00 Synthetic\n   H??.9 Unknown") + "</div>", CASE
        )
        self.assertEqual(record.diagnoses[0].name, "Synthetic")
        self.assertEqual(record.parsing_issues, ("SOAP_DIAGNOSIS_ROW_UNRECOGNIZED",))

    def test_diagnosis_padding_is_only_retained_in_raw_text(self):
        source = '<div id="data">' + pre("ICD碼：Z00.00   Synthetic padded name    ") + "</div>"
        record = parse_soap(source, CASE)
        self.assertEqual(record.diagnoses[0].name, "Synthetic padded name")
        self.assertTrue(record.diagnoses[0].raw_text.endswith("    "))

    def test_numbers_inside_order_names_are_not_quantities(self):
        text = ORDER_HEADER + "\n" + order("SYNTHETIC TEST  24 HR", "2.00")
        record = parse_soap('<div id="data">' + pre(text) + "</div>", CASE)
        self.assertEqual(
            [(o.name, o.quantity) for o in record.orders], [("SYNTHETIC TEST  24 HR", "2.00")]
        )
        self.assertEqual(record.parsing_issues, ())

    def test_single_and_double_order_columns_keep_quantities(self):
        text = (
            ORDER_HEADER
            + "  "
            + ORDER_HEADER
            + "\n"
            + order("A", "1.00")
            + " "
            + order("B", "2.00")
        )
        text += "\n" + order("C", "3.00")
        source = (
            '<div id="data">' + pre(text) + pre(ORDER_HEADER + "\n" + order("D", "4.00")) + "</div>"
        )
        record = parse_soap(source, CASE)
        self.assertEqual(
            [(o.name, o.quantity) for o in record.orders],
            [("A", "1.00"), ("B", "2.00"), ("C", "3.00"), ("D", "4.00")],
        )

    def test_malformed_order_keeps_good_rows_and_original_text(self):
        text = (
            ORDER_HEADER
            + "\n"
            + order("A", "1.00")
            + "\nSYNTHETIC BROKEN ROW\n"
            + order("B", "2.00")
        )
        record = parse_soap('<div id="data">' + pre(text) + "</div>", CASE)
        self.assertEqual([o.name for o in record.orders], ["A", "B"])
        self.assertEqual(record.parsing_issues, ("SOAP_ORDER_ROW_UNRECOGNIZED",))
        self.assertIn("SYNTHETIC BROKEN ROW", record.full_text)

    def test_wide_characters_in_medications_and_malformed_rows(self):
        text = MEDICATION_HEADER + "\n" + medication("合成藥品", chinese=True)
        text += "\nSYNTHETIC BROKEN ROW\n" + medication()
        record = parse_soap('<div id="data">' + pre(text) + "</div>", CASE)
        self.assertEqual([m.name for m in record.medications], ["合成藥品", "SYNTHETIC DROP"])
        self.assertEqual([m.route for m in record.medications], ["OU", "OU"])
        self.assertEqual(record.parsing_issues, ("SOAP_MEDICATION_ROW_UNRECOGNIZED",))

    def test_changed_summary_headers_are_reported_not_silently_empty(self):
        for text, issue in [
            ("藥 名 劑量 新欄位", "SOAP_MEDICATION_HEADER_UNRECOGNIZED"),
            ("檢查驗項目 新欄位", "SOAP_ORDER_HEADER_UNRECOGNIZED"),
        ]:
            with self.subTest(issue=issue):
                record = parse_soap('<div id="data">' + pre(text) + "</div>", CASE)
                self.assertEqual(record.parsing_issues, (issue,))
                self.assertEqual(record.unclassified_blocks, (text,))

    def test_empty_summaries_and_absent_summaries_are_distinguishable(self):
        source = (
            '<div id="data">'
            + pre("ICD碼：")
            + pre(MEDICATION_HEADER)
            + pre(ORDER_HEADER)
            + "</div>"
        )
        record = parse_soap(source, CASE)
        self.assertEqual(record.present_sections, ("DIAGNOSES", "MEDICATIONS", "ORDERS"))
        self.assertEqual((record.diagnoses, record.medications, record.orders), ((), (), ()))
        self.assertEqual(record.parsing_issues, ())
        self.assertEqual(parse_soap('<div id="data"></div>', CASE).present_sections, ())

    def test_unknown_layout_is_not_a_valid_empty_response(self):
        for source, code in [
            ("<div>LOGIN</div>", "PRQ_SOAP_CONTAINER_MISSING"),
            (
                '<div id="data"><article>new layout</article></div>',
                "PRQ_SOAP_STRUCTURE_UNRECOGNIZED",
            ),
        ]:
            with self.subTest(code=code), self.assertRaises(ParseError) as raised:
                parse_soap(source, CASE)
            self.assertEqual(raised.exception.info.code, code)
        self.assertEqual(parse_soap('<div id="data">查無資料!</div>', CASE).blocks, ())

    def test_unlabelled_legacy_text_and_constructor_remain_compatible(self):
        text = "\r\n  ＳＹＮＴＨＥＴＩＣ  \r\n"
        record = parse_soap('<div id="data">' + pre(text) + "</div>", CASE)
        self.assertEqual(record.blocks, ("\n  ＳＹＮＴＨＥＴＩＣ  \n",))
        self.assertEqual(record.parsing_issues, ("SOAP_SECTIONS_UNRECOGNIZED",))
        self.assertIsNone(record.subjective)
        self.assertEqual(SoapRecord(CASE, ("legacy",)).full_text, "legacy")

    def test_public_models_and_json_include_structured_values(self):
        record = parse_soap(page(), CASE)
        payload = to_jsonable(record)
        self.assertEqual(payload["subjective"], "synthetic complaint")
        self.assertEqual(payload["diagnoses"][0]["code"], "Z00.00")
        self.assertEqual(payload["orders"][0]["quantity"], "1.00")
        self.assertIsInstance(record.diagnoses[0], vghks_sdk.SoapDiagnosis)
        self.assertIsInstance(record.orders[0], vghks_sdk.SoapOrder)
        self.assertIsInstance(record.medications[0], vghks_sdk.SoapMedication)
        json.dumps(payload, ensure_ascii=False)

    def test_offline_replay_exposes_only_structure_and_counts(self):
        with patch("socket.socket", side_effect=AssertionError("no network")):
            result = replay_response("prq.soap", page().encode(), {"hhisnum": CASE.mrn})
        self.assertEqual(result["status"], "PARSED")
        self.assertEqual(result["soap_diagnosis_count"], 2)
        self.assertEqual(result["soap_order_count"], 2)
        self.assertEqual(result["soap_medication_count"], 1)
        self.assertEqual(result["soap_chronic_prescription_period_count"], 0)
        self.assertEqual(result["soap_parsing_issues"], [])
        chronic_page = page().replace(
            MEDICATION_HEADER,
            "慢性病連續處方箋處方 服藥期限:2026/09/21~2026/12/14\n"
            + MEDICATION_HEADER,
            1,
        )
        chronic_result = replay_response(
            "prq.soap", chronic_page.encode(), {"hhisnum": CASE.mrn}
        )
        self.assertEqual(chronic_result["soap_chronic_prescription_period_count"], 1)
        self.assertNotIn("2026-09-21", json.dumps(chronic_result))
        for secret in [
            CASE.mrn,
            "Z00.00",
            "Synthetic encounter",
            "synthetic complaint",
            "SYNTHETIC DROP",
        ]:
            self.assertNotIn(secret, json.dumps(result))

    def test_service_uses_same_atom_context_and_no_extra_order_requests(self):
        class Runtime:
            auth = SimpleNamespace(hid_for=lambda app: "SYNTHETIC-HID")
            settings = SimpleNamespace(prq_base_url="https://example.test/PRQWeb")

            def __init__(self):
                self.calls = []

            def execute(self, spec, operation, *, operation_name):
                self.operation = (spec.key, operation_name)
                return operation()

            def request_text(self, spec, url, **kwargs):
                self.calls.append((spec.key, kwargs))
                return {
                    "prq.case_detail": '<div id="tabs"></div>',
                    "prq.key_preflight": "ssID=s&keyOne=a&keyTwo=b&keyThree=c",
                    "prq.soap": page(),
                }[spec.key]

        runtime = Runtime()
        service = RecordsService(PrqAdapter(runtime))
        with patch("socket.socket", side_effect=AssertionError("no network")):
            record = service.get_soap(CASE)
        self.assertEqual(record.case, CASE)
        self.assertEqual(record.subjective, "synthetic complaint")
        self.assertEqual(runtime.operation, ("prq.soap", "get_soap"))
        self.assertEqual(
            [key for key, _ in runtime.calls], ["prq.case_detail", "prq.key_preflight", "prq.soap"]
        )
        self.assertEqual(runtime.calls[-1][1]["params"]["caseNo"], CASE.case_no)
        self.assertEqual(runtime.calls[-1][1]["params"]["hhisnum"], CASE.mrn)
        with self.assertRaises(ConfigurationError):
            service.get_soap(replace(CASE, case_type="A"))
        self.assertEqual(len(runtime.calls), 3)


@unittest.skipUnless(os.getenv("VGHKS_RUN_HAR_CONTRACT") == "1", "private HAR is opt-in")
class StructuredSoapHarTests(unittest.TestCase):
    def test_existing_soap_bodies_retain_text_and_labelled_sections(self):
        root = Path(__file__).resolve().parents[1] / "data"
        files = [*root.glob("har/*.har"), *root.glob("recordings/*/*.har")]
        if not files:
            self.skipTest("private HAR unavailable")
        count = 0
        structured = 0
        with patch("socket.socket", side_effect=AssertionError("no network")):
            for path in files:
                for entry in load_har(path).entries:
                    if (
                        not entry.path.endswith("/QueryBillingSOAP.do")
                        or entry.response_status != 200
                        or not entry.has_body
                    ):
                        continue
                    record = parse_soap(entry.text(), CASE)
                    soup = BeautifulSoup(entry.text(), "html.parser")
                    expected = tuple(
                        text
                        for node in soup.select("#data .soap pre")
                        if (
                            text := node.get_text("", strip=False)
                            .replace("\r\n", "\n")
                            .replace("\r", "\n")
                        ).strip()
                    )
                    self.assertTrue(record.blocks == expected, "legacy SOAP text changed")
                    structured += {"S", "O", "AP"}.issubset(record.present_sections) and bool(
                        record.diagnoses
                    )
                    self.assertFalse(record.parsing_issues, "recorded SOAP has unrecognized rows")
                    count += 1
        self.assertGreater(count, 0)
        self.assertGreater(structured, 0)


if __name__ == "__main__":
    unittest.main()
