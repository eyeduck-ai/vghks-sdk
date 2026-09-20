"""Synthetic patient lookup, visit selection and downstream composition checks."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from vghks_sdk.adapters.prq import PrqAdapter
from vghks_sdk.core.errors import AuthExpiredError, ConfigurationError, ParseError
from vghks_sdk.live.config import LiveTestConfig, resolve_live_test_config
from vghks_sdk.models import VisitCase, VisitFilter
from vghks_sdk.offline.replay import identify_operation, replay_hars, replay_response
from vghks_sdk.parsing.prq import parse_patient_identity, parse_visit_cases
from vghks_sdk.queries import query_spec, run_query
from vghks_sdk.runtime import SDKRuntime
from vghks_sdk.services.orders import OrdersService
from vghks_sdk.services.records import RecordsService

MRN = "00000000"
NATIONAL_ID = "TESTID01"
CONTEXT = """<frameset id="hFrameset">
<frame name="lPatientData" src="/PRQWeb/Page/JSP/KS_Patient.jsp">
<frame name="lList" src="/PRQWeb/QueryCaseList.do"></frameset>"""
EMPTY_LIST = (
    '<input id="typeO"><input id="typeA"><input id="typeE"><script>var aryCase=[];</script>'
)


def patient_header(mrn=MRN, identifier=NATIONAL_ID):
    return (
        f'<span id="pHistno">{mrn}</span>'
        '<script>var link="/AutoLogon?empID="+empId+'
        f'"&hidno={identifier}&hcase=&hseq=";</script>'
    )


def visit_link(**overrides):
    params = {
        "hid": "STALE-HID",
        "index": "0",
        "hhisnum": MRN,
        "caseType": "O",
        "caseNo": "CASE01",
        "caseSec": "70",
        "caseDT": "2026-01-02",
        "caseSectC": "眼科上午",
        "hidno": NATIONAL_ID,
    }
    params.update(overrides)
    return "/PRQWeb/QueryCaseDetail.do?" + urlencode(params)


def visit_row(doctor="測試醫師甲", **overrides):
    href = json.dumps(visit_link(**overrides), ensure_ascii=False)
    physician = json.dumps(doctor, ensure_ascii=False)
    return f'aryCase[0]=new KSCase({href}, "2026-01-02", caseTypeC, caseSectC, {physician});'


def visit_page(*rows):
    return EMPTY_LIST + "<script>" + "\n".join(rows or (visit_row(),)) + "</script>"


class VisitParsingTests(unittest.TestCase):
    def test_physician_column_and_complete_concatenated_link_are_parsed(self):
        prefix = visit_link(vsNo="D001") + "&vsNm="
        row = (
            f'new KSCase({json.dumps(prefix)}+encodeURIComponent("O\'Test 醫師")+"&rsNm=",'
            '"2026-01-02", caseTypeC, caseSectC, "O\'Test 醫師");'
        )
        cases = parse_visit_cases(visit_page(row, row), MRN)
        self.assertEqual(len(cases), 1)
        case = cases[0]
        self.assertEqual(case.doctor_name, "O'Test 醫師")
        self.assertEqual(case.doctor_card, "D001")
        self.assertEqual(case.detail_params["vsNm"], case.doctor_name)
        self.assertEqual(case.case_type_label, "門診")
        self.assertNotIn("hidno", case.detail_params)

    def test_doctor_is_not_inferred_from_another_branch_or_section(self):
        cases = parse_visit_cases(
            visit_page(
                visit_row(doctor=""),
                visit_row(doctor="OTHER", vsNo="D002"),
            ),
            MRN,
        )
        self.assertEqual(len(cases), 1)
        self.assertEqual((cases[0].doctor_name, cases[0].doctor_card), ("", ""))

    def test_active_rendering_branch_preserves_card_and_detail_context(self):
        inactive = visit_row(doctor="", caseType="A")
        active = visit_row(
            caseType="A", vsNo="1001F", dbSource="SYNTHETIC", vsNm="測試醫師甲"
        )
        source = f'if ("current" == "old" || "current" == "external") {{ {inactive} }} else {{ {active} }}'
        for page in (visit_page(source), source):
            with self.subTest(script_element=page != source):
                cases = parse_visit_cases(page, MRN)
                self.assertEqual(len(cases), 1)
                self.assertEqual(cases[0].doctor_card, "1001F")
                self.assertEqual(cases[0].doctor_name, "測試醫師甲")
                self.assertEqual(cases[0].detail_params["dbSource"], "SYNTHETIC")
                self.assertEqual(cases[0].detail_params["vsNm"], "測試醫師甲")

    def test_inactive_constructor_cannot_reappear_via_literal_url_fallback(self):
        active = visit_row(vsNo="D001")
        # Inactive paths may contain another patient or unsupported expressions;
        # neither should affect the selected, statically known branch.
        inactive = visit_row(doctor="OTHER", hhisnum="11111111", vsNo="D002")
        inactive = inactive.replace('"OTHER"', 'getDoctor()')
        cases = parse_visit_cases(visit_page(f'if (true) {{ {active} }} else {{ {inactive} }}'), MRN)
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0].doctor_card, "D001")

    def test_unknown_visit_branch_fails_instead_of_guessing_a_physician(self):
        page = visit_page(f'if (runtimeFlag) {{ {visit_row(vsNo="D001")} }}')
        with self.assertRaises(ParseError) as caught:
            parse_visit_cases(page, MRN)
        self.assertEqual(caught.exception.info.code, "JS_BRANCH_UNSUPPORTED")

    def test_legacy_anchor_has_physician_when_url_contains_it(self):
        url = visit_link(vsNm="測試醫師乙", vsNo="D002").replace("&", "&amp;")
        result = parse_visit_cases(f'<a href="{url}">visit</a>', MRN)
        self.assertEqual(result[0].doctor_name, "測試醫師乙")
        self.assertEqual(result[0].doctor_card, "D002")

    def test_unrecognized_doctor_expression_does_not_silently_hide_a_visit(self):
        row = f'new KSCase({json.dumps(visit_link())}, "date", type, section, getDoctor());'
        with self.assertRaises(ParseError) as caught:
            parse_visit_cases(visit_page(row), MRN)
        self.assertEqual(caught.exception.info.code, "PRQ_CASE_EXPRESSION_UNSUPPORTED")

    def test_no_sdk_history_limit_and_three_types_are_retained(self):
        rows = [visit_row(caseNo=f"CASE{i}", caseType=("O", "A", "E")[i % 3]) for i in range(101)]
        cases = parse_visit_cases(visit_page(*rows), MRN)
        self.assertEqual(len(cases), 101)
        self.assertEqual({case.case_type_label for case in cases}, {"門診", "住院", "急診"})

    def test_mixed_patient_page_fails_instead_of_returning_partial_results(self):
        with self.assertRaises(ParseError) as caught:
            parse_visit_cases(visit_page(visit_row(), visit_row(hhisnum="11111111")), MRN)
        self.assertEqual(caught.exception.info.code, "PRQ_CASE_PATIENT_MISMATCH")

    def test_case_national_id_must_agree_if_returned(self):
        with self.assertRaises(ParseError):
            parse_visit_cases(visit_page(), MRN, expected_national_id="OTHERID")

    def test_header_resolves_mrn_and_checks_patient_identity(self):
        self.assertEqual(
            parse_patient_identity(patient_header(), expected_national_id=NATIONAL_ID), MRN
        )
        for header in (
            patient_header(identifier="OTHERID"),
            '<span id="pHistno">00000000</span>',
            patient_header() + patient_header(mrn="11111111"),
            "<html>No patient</html>",
        ):
            with self.subTest(header=header), self.assertRaises(ParseError):
                parse_patient_identity(header, expected_national_id=NATIONAL_ID)

    def test_header_accepts_complete_urls_and_query_fragments(self):
        for value in (
            f"/AutoLogon?hidno={NATIONAL_ID}",
            f"https://hospital.example/AutoLogon?hidno={NATIONAL_ID}",
            f"?hidno={NATIONAL_ID}&caseNo=",
            f"&hidno={NATIONAL_ID}&caseNo=",
            "&amp;hidno=%54ESTID01&amp;caseNo=",
        ):
            page = f'<span id="pHistno">{MRN}</span><script>var link={json.dumps(value)};</script>'
            with self.subTest(value=value):
                self.assertEqual(
                    parse_patient_identity(page, expected_national_id=NATIONAL_ID), MRN
                )

    def test_header_rejects_conflicting_blank_or_unrelated_fragment_ids(self):
        for page in (
            patient_header(identifier="OTHERID"),
            patient_header(identifier=""),
            patient_header() + '<script>var other="&hidno=OTHERID";</script>',
            patient_header() + '<script>var other="&hidno=";</script>',
            patient_header(identifier=f"{NATIONAL_ID}&hidno=OTHERID"),
            patient_header(identifier=f"{NATIONAL_ID}-OTHER"),
            f'<span id="pHistno">{MRN}</span><script>var text="prefixhidno={NATIONAL_ID}";</script>',
            f'<span id="pHistno">{MRN}</span><script>var url="/elsewhere#hidno={NATIONAL_ID}";</script>',
        ):
            with self.subTest(page=page), self.assertRaises(ParseError) as caught:
                parse_patient_identity(page, expected_national_id=NATIONAL_ID)
            self.assertEqual(caught.exception.info.code, "PRQ_PATIENT_ID_MISMATCH")


class VisitSelectionTests(unittest.TestCase):
    def setUp(self):
        self.case = VisitCase(
            MRN,
            date(2026, 1, 2),
            "O",
            "C1",
            "70",
            "眼科上午",
            doctor_name="測試醫師甲",
            doctor_card="D001",
        )

    def test_all_four_dimensions_and_inclusive_exact_day(self):
        selector = VisitFilter(
            section_codes=("70",),
            case_types=("O",),
            start_date=date(2026, 1, 2),
            end_date=date(2026, 1, 2),
            doctor_names=("測試醫師甲",),
        )
        self.assertTrue(selector.matches(self.case))
        for changes in (
            {"visit_date": date(2026, 1, 1)},
            {"visit_date": None},
            {"case_type": "E"},
            {"section_code": "60"},
            {"doctor_name": "測試醫師乙"},
        ):
            self.assertFalse(selector.matches(replace(self.case, **changes)))

    def test_doctor_selectors_or_normalized_but_card_is_exact(self):
        selector = VisitFilter(
            all_sections=True,
            doctor_names=("測試醫師乙",),
            doctor_name_contains=("eye doctor",),
            doctor_cards=("ｄ００１",),
        )
        self.assertTrue(selector.matches(self.case))
        self.assertTrue(
            selector.matches(replace(self.case, doctor_card="", doctor_name="測試醫師乙"))
        )
        self.assertTrue(
            selector.matches(replace(self.case, doctor_card="", doctor_name="ＥＹＥ DOCTOR 甲"))
        )
        self.assertFalse(selector.matches(replace(self.case, doctor_card="D001F")))
        self.assertFalse(selector.matches(replace(self.case, doctor_card="", doctor_name="")))

    def test_inpatient_emergency_filters_and_default_outpatient_compatibility(self):
        rows = [self.case, replace(self.case, case_type="A"), replace(self.case, case_type="E")]
        self.assertEqual(len(VisitFilter(all_sections=True).select(rows)), 1)
        selected = VisitFilter(all_sections=True, case_types=("a", "e")).select(rows)
        self.assertEqual([row.case_type for row in selected], ["A", "E"])

    def test_physician_filter_roundtrips_through_test_config(self):
        config = LiveTestConfig(
            visit_filter=VisitFilter(
                section_codes=("70",),
                doctor_names=("測試醫師甲",),
                doctor_name_contains=("測試",),
                doctor_cards=("D001",),
            )
        )
        restored = resolve_live_test_config(json_values=config.to_safe_dict(), environ={})
        self.assertEqual(restored.visit_filter, config.visit_filter)


class VisitLookupTests(unittest.TestCase):
    def setUp(self):
        self.runtime = object.__new__(SDKRuntime)
        self.runtime.auth = Mock()
        self.runtime.auth.hid_for.return_value = "CURRENT-HID"
        self.runtime.settings = SimpleNamespace(prq_base_url="https://internal.test/PRQWeb")
        self.runtime.operation_lock = threading.RLock()
        self.runtime.diagnostics = None
        self.runtime.raw_capture = None
        self.replies = {
            "prq.patient_context": CONTEXT,
            "prq.patient_identity": patient_header(),
            "prq.visit_cases": visit_page(),
            "prq.case_detail": '<div id="tabs"><ul id="tab_ul"><li id="soap">SOAP</li></ul></div>',
            "prq.key_preflight": "ssID=s&keyOne=a&keyTwo=b&keyThree=c",
            "prq.soap": '<div id="data"><div class="soap"><pre>P: synthetic plan</pre></div></div>',
            "prq.case_orders_page": "page",
            "prq.case_orders_select": "select",
            "prq.case_orders": "<script>var aryCase=[];</script>",
        }

        def request(spec, _url, **_kwargs):
            # Every request in the patient lookup must use the same operation lock.
            self.assertTrue(self.runtime.operation_lock._is_owned())
            reply = self.replies[spec.key]
            if isinstance(reply, Exception):
                raise reply
            return reply

        self.runtime.request_text = Mock(side_effect=request)
        self.adapter = PrqAdapter(self.runtime)
        self.records = RecordsService(self.adapter)
        self.orders = OrdersService(self.adapter)

    def operation_keys(self):
        return [call.args[0].key for call in self.runtime.request_text.call_args_list]

    def test_mrn_lookup_keeps_existing_request_sequence(self):
        self.assertEqual(self.records.get_visit_cases(" ００００００００ ")[0].mrn, MRN)
        self.assertEqual(self.operation_keys(), ["prq.patient_context", "prq.visit_cases"])
        request = self.runtime.request_text.call_args_list[0]
        self.assertEqual(
            request.kwargs["data"], {"id": MRN, "queryID": "", "queryPtID": MRN, "type": "1"}
        )

    def test_id_lookup_resolves_mrn_before_visit_details_soap_and_orders(self):
        selected = self.records.find_visit_cases(
            national_id=" ｔｅｓｔｉｄ０１ ",
            visit_filter=VisitFilter(section_codes=("70",), doctor_names=("測試醫師甲",)),
        )
        self.assertEqual(len(selected), 1)
        self.assertEqual(
            self.operation_keys(),
            ["prq.patient_context", "prq.patient_identity", "prq.visit_cases"],
        )
        request = self.runtime.request_text.call_args_list[0]
        self.assertEqual(
            request.kwargs["data"],
            {"id": NATIONAL_ID, "queryID": NATIONAL_ID, "queryPtID": "", "type": "2"},
        )
        case = selected[0]
        soap = self.records.get_soap(case)
        self.assertEqual(soap.case, case)
        self.assertIn("synthetic plan", soap.full_text)
        self.assertEqual(self.orders.get_case_orders(case), [])
        calls = self.runtime.request_text.call_args_list
        for call in calls:
            params = call.kwargs.get("params", {})
            if "hhisnum" in params:
                self.assertEqual(params["hhisnum"], MRN)
            if call.args[0].key == "prq.case_detail":
                self.assertEqual(params["caseNo"], case.case_no)
                self.assertEqual(params["hid"], "CURRENT-HID")
                self.assertNotIn("hidno", params)

    def test_multiple_local_filters_make_no_additional_requests(self):
        rows = self.records.get_visit_cases(MRN)
        count = self.runtime.request_text.call_count
        self.assertEqual(
            len(VisitFilter(all_sections=True, doctor_cards=("OTHER",)).select(rows)), 0
        )
        self.assertEqual(
            len(VisitFilter(all_sections=True, doctor_names=("測試醫師甲",)).select(rows)), 1
        )
        self.assertEqual(self.runtime.request_text.call_count, count)

    def test_invalid_or_ambiguous_identifiers_fail_before_auth_or_network(self):
        for kwargs in (
            {},
            {"mrn": MRN, "national_id": NATIONAL_ID},
            {"mrn": ""},
            {"national_id": ""},
            {"national_id": "bad&input"},
            {"national_id": 123},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ConfigurationError):
                self.records.get_visit_cases(**kwargs)
        self.runtime.request_text.assert_not_called()
        self.runtime.auth.ensure.assert_not_called()

    def test_unknown_lookup_page_does_not_read_previous_patient_session(self):
        self.replies["prq.patient_context"] = '<script>alert("not found")</script>'
        with self.assertRaises(ParseError) as caught:
            self.records.get_visit_cases(national_id=NATIONAL_ID)
        self.assertEqual(caught.exception.info.code, "PRQ_PATIENT_CONTEXT_UNCONFIRMED")
        self.assertEqual(self.operation_keys(), ["prq.patient_context"])

    def test_header_identity_mismatch_stops_before_case_list(self):
        self.replies["prq.patient_identity"] = patient_header(identifier="OTHERID")
        with self.assertRaises(ParseError) as caught:
            self.records.get_visit_cases(national_id=NATIONAL_ID)
        self.assertEqual(caught.exception.info.code, "PRQ_PATIENT_ID_MISMATCH")
        self.assertEqual(self.operation_keys(), ["prq.patient_context", "prq.patient_identity"])

    def test_empty_case_list_is_different_from_failed_patient_lookup(self):
        self.replies["prq.visit_cases"] = EMPTY_LIST
        self.assertEqual(self.records.get_visit_cases(national_id=NATIONAL_ID), [])
        self.replies["prq.visit_cases"] = "<html>Unknown response</html>"
        with self.assertRaises(ParseError) as caught:
            self.records.get_visit_cases(national_id=NATIONAL_ID)
        self.assertEqual(caught.exception.info.code, "PRQ_CASE_LIST_STRUCTURE_MISSING")

    def test_expired_list_reestablishes_and_revalidates_patient_identity(self):
        original = self.runtime.request_text.side_effect
        failed = False

        def expiring(spec, url, **kwargs):
            nonlocal failed
            if spec.key == "prq.visit_cases" and not failed:
                failed = True
                raise AuthExpiredError("synthetic expiration")
            return original(spec, url, **kwargs)

        self.runtime.request_text.side_effect = expiring
        rows = self.records.get_visit_cases(national_id=NATIONAL_ID)
        self.assertEqual(len(rows), 1)
        self.runtime.auth.login.assert_called_once_with(force=True)
        self.assertEqual(
            self.operation_keys(),
            ["prq.patient_context", "prq.patient_identity", "prq.visit_cases"] * 2,
        )

    def test_catalog_accepts_either_identifier_but_not_both(self):
        sdk = SimpleNamespace(records=self.records)
        self.assertIn(("national_id",), query_spec("prq.visit_cases").alternative_inputs)
        self.assertEqual(run_query(sdk, "prq.visit_cases", national_id=NATIONAL_ID)[0].mrn, MRN)
        with self.assertRaises(ConfigurationError):
            run_query(sdk, "prq.visit_cases", mrn=MRN, national_id=NATIONAL_ID)

    def test_inpatient_and_emergency_do_not_use_outpatient_soap_or_orders(self):
        row = parse_visit_cases(visit_page(), MRN)[0]
        for case_type in ("A", "E"):
            case = replace(row, case_type=case_type)
            for method in (self.records.get_soap, self.orders.get_case_orders):
                with self.assertRaises(ConfigurationError) as caught:
                    method(case)
                self.assertEqual(caught.exception.info.code, "CASE_TYPE_UNSUPPORTED")
        self.runtime.request_text.assert_not_called()


class VisitReplayTests(unittest.TestCase):
    def test_patient_header_replay_reports_only_count(self):
        request = {"method": "GET", "url": "https://example.test/PRQWeb/Page/JSP/KS_Patient.jsp"}
        self.assertEqual(identify_operation(request), "prq.patient_identity")
        result = replay_response(
            "prq.patient_identity", patient_header().encode(), {}, mime="text/html; charset=utf-8"
        )
        self.assertEqual(result, {"status": "PARSED", "error_code": "", "record_count": 1})

    def test_id_har_replay_uses_header_mrn_and_leaves_identifiers_out_of_report(self):
        entries = []
        for path, text, form in (
            (
                "QueryPatientRecord.do",
                CONTEXT,
                {"id": NATIONAL_ID, "type": "2", "queryID": NATIONAL_ID, "queryPtID": ""},
            ),
            ("Page/JSP/KS_Patient.jsp", patient_header(), None),
            ("QueryCaseList.do", visit_page(), None),
        ):
            request = {
                "method": "POST" if form else "GET",
                "url": "https://example.test/PRQWeb/" + path,
            }
            if form:
                request["postData"] = {"text": urlencode(form)}
            entries.append(
                {
                    "request": request,
                    "response": {"status": 200, "content": {"text": text, "mimeType": "text/html"}},
                }
            )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            har = root / "synthetic.har"
            har.write_text(
                json.dumps({"log": {"version": "1.2", "entries": entries}}), encoding="utf-8"
            )
            with patch("requests.sessions.Session.request") as network:
                result = replay_hars(har, output_path=root / "replay.json")
            network.assert_not_called()
            self.assertEqual(result["status"], "OK")
            output = (root / "replay.json").read_text(encoding="utf-8")
            for identifier in (MRN, NATIONAL_ID, "測試醫師甲"):
                self.assertNotIn(identifier, output)


if __name__ == "__main__":
    unittest.main()
