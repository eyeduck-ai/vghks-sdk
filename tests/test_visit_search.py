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
from vghks_sdk.core.errors import (
    AccessReviewRequiredError,
    AuthExpiredError,
    AuthorizationError,
    ConfigurationError,
    ParseError,
    RequestError,
)
from vghks_sdk.core.operations import operation_spec
from vghks_sdk.live.atomic import _query_inputs, build_test_plan
from vghks_sdk.live.config import LiveTestConfig, resolve_live_test_config
from vghks_sdk.models import OrderHistoryFilter, VisitCase, VisitFilter
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


def access_review_form(mrn=MRN, hid="CURRENT-HID", action="../../../EMRProcess.do"):
    fields = {
        "reqCode": "saveAccessCause",
        "value(status)": "01",
        "value(smr_hid)": hid,
        "value(smr_hhisnum)": mrn,
        "value(smr_Flg)": "Case1",
        "value(inCaseFlg)": "N",
        "value(bgnDt)": "2026-01-01",
        "value(endDt)": "2026-12-31",
        "value(causeOther1)": "",
    }
    hidden = "".join(
        f'<input type="hidden" name="{name}" value="{value}">'
        for name, value in fields.items()
    )
    return (
        f'<form id="addForm" method="post" action="{action}">'
        + hidden
        + '<input type="checkbox" name="valueA(cause)" value="1A">了解病情'
        + '<input type="checkbox" name="valueA(cause)" value="2B">其他原因'
        + "</form>"
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

    def test_patient_scoped_active_cases_keep_legacy_mrn_and_lookup_mrn(self):
        rows = parse_visit_cases(
            visit_page(
                visit_row(caseNo="CURRENT"),
                visit_row(hhisnum="11111111", caseNo="LEGACY", caseDT="2009-09-03"),
                visit_row(hhisnum="22222222", caseNo="OLDER", caseDT="2001-04-05"),
            ),
            MRN,
            allow_related_mrns=True,
        )
        self.assertEqual({case.case_no for case in rows}, {"CURRENT", "LEGACY", "OLDER"})
        self.assertEqual({case.mrn for case in rows}, {MRN, "11111111", "22222222"})
        legacy = next(case for case in rows if case.case_no == "LEGACY")
        self.assertEqual((legacy.mrn, legacy.lookup_mrn, legacy.patient_mrn),
                         ("11111111", MRN, MRN))
        self.assertEqual(legacy.detail_params["hhisnum"], "11111111")
        self.assertEqual(len(VisitFilter(all_sections=True).select(rows)), 3)

    def test_unscoped_or_malformed_legacy_link_still_fails(self):
        for source in (
            f'<a href="{visit_link(hhisnum="11111111")}">visit</a>',
            visit_page(visit_row(hhisnum="../other")),
        ):
            with self.subTest(source=source), self.assertRaises(ParseError) as caught:
                parse_visit_cases(source, MRN, allow_related_mrns=True)
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
        self.runtime._operation_write_attempts = []
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
            if spec.mutates:
                for index in range(len(self.runtime._operation_write_attempts)):
                    self.runtime._operation_write_attempts[index] = True
            reply = self.replies[spec.key]
            if isinstance(reply, list):
                reply = reply.pop(0)
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

    def test_mrn_lookup_rejects_unconfirmed_context_before_case_list(self):
        self.replies["prq.patient_context"] = '<script>alert("not found")</script>'
        with self.assertRaises(ParseError) as caught:
            self.records.get_visit_cases(MRN)
        self.assertEqual(caught.exception.info.code, "PRQ_PATIENT_CONTEXT_UNCONFIRMED")
        self.assertEqual(self.operation_keys(), ["prq.patient_context"])

    def test_access_review_automatically_submits_recorded_care_reason(self):
        self.replies["prq.patient_context"] = access_review_form()
        self.replies["prq.access_review"] = CONTEXT
        rows = self.records.get_visit_cases(MRN)
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            self.operation_keys(),
            ["prq.patient_context", "prq.access_review", "prq.visit_cases"],
        )
        review_call = self.runtime.request_text.call_args_list[1]
        self.assertTrue(review_call.args[0].mutates)
        self.assertEqual(review_call.args[1], "https://internal.test/PRQWeb/EMRProcess.do")
        submitted = dict(review_call.kwargs["data"])
        self.assertEqual(submitted["valueA(cause)"], "1A")
        self.assertEqual(submitted["value(smr_hhisnum)"], MRN)
        self.assertEqual(submitted["value(smr_hid)"], "CURRENT-HID")
        self.assertEqual(
            len(run_query(
                SimpleNamespace(records=self.records),
                "prq.visit_cases",
                mrn=MRN,
            )),
            1,
        )

    def test_access_review_rejects_mismatched_patient_login_target_and_reason(self):
        for form, code in (
            (access_review_form(mrn="OTHER"), "PRQ_ACCESS_REVIEW_PATIENT_MISMATCH"),
            (access_review_form(hid="OTHER"), "PRQ_ACCESS_REVIEW_HID_MISMATCH"),
            (access_review_form(action="https://outside.test/EMRProcess.do"),
             "PRQ_ACCESS_REVIEW_FORM_INVALID"),
        ):
            with self.subTest(code=code):
                self.runtime.request_text.reset_mock()
                self.replies["prq.patient_context"] = form
                with self.assertRaises(ParseError) as caught:
                    self.records.get_visit_cases(MRN)
                self.assertEqual(caught.exception.info.code, code)
                self.assertEqual(self.operation_keys(), ["prq.patient_context"])
        self.replies["prq.patient_context"] = access_review_form()
        self.runtime.request_text.reset_mock()
        with self.assertRaises(AccessReviewRequiredError) as caught:
            self.replies["prq.patient_context"] = access_review_form().replace(
                'value="1A"', 'value="1C"'
            )
            self.records.get_visit_cases(MRN)
        self.assertEqual(caught.exception.info.code, "PRQ_ACCESS_REVIEW_REQUIRED")
        self.assertEqual(self.operation_keys(), ["prq.patient_context"])
        self.replies["prq.patient_context"] = access_review_form()
        with self.assertRaises(ConfigurationError) as caught:
            self.records.get_visit_cases(MRN, access_review_reason="9Z")
        self.assertEqual(caught.exception.info.code, "PRQ_ACCESS_REVIEW_REASON_UNAVAILABLE")
        with self.assertRaises(ConfigurationError) as caught:
            self.records.get_visit_cases(national_id=NATIONAL_ID, access_review_reason="1A")
        self.assertEqual(caught.exception.info.code, "PRQ_ACCESS_REVIEW_INPUT_INVALID")

    def test_access_review_failure_is_not_reposted_or_treated_as_success(self):
        self.replies["prq.patient_context"] = access_review_form()
        self.replies["prq.access_review"] = "<html>not confirmed</html>"
        with self.assertRaises(AuthorizationError) as caught:
            self.records.get_visit_cases(MRN)
        self.assertEqual(caught.exception.info.code, "PRQ_ACCESS_REVIEW_NOT_ACCEPTED")
        self.assertEqual(self.operation_keys(), ["prq.patient_context", "prq.access_review"])
        self.runtime.auth.login.assert_not_called()

        self.runtime.request_text.reset_mock()
        self.replies["prq.access_review"] = AuthExpiredError("synthetic expired")
        with self.assertRaises(AuthExpiredError):
            self.records.get_visit_cases(MRN)
        self.assertEqual(self.operation_keys(), ["prq.patient_context", "prq.access_review"])
        self.runtime.auth.login.assert_not_called()

    def test_soap_can_complete_patient_review_before_loading_case(self):
        case = self.records.get_visit_cases(MRN)[0]
        self.runtime.request_text.reset_mock()
        self.replies["prq.patient_context"] = access_review_form()
        self.replies["prq.access_review"] = CONTEXT
        soap = self.records.get_soap(case, access_review_reason="1A")
        self.assertIsNotNone(soap)
        self.assertEqual(
            self.operation_keys(),
            ["prq.patient_context", "prq.access_review", "prq.case_detail",
             "prq.key_preflight", "prq.soap"],
        )
        review_call = self.runtime.request_text.call_args_list[1]
        self.assertEqual(dict(review_call.kwargs["data"])["valueA(cause)"], "1A")

        self.runtime.request_text.reset_mock()
        self.assertIsNotNone(run_query(
            SimpleNamespace(records=self.records), "prq.soap", case=case,
            access_review_reason="1A",
        ))

    def test_soap_retries_read_once_after_automatic_review(self):
        case = self.records.get_visit_cases(MRN)[0]
        self.runtime.request_text.reset_mock()
        self.replies["prq.soap"] = [access_review_form(), self.replies["prq.soap"]]
        self.replies["prq.access_review"] = CONTEXT
        self.assertIsNotNone(self.records.get_soap(case))
        self.assertEqual(self.operation_keys().count("prq.access_review"), 1)
        self.assertEqual(self.operation_keys().count("prq.soap"), 2)

    def test_case_detail_resumes_after_review_and_stops_if_rechallenged(self):
        case = self.records.get_visit_cases(MRN)[0]
        detail = self.replies["prq.case_detail"]
        self.runtime.request_text.reset_mock()
        self.replies["prq.access_review"] = CONTEXT
        self.replies["prq.case_detail"] = [access_review_form(), detail]
        self.assertIsNotNone(self.records.get_case_detail(case))
        self.assertEqual(self.operation_keys(), [
            "prq.case_detail", "prq.access_review", "prq.case_detail",
        ])

        self.runtime.request_text.reset_mock()
        self.replies["prq.case_detail"] = [access_review_form(), access_review_form()]
        with self.assertRaises(AuthorizationError) as caught:
            self.records.get_case_detail(case)
        self.assertEqual(caught.exception.info.code, "PRQ_ACCESS_REVIEW_NOT_ACCEPTED")
        self.assertEqual(self.operation_keys().count("prq.access_review"), 1)

    def test_soap_does_not_submit_twice_if_detail_and_soap_both_challenge(self):
        case = self.records.get_visit_cases(MRN)[0]
        self.runtime.request_text.reset_mock()
        self.replies["prq.case_detail"] = [
            access_review_form(), self.replies["prq.case_detail"],
        ]
        self.replies["prq.access_review"] = CONTEXT
        self.replies["prq.soap"] = access_review_form()
        with self.assertRaises(AuthorizationError) as caught:
            self.records.get_soap(case)
        self.assertEqual(caught.exception.info.code, "PRQ_ACCESS_REVIEW_NOT_ACCEPTED")
        self.assertEqual(self.operation_keys().count("prq.access_review"), 1)

    def test_upload_history_establishes_patient_context_and_auto_reviews(self):
        self.replies["prq.patient_context"] = access_review_form()
        self.replies["prq.access_review"] = CONTEXT
        self.replies["prq.upload_history"] = (
            '<table id="tbObj"><tr><th>Category</th></tr></table>'
        )
        history = self.records.get_upload_history(MRN)
        self.assertEqual(history.mrn, MRN)
        self.assertEqual(self.operation_keys(), [
            "prq.patient_context", "prq.access_review", "prq.upload_history",
        ])

    def test_order_history_resumes_after_context_review(self):
        self.replies["prq.patient_history_context"] = access_review_form()
        self.replies["prq.access_review"] = CONTEXT
        self.replies["prq.order_history_page"] = "page"
        self.replies["prq.order_history_select"] = "select"
        self.replies["prq.order_history"] = "<script>var aryCase=[];</script>"
        self.assertEqual(self.adapter.get_order_history(MRN, OrderHistoryFilter()), [])
        self.assertEqual(self.operation_keys(), [
            "prq.patient_history_context", "prq.access_review",
            "prq.order_history_page", "prq.order_history_select", "prq.order_history",
        ])

    def test_runtime_never_replays_operation_after_review_post_attempt(self):
        self.runtime.transport = Mock()
        self.runtime.transport.request.side_effect = RequestError(
            "synthetic rejection", status_code=401,
        )

        def operation():
            self.runtime.request_response(
                operation_spec("prq.access_review"),
                "https://internal.test/PRQWeb/EMRProcess.do",
                data={"reqCode": "saveAccessCause"},
            )

        with self.assertRaises(AuthExpiredError):
            self.runtime.execute(
                operation_spec("prq.visit_cases"), operation,
                operation_name="synthetic_review",
            )
        self.runtime.transport.request.assert_called_once()
        self.runtime.auth.login.assert_not_called()

    def test_runtime_can_reauthenticate_before_review_post(self):
        attempts = 0

        def operation():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise AuthExpiredError("synthetic expired before review")
            return "ready"

        self.assertEqual(self.runtime.execute(
            operation_spec("prq.visit_cases"), operation,
            operation_name="synthetic_patient_lookup",
        ), "ready")
        self.assertEqual(attempts, 2)
        self.runtime.auth.login.assert_called_once_with(force=True)

    def test_offline_review_ack_requires_matching_patient_and_login(self):
        payload = {"value(smr_hhisnum)": MRN, "value(smr_hid)": "CURRENT-HID"}
        confirmed = replay_response(
            "prq.access_review", CONTEXT.encode(), payload,
            context_mrn=MRN, context_hid="CURRENT-HID",
        )
        self.assertEqual(confirmed["status"], "RECORDED_ACK")
        mismatched = replay_response(
            "prq.access_review", CONTEXT.encode(), payload,
            context_mrn=MRN, context_hid="OTHER-HID",
        )
        self.assertEqual(mismatched["error_code"], "PRQ_ACCESS_REVIEW_HID_MISMATCH")

    def test_live_query_inputs_carry_explicit_review_reason(self):
        config = resolve_live_test_config(json_values=LiveTestConfig(
            profile="regression", test_mrn=MRN, access_review_reason="1A",
            max_cases=1,
        ).to_safe_dict())
        case = self.records.get_visit_cases(MRN)[0]
        self.assertEqual(
            _query_inputs(query_spec("prq.visit_cases"), config, {}),
            [{"mrn": MRN, "access_review_reason": "1A"}],
        )
        self.assertEqual(
            _query_inputs(query_spec("prq.soap"), config, {"prq.visit_cases": [[case]]}),
            [{"case": case, "access_review_reason": "1A"}],
        )
        plan = build_test_plan(config)
        self.assertTrue(plan["conditional_access_review"])
        self.assertNotIn("prq.access_review", plan["excluded_write_operations"])

        automatic = resolve_live_test_config(json_values=LiveTestConfig(
            profile="regression", test_mrn=MRN, max_cases=1,
        ).to_safe_dict())
        self.assertEqual(
            _query_inputs(query_spec("prq.visit_cases"), automatic, {}),
            [{"mrn": MRN}],
        )
        automatic_plan = build_test_plan(automatic)
        self.assertTrue(automatic_plan["conditional_access_review"])
        self.assertNotIn("prq.access_review", automatic_plan["excluded_write_operations"])

    def test_legacy_case_uses_source_mrn_for_detail_and_soap(self):
        self.replies["prq.visit_cases"] = visit_page(
            visit_row(caseNo="CURRENT"),
            visit_row(hhisnum="11111111", caseNo="LEGACY", caseDT="2009-09-03"),
        )
        rows = self.records.get_visit_cases(MRN)
        self.assertEqual(len(rows), 2)
        legacy = next(case for case in rows if case.case_no == "LEGACY")
        self.assertEqual((legacy.mrn, legacy.patient_mrn), ("11111111", MRN))
        self.records.get_soap(legacy)
        requests = self.runtime.request_text.call_args_list
        for call in requests:
            if call.args[0].key in {"prq.case_detail", "prq.soap"}:
                self.assertEqual(call.kwargs["params"]["hhisnum"], "11111111")

    def test_national_id_lookup_accepts_alias_but_rejects_explicit_id_conflict(self):
        self.replies["prq.visit_cases"] = visit_page(
            visit_row(caseNo="CURRENT"),
            visit_row(hhisnum="11111111", caseNo="LEGACY"),
        )
        rows = self.records.get_visit_cases(national_id=NATIONAL_ID)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(case.patient_mrn == MRN for case in rows))
        self.replies["prq.visit_cases"] = visit_page(
            visit_row(caseNo="CURRENT"),
            visit_row(hhisnum="11111111", caseNo="LEGACY", hidno="OTHERID"),
        )
        with self.assertRaises(ParseError) as caught:
            self.records.get_visit_cases(national_id=NATIONAL_ID)
        self.assertEqual(caught.exception.info.code, "PRQ_PATIENT_ID_MISMATCH")

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
    def test_replay_only_accepts_legacy_mrn_with_confirmed_context(self):
        page = visit_page(
            visit_row(caseNo="CURRENT"),
            visit_row(hhisnum="11111111", caseNo="LEGACY"),
        ).encode()
        unconfirmed = replay_response("prq.visit_cases", page, {}, context_mrn=MRN)
        confirmed = replay_response(
            "prq.visit_cases", page, {}, context_mrn=MRN, trusted_visit_context=True
        )
        self.assertEqual(unconfirmed["error_code"], "PRQ_CASE_PATIENT_MISMATCH")
        self.assertEqual((confirmed["status"], confirmed["record_count"]), ("PARSED", 2))

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
