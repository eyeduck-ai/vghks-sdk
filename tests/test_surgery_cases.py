from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from test_webmaas import adapter_with, response

from vghks_sdk.adapters.auth import AppSession, AuthenticationAdapter
from vghks_sdk.adapters.surgery_cases import SurgeryCasesAdapter
from vghks_sdk.core.config import PortalCredentials, SDKSettings
from vghks_sdk.core.errors import ConfigurationError, ParseError, RequestError
from vghks_sdk.core.transport import SafeSessionTransport
from vghks_sdk.live.atomic import _classify, _query_inputs, build_test_plan
from vghks_sdk.live.config import LiveTestConfig, load_live_test_config, resolve_live_test_config
from vghks_sdk.models import BinaryAsset, SurgeryCaseFilter, SurgeryCaseRef, SurgeryNoteRef
from vghks_sdk.offline.replay import replay_response
from vghks_sdk.parsing.surgery_cases import (
    parse_surgery_cases,
    parse_surgery_departments,
    parse_surgery_note,
)
from vghks_sdk.queries import query_spec
from vghks_sdk.workflows import collect_surgery_records

ROOT = Path(__file__).resolve().parents[1]
REF = SurgeryCaseRef("SYNTHETIC", "TEST-REQUEST", "0")
PATH = r"\\hfs01_1A0.vghks.gov.tw\OPG\4\92\SYNTHETIC\TEST-REQUEST\20260901000000\opnote.pdf"
PDF = b"%PDF-1.4\nsynthetic-only\n%%EOF\n"


def row(ref=REF, day="2026-06-01"):
    return {
        "hhisnum": ref.mrn,
        "opreqno": ref.request_no,
        "opseqno": int(ref.sequence_no),
        "op_date": day,
        "op_name": "Synthetic procedure",
        "code1": "80416",
        "code2": "",
        "code3": None,
        "code4": "",
        "sect": "OPH",
        "opdoct1": "SYNTHETIC-DR",
        "opdoct1n": "Synthetic surgeon",
        "opdoct2": "ASSISTANT",
        "rpstatus": "68",
        "hnamec": "Synthetic patient",
        "blockHnamec": "Synt**",
        "hid": "MUST-NOT-ENTER-MODEL",
        "futureField": {"preserved": True},
    }


class SurgeryCaseTests(unittest.TestCase):
    def test_filter_maps_recorded_fields_without_losing_empty_assistant_positions(self):
        selector = SurgeryCaseFilter(
            supervising_card=" sup ",
            assistant_cards=("", "a2"),
            procedure_code="80416",
            period="2YB",
        )
        form = selector.to_form()
        self.assertEqual(form["guiDoctId"], "SUP")
        self.assertEqual((form["assDoct1"], form["assDoct2"], form["assDoct4"]), ("", "A2", ""))
        self.assertEqual((form["opdate"], form["opCode"], form["opDept"]), ("2YB", "80416", "ALL"))
        for values in (
            {},
            {"surgeon_card": "DR", "period": "all"},
            {"surgeon_card": "DR", "assistant_cards": "ABC"},
            {"surgeon_card": "DR", "procedure_code": "x&hid=y"},
        ):
            with self.subTest(values=values), self.assertRaises(ConfigurationError):
                SurgeryCaseFilter(**values)

    def test_case_data_preserves_all_fields_but_does_not_infer_pdf_from_status(self):
        cases = parse_surgery_cases({"oprmlist": [row(), row()]})
        self.assertEqual(len(cases), 1)
        case = cases[0]
        self.assertEqual(case.reference.sequence_no, "0")
        self.assertEqual(case.surgery_date, date(2026, 6, 1))
        self.assertEqual(case.procedure_codes, ("80416",))
        self.assertEqual(case.assistant_cards, ("ASSISTANT", "", "", ""))
        self.assertTrue(case.fields["futureField"]["preserved"])
        self.assertNotIn("hid", case.fields)
        self.assertEqual(parse_surgery_cases({"oprmlist": []}), [])
        self.assertEqual(
            parse_surgery_departments({"deptlist": ["OPH", "ORTH", "OPH"]}), ["OPH", "ORTH"]
        )

    def test_malformed_truncated_and_conflicting_cases_fail_explicitly(self):
        for value in (
            {},
            {"oprmlist": None},
            {"oprmlist": [{}]},
            {"oprmlist": [row(day="bad")]},
            {"oprmlist": [row()], "recordsTotal": 10},
            {"oprmlist": [row(), {**row(), "code1": "DIFFERENT"}]},
            {"oprmlist": [{**row(), "opseqno": False}]},
        ):
            with self.subTest(value=str(value)[:50]), self.assertRaises(ParseError):
                parse_surgery_cases(value)

    def test_note_availability_path_and_case_identity_are_separate(self):
        note = parse_surgery_note({"rtnYN": "Y", "path": PATH}, REF)
        self.assertEqual(note.reference, REF)
        self.assertIsNone(parse_surgery_note({"rtnYN": "N"}, REF))
        self.assertEqual(_classify(None), "EMPTY")
        for payload in (
            {"rtnYN": []},
            {},
            {"rtnYN": "Y"},
            {"rtnYN": "Y", "path": PATH.replace("SYNTHETIC", "OTHER")},
            {"rtnYN": "Y", "path": PATH.replace("TEST-REQUEST", "OTHER")},
            {"rtnYN": "Y", "path": PATH.replace("OPG", "EMRU")},
            {"rtnYN": "Y", "path": "https://example.invalid/file.pdf"},
            {"rtnYN": "Y", "path": PATH.replace("opnote.pdf", "..\\opnote.pdf")},
        ):
            with self.subTest(payload=payload), self.assertRaises(ParseError):
                parse_surgery_note(payload, REF)
        replay = replay_response(
            "oppl_records.note",
            b'{"rtnYN":"N"}',
            {"hhisnum": REF.mrn, "reqno": REF.request_no, "seqno": "0"},
        )
        self.assertEqual((replay["status"], replay["record_count"]), ("EMPTY", 0))

    def test_adapter_uses_record_query_hid_and_fresh_hid_after_reauthentication(self):
        previous, session, auth = adapter_with(
            response("expired", 403), response(json.dumps({"oprmlist": [row()]}))
        )
        auth.hid_for.side_effect = ["EXPIRED", "FRESH"]
        adapter = SurgeryCasesAdapter(previous.runtime)
        self.assertEqual(len(adapter.get_cases(SurgeryCaseFilter(surgeon_card="DR"))), 1)
        auth.login.assert_called_once_with(force=True)
        self.assertEqual(
            [c.args for c in auth.hid_for.call_args_list], [("oppl_records",), ("oppl_records",)]
        )
        payload = session.request.call_args.kwargs["data"]
        self.assertEqual(
            (payload["method"], payload["hid"], payload["doctVId"]), ("getQlog", "FRESH", "DR")
        )
        self.assertIn("qlogAction.do", session.request.call_args.kwargs["headers"]["Referer"])

    def test_adapter_note_then_pdf_use_recorded_viewer_and_validate_bytes(self):
        pdf = response(PDF.decode())
        pdf.headers["Content-Type"] = "application/pdf"
        previous, session, auth = adapter_with(
            response(json.dumps({"rtnYN": "Y", "path": PATH})), pdf
        )
        auth.hid_for.return_value = "FRESH"
        adapter = SurgeryCasesAdapter(previous.runtime)
        note = adapter.get_record_ref(REF)
        asset = adapter.download_record(note)
        self.assertEqual(asset.content, PDF)
        self.assertTrue(session.request.call_args.args[1].endswith("/jsp/pc/page/show/showPDF.jsp"))
        self.assertEqual(session.request.call_args.kwargs["params"], {"file": PATH})
        self.assertEqual(session.request.call_args_list[0].kwargs["data"]["seqno"], "0")
        previous, _, _ = adapter_with(response("<html>viewer instead of PDF</html>"))
        with self.assertRaises(ParseError) as error:
            SurgeryCasesAdapter(previous.runtime).download_record(note)
        self.assertEqual(error.exception.info.code, "PDF_BINARY_INVALID")

    def test_record_and_schedule_sso_cannot_reuse_each_others_hid(self):
        auth = AuthenticationAdapter(
            settings=SDKSettings(),
            credentials=PortalCredentials("SYNTHETIC", "SECRET"),
            transport=MagicMock(spec=SafeSessionTransport),
        )
        auth._portal_authenticated = True
        calls = []

        def open_profile(profile):
            calls.append(profile)
            session = AppSession(
                profile.key, f"HID-{len(calls)}", "https://synthetic.test/OPPLWeb/landing"
            )
            auth._apps[profile.key] = session
            return session

        with patch.object(auth, "_open_profile", side_effect=open_profile):
            auth.ensure("oppl")
            first = auth.ensure("oppl_records")
            self.assertIs(auth.ensure("oppl_records"), first)
            auth.ensure("oppl")
            final = auth.ensure("oppl_records")
        self.assertNotEqual(first.hid, final.hid)
        self.assertEqual([p.key for p in calls], ["oppl", "oppl_records", "oppl", "oppl_records"])
        self.assertTrue(calls[1].app_dn.startswith("ou=010801_04,"))

    def test_live_config_queries_all_cases_and_samples_notes_across_years(self):
        config = resolve_live_test_config(
            json_values=load_live_test_config(ROOT / "configs/surgery-records.example.json"),
            environ={},
        )
        plan = build_test_plan(config)
        self.assertEqual(plan["auth_targets"], ["portal", "oppl_records"])
        self.assertEqual(len(plan["operations"]), 4)
        self.assertFalse(plan["weekly_opd_soap"]["enabled"])
        config = replace(config, doctor_card="SYNTHETIC", opd_date=date(2026, 9, 20), max_items=2)
        inputs = _query_inputs(query_spec("oppl_records.cases"), config, {})
        self.assertEqual([i["filter"].period for i in inputs], ["24M", "2YB"])
        self.assertTrue(all(i["filter"].surgeon_card == "SYNTHETIC" for i in inputs))
        self.assertEqual(
            resolve_live_test_config(json_values=config.to_safe_dict(), environ={}), config
        )
        for bad in ({"assistant_cards": None}, {"periods": "24M"}, {"unknown": True}):
            with self.assertRaises(ConfigurationError):
                LiveTestConfig(surgery_query=bad)

    def test_explicit_supervising_physician_works_without_login_card_filter(self):
        config = LiveTestConfig(
            profile="atomic",
            only_operations=("oppl_records.cases",),
            surgery_query={"supervising_card": "SUP", "periods": ["24M"]},
        )
        plan = build_test_plan(config)
        self.assertTrue(plan["operations"][0]["input_available"])
        inputs = _query_inputs(query_spec("oppl_records.cases"), config, {})
        self.assertEqual(inputs[0]["filter"].supervising_card, "SUP")
        self.assertEqual(inputs[0]["filter"].surgeon_card, "")


class SurgeryCollectionTests(unittest.TestCase):
    def test_all_partitions_deduplicate_and_failures_do_not_hide_later_records(self):
        refs = [replace(REF, request_no=f"TEST-{i}") for i in range(4)]
        cases = parse_surgery_cases({"oprmlist": [row(ref) for ref in refs]})

        def note(ref):
            if ref == refs[0]:
                raise RequestError("synthetic note failure")
            if ref == refs[1]:
                return None
            return SurgeryNoteRef(ref, PATH.replace(REF.request_no, ref.request_no))

        def pdf(ref):
            if ref.reference == refs[2]:
                raise ParseError("synthetic PDF failure")
            return BinaryAsset(PDF, "application/pdf")

        surgery = SimpleNamespace(
            get_cases=Mock(side_effect=[cases[:3], cases[2:]]),
            get_record_ref=Mock(side_effect=note),
            download_record=Mock(side_effect=pdf),
        )
        with tempfile.TemporaryDirectory() as d:
            result = collect_surgery_records(
                SimpleNamespace(surgery=surgery),
                SurgeryCaseFilter(surgeon_card="DR"),
                periods=("24M", "2YB"),
                output_dir=Path(d),
            )
            self.assertEqual(result.status, "INCOMPLETE")
            self.assertEqual(len(result.cases), 4)
            self.assertEqual(
                [r["status"] for r in result.records], ["ERROR", "NOT_FOUND", "ERROR", "DOWNLOADED"]
            )
            self.assertEqual(len(list(Path(d, "stage1_cases").glob("*.json"))), 2)
            self.assertEqual(len(list(Path(d, "stage2_notes").glob("*.json"))), 4)
            self.assertEqual(len(list(Path(d, "stage3_pdf").glob("*.pdf"))), 1)
            self.assertEqual(json.loads(result.manifest_path.read_text())["errors"], 2)

    def test_partition_failure_is_incomplete_but_valid_partition_is_processed(self):
        cases = parse_surgery_cases({"oprmlist": [row()]})
        surgery = SimpleNamespace(
            get_cases=Mock(side_effect=[RequestError("synthetic"), cases]),
            get_record_ref=Mock(return_value=None),
            download_record=Mock(),
        )
        with tempfile.TemporaryDirectory() as d:
            result = collect_surgery_records(
                SimpleNamespace(surgery=surgery),
                SurgeryCaseFilter(surgeon_card="DR"),
                periods=("24M", "2YB"),
                output_dir=Path(d),
                start_date=date(2026, 6, 1),
                end_date=date(2026, 6, 1),
            )
            self.assertEqual((result.status, len(result.cases)), ("INCOMPLETE", 1))
            surgery.get_record_ref.assert_called_once()
            surgery.download_record.assert_not_called()

    def test_collection_has_no_hidden_case_limit_and_preserves_no_note_status(self):
        cases = parse_surgery_cases(
            {"oprmlist": [row(replace(REF, request_no=f"TEST-{i}")) for i in range(105)]}
        )
        surgery = SimpleNamespace(
            get_cases=Mock(return_value=cases),
            get_record_ref=Mock(return_value=None),
            download_record=Mock(),
        )
        with tempfile.TemporaryDirectory() as d:
            result = collect_surgery_records(
                SimpleNamespace(surgery=surgery),
                SurgeryCaseFilter(surgeon_card="DR"),
                output_dir=Path(d),
                download=False,
            )
            self.assertEqual(
                (result.status, len(result.cases), surgery.get_record_ref.call_count),
                ("OK", 105, 105),
            )
            with self.assertRaises(ConfigurationError):
                collect_surgery_records(
                    SimpleNamespace(surgery=surgery),
                    SurgeryCaseFilter(surgeon_card="DR"),
                    output_dir=Path(d),
                )


@unittest.skipUnless(os.environ.get("VGHKS_RUN_HAR_CONTRACT") == "1", "opt-in HAR fixtures")
class SurgeryHarTests(unittest.TestCase):
    def test_recorded_cases_and_locator_without_network(self):
        source = ROOT / "data/recordings/2026-09-20/手術紀錄查詢.har"
        if not source.exists():
            self.skipTest("local HAR unavailable")
        entries = json.loads(source.read_text(encoding="utf-8-sig"))["log"]["entries"]
        counts = []
        refs = set()
        with patch("requests.sessions.Session.request", side_effect=AssertionError("offline only")):
            for entry in entries:
                params = {
                    p["name"]: p["value"]
                    for p in entry["request"].get("postData", {}).get("params", [])
                }
                text = entry["response"]["content"].get("text", "")
                if params.get("method") == "getQlog":
                    cases = parse_surgery_cases(json.loads(text))
                    counts.append((params["opdate"], len(cases)))
                    refs.update(c.reference for c in cases)
                    self.assertTrue(all("80416" in c.procedure_codes for c in cases))
                if params.get("method") == "getOpnotePDF":
                    ref = SurgeryCaseRef(params["hhisnum"], params["reqno"], params["seqno"])
                    self.assertIsNotNone(parse_surgery_note(json.loads(text), ref))
                    self.assertIn(ref, refs)
        self.assertEqual(counts, [("24M", 74), ("2YB", 66), ("24M", 74)])
        self.assertEqual(len(refs), 140)


if __name__ == "__main__":
    unittest.main()
