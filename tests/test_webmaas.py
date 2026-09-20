from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from datetime import date
from html import escape
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from vghks_sdk import PatientBasicInfo, PortalCredentials, RequestPolicy, SDKSettings
from vghks_sdk.adapters.auth import AppSession, AuthenticationAdapter
from vghks_sdk.adapters.webmaas import WebMaasAdapter
from vghks_sdk.core.errors import ConfigurationError, NotFoundError, ParseError
from vghks_sdk.core.transport import SafeSessionTransport
from vghks_sdk.models import to_jsonable
from vghks_sdk.parsing.webmaas import (
    find_next_displaytag_href,
    parse_patient_basic_info,
    parse_query_form,
    parse_registration_records,
)
from vghks_sdk.runtime import SDKRuntime

ROOT = Path(__file__).resolve().parents[1]
MRN = "TEST001"
TOKEN = "org.apache.struts.taglib.html.TOKEN"
HEADERS = (
    "看 診 日 期",
    "科 別",
    "診別",
    "序號",
    "病歷號",
    "姓 名",
    "預計看診",
    "狀態",
    "掛 號 時 間",
    "掛號人員",
    "取消時間",
    "取消人員",
    "註記",
)


def landing(form_id="QUY15WForm", token="fresh-token"):
    return (
        f'<form id="{form_id}"><input type="hidden" name="{TOKEN}" value="{token}">'
        '<input type="hidden" name="hcaseno" value="STALE-CASE">'
        '<input name="patno" value="OLD-PATIENT"></form>'
    )


def basic_info(mrn=MRN):
    fields = (
        ("病歷號", mrn),
        ("姓名", "合成病人"),
        ("身分證號", "SYNTHETIC-ID"),
        ("生日", "2000-01-02"),
        ("性別", "女 (26歲8個月8天 )"),
        ("血型", ""),
        ("身高", "160.0"),
        ("體重", "50.0"),
        ("電話", "000000000"),
        ("地址", "合成地址"),
        ("聯絡人", "合成聯絡人"),
        ("病房床位", "TEST-001"),
        ("入院日期", "2026-06-01 09:00:00"),
        ("出院時間", "2026-06-02 10:00:00"),
        ("CASE", "SYNTHETIC-CASE"),
        ("入院診斷", "合成診斷"),
        ("", "Synthetic diagnosis"),
        ("未來新欄位", "keep-me"),
    )
    return (
        '<div id="DETAIL">'
        + "".join(
            f'<div class="form-group"><label>{label}</label>'
            f'<span class="label label-orange">{escape(value)}</span></div>'
            for label, value in fields
        )
        + '<label class="alert alert-danger">此住院病人已出院!</label></div>'
    )


def registration(mrn=MRN, day="2026-11-13", next_href="", *, empty=False):
    values = (
        day,
        "70\u00a0眼科上午",
        "02",
        "001",
        mrn,
        "合成病人",
        "10:30",
        "",
        "2026-09-20 10:00",
        "TEST-STAFF",
        "",
        "",
        "",
    )
    body = (
        '<td colspan="13">Nothing found to display.</td>'
        if empty
        else "".join(f"<td>{escape(value)}</td>" for value in values)
    )
    return (
        '<table id="row"><tr>'
        + "".join(f"<th>{h}</th>" for h in HEADERS)
        + (f"</tr><tr>{body}</tr></table>")
        + (f'<a href="{escape(next_href)}">下一頁</a>' if next_href else "")
    )


def response(body, status=200):
    result = requests.Response()
    result.status_code = status
    result.url = "https://synthetic.test/webmaas/query"
    result.headers["Content-Type"] = "text/html; charset=utf-8"
    result._content = body.encode("utf-8")
    result._content_consumed = True
    return result


def adapter_with(*responses):
    session = requests.Session()
    session.request = MagicMock(side_effect=responses)
    auth = MagicMock(spec=AuthenticationAdapter)
    auth.assert_not_expired = MagicMock()
    auth.credentials = PortalCredentials("SYNTHETIC", "TEST-SECRET")
    policy = RequestPolicy(min_delay_seconds=0, max_delay_seconds=0, max_attempts=1)
    transport = SafeSessionTransport(policy=policy, session=session, sleeper=lambda _: None)
    runtime = SDKRuntime(settings=SDKSettings(), transport=transport, auth=auth)
    return WebMaasAdapter(runtime), session, auth


def identity(mrn=MRN):
    return response(json.dumps([{"patno": mrn, "patname": "Synthetic", "birthday": "2000-01-02"}]))


class WebMaasParserTests(unittest.TestCase):
    def test_basic_info_retains_context_notices_and_unknown_fields(self):
        result = parse_patient_basic_info(basic_info(), MRN)
        self.assertIsInstance(result, PatientBasicInfo)
        self.assertEqual(result.birthday, date(2000, 1, 2))
        self.assertEqual(result.sex, "女")
        self.assertEqual(result.age, "26歲8個月8天")
        self.assertEqual(result.height, "160.0")
        self.assertEqual(result.admission_diagnosis, ("合成診斷", "Synthetic diagnosis"))
        self.assertIn("已出院", result.notices[0])
        self.assertEqual(result.fields["未來新欄位"], "keep-me")
        self.assertEqual(result.raw_html, basic_info())
        self.assertEqual(to_jsonable(result)["birthday"], "2000-01-02")

    def test_wrong_patient_or_unknown_page_does_not_become_success_or_empty(self):
        for source, code in (
            (basic_info("WRONG"), "WEBMAAS_PATIENT_MISMATCH"),
            ("<html>maintenance</html>", "WEBMAAS_BASIC_INFO_STRUCTURE_MISSING"),
            ('<div id="DETAIL">unknown layout</div>', "WEBMAAS_BASIC_INFO_IDENTITY_MISSING"),
        ):
            with self.subTest(code=code), self.assertRaises(ParseError) as caught:
                parse_patient_basic_info(source, MRN)
            self.assertEqual(caught.exception.info.code, code)
        with self.assertRaises(NotFoundError):
            parse_patient_basic_info('<div id="LIST">查無資料!</div>', MRN)

    def test_four_digit_birth_year_is_not_mistaken_for_roc(self):
        for source, expected in (("1910-01-02", 1910), ("099/01/02", 2010)):
            with self.subTest(source=source):
                result = parse_patient_basic_info(basic_info().replace("2000-01-02", source), MRN)
                self.assertEqual(result.birthday, date(expected, 1, 2))

    def test_typed_registration_and_raw_columns_keep_blank_status(self):
        result = parse_registration_records(registration(day="115-11-13"), MRN)[0]
        self.assertEqual(result.visit_date, date(2026, 11, 13))
        self.assertEqual((result.section_code, result.section_name), ("70", "眼科上午"))
        self.assertEqual((result.room, result.sequence_no), ("02", "001"))
        self.assertEqual(result.status, "")
        self.assertEqual(len(result.columns), len(HEADERS))
        self.assertEqual(parse_registration_records(registration(empty=True), MRN), [])

    def test_registration_rejects_wrong_patient_missing_table_and_broken_columns(self):
        for source in (
            registration("WRONG"),
            "<html>maintenance</html>",
            '<table id="row"><tr><th>病歷號</th></tr><tr><td>A</td><td>B</td></tr></table>',
            registration(day="115-19-90"),
        ):
            with self.subTest(source=source[:25]), self.assertRaises(ParseError):
                parse_registration_records(source, MRN)

    def test_form_needs_a_fresh_nonempty_token(self):
        self.assertEqual(parse_query_form(landing(), "QUY15WForm")[TOKEN], "fresh-token")
        for source in (landing(token=""), '<form id="QUY15WForm"></form>', "<form></form>"):
            with self.assertRaises(ParseError):
                parse_query_form(source, "QUY15WForm")

    def test_pagination_does_not_confuse_sort_or_last_links_with_next(self):
        source = (
            '<a href="?d-123-s=1">next sort</a><a href="?d-123-p=9">&gt;&gt;</a>'
            '<a href="?d-123-p=2" rel="next">2</a>'
        )
        self.assertEqual(find_next_displaytag_href(source), "?d-123-p=2")


class WebMaasAdapterTests(unittest.TestCase):
    def test_basic_info_uses_fresh_form_and_quy_patient_check(self):
        adapter, session, auth = adapter_with(
            response(landing()), identity(), response(basic_info())
        )
        self.assertEqual(adapter.get_basic_info(MRN).mrn, MRN)
        auth.ensure_webmaas_page.assert_called_once_with("QUY15W001")
        calls = session.request.call_args_list
        self.assertEqual([c.args[0] for c in calls], ["GET", "POST", "POST"])
        self.assertEqual(calls[1].kwargs["params"]["pageid"], "QUY15W001")
        payload = calls[2].kwargs["data"]
        self.assertEqual(payload[TOKEN], "fresh-token")
        self.assertEqual((payload["patno"], payload["hcaseno"], payload["type"]), (MRN, "", "A"))
        self.assertTrue(calls[2].kwargs["headers"]["Referer"].endswith("/QUY/QUY15W001.do"))

    def test_reauthentication_rebuilds_token_and_patient_context(self):
        adapter, session, auth = adapter_with(
            response(landing(token="old-token")),
            identity(),
            response("expired", 403),
            response(landing(token="new-token")),
            identity(),
            response(basic_info()),
        )
        adapter.get_basic_info(MRN)
        auth.login.assert_called_once_with(force=True)
        self.assertEqual(session.request.call_args_list[-1].kwargs["data"][TOKEN], "new-token")

    def test_invalid_patient_input_stops_before_network(self):
        adapter, session, _ = adapter_with()
        for method in (
            adapter.get_basic_info,
            adapter.get_demographics,
            adapter.get_registration_history,
        ):
            with self.assertRaises(ConfigurationError):
                method("../bad")
        session.request.assert_not_called()

    def test_registration_fetches_next_page_and_deduplicates_rows(self):
        next_page = f"?d-123-p=2&patno={MRN}"
        adapter, session, _ = adapter_with(
            response(landing("RSV11WForm")),
            identity(),
            response(registration(next_href=next_page)),
            response(registration()),
        )
        self.assertEqual(len(adapter.get_registration_history(MRN)), 1)
        self.assertEqual(session.request.call_count, 4)
        self.assertIn("d-123-p=2", session.request.call_args_list[-1].args[1])

    def test_pagination_loop_and_foreign_patient_or_origin_never_return_partial_results(self):
        for link, code in (
            (
                "https://foreign.test/webmaas/RSV/RSV11W001.do?d-1-p=2",
                "WEBMAAS_REGISTRATION_PAGE_INVALID",
            ),
            ("?d-1-p=2&patno=WRONG", "WEBMAAS_REGISTRATION_PAGE_INVALID"),
            ("?d-1-p=2", "WEBMAAS_REGISTRATION_PAGINATION_LOOP"),
        ):
            adapter, session, _ = adapter_with(
                response(landing("RSV11WForm")),
                identity(),
                response(registration(next_href=link)),
                response(registration(next_href=link)),
            )
            with self.subTest(link=link), self.assertRaises(ParseError) as caught:
                adapter.get_registration_history(MRN)
            self.assertEqual(caught.exception.info.code, code)
            self.assertEqual(session.request.call_count, 4 if "LOOP" in code else 3)

    def test_basic_info_identity_is_checked_again_after_ajax(self):
        adapter, _, _ = adapter_with(response(landing()), identity(), response(basic_info("WRONG")))
        with self.assertRaises(ParseError):
            adapter.get_basic_info(MRN)


class WebMaasRoleTests(unittest.TestCase):
    def test_sso_switches_recorded_roles_with_fresh_keys_and_caches_only_current_role(self):
        settings = SDKSettings(
            webmaas_base_url="https://synthetic.test/webmaas",
            sectord_base_url="https://synthetic.test/SectOrdWeb",
        )
        transport = MagicMock(spec=SafeSessionTransport)
        transport.text.side_effect = lambda value: value.text
        calls = []

        def request(method, url, **kwargs):
            calls.append((url, kwargs))
            if url.endswith("/so.do"):
                return response(f"ssID=fresh-{len(calls)}&keyOne=1&keyTwo=2&keyThree=3")
            result = response("<html><title>synthetic query</title></html>")
            result.url = kwargs["params"]["targetURL"]
            return result

        transport.request.side_effect = request
        auth = AuthenticationAdapter(
            settings=settings,
            credentials=PortalCredentials("SYNTHETIC", "SECRET"),
            transport=transport,
        )
        auth._portal_authenticated = True
        auth._apps["sectord"] = AppSession("sectord", "FRESH-HID", settings.sectord_base_url)
        auth.ensure_webmaas_page("QUY15W001")
        auth.ensure_webmaas_page("QUY15W001")
        auth.ensure_webmaas_page("RSV11W001")
        auth.ensure_webmaas_page("QUY15W001")
        sso = [kwargs["params"] for url, kwargs in calls if url.endswith("WPSAutoLogon")]
        self.assertEqual(
            [p["externalRoles"] for p in sso], ["maas_QRY15", "maas_RSV11", "maas_QRY15"]
        )
        self.assertEqual(len({p["ssID"] for p in sso}), 3)
        self.assertTrue(sso[0]["targetURL"].endswith("/QUY/QUY15W001.do"))
        self.assertEqual(len(calls), 6)


class SDKBoundaryTests(unittest.TestCase):
    def test_patient_example_preserves_diagnostics_and_selects_only_needed_services(self):
        from vghks_sdk.live.atomic import build_test_plan
        from vghks_sdk.live.config import load_live_test_config, resolve_live_test_config

        config = resolve_live_test_config(
            json_values=load_live_test_config(ROOT / "configs/patient-queries.example.json"),
            environ={},
        )
        plan = build_test_plan(config)
        self.assertEqual(plan["auth_targets"], ["portal", "sectord", "webmaas"])
        self.assertEqual(len(plan["operations"]), 3)
        self.assertTrue(plan["network_checks"])
        self.assertFalse(plan["weekly_opd_soap"]["enabled"])

    def test_plain_sdk_import_does_not_load_live_or_offline_tools(self):
        code = (
            "import sys; sys.path.insert(0,sys.argv[1]); import vghks_sdk; "
            "assert not any(n.startswith(('vghks_sdk.live','vghks_sdk.offline',"
            "'vghks_sdk.contracts','vghks_sdk.cli')) for n in sys.modules); "
            "from vghks_sdk import LiveTestConfig; "
            "assert LiveTestConfig.__module__ == 'vghks_sdk.live.config'"
        )
        subprocess.run(
            [sys.executable, "-c", code, str(ROOT / "src")], check=True, capture_output=True
        )

    def test_model_imports_remain_compatible(self):
        from vghks_sdk.models import HtmlDocument, PatientDemographics
        from vghks_sdk.models.patients import PatientDemographics as GroupedPatient
        from vghks_sdk.operation_models import HtmlDocument as CompatibleDocument

        self.assertIs(PatientDemographics, GroupedPatient)
        self.assertIs(HtmlDocument, CompatibleDocument)


@unittest.skipUnless(os.environ.get("VGHKS_RUN_HAR_CONTRACT") == "1", "opt-in HAR fixtures")
class NewPatientHarTests(unittest.TestCase):
    def test_recorded_registration_and_basic_info_without_network(self):
        directory = ROOT / "data/recordings/2026-09-20"
        files = list(directory.glob("查詢病人掛號資料+病人基本資料.har"))
        if not files:
            self.skipTest("local HAR unavailable")
        entries = json.loads(files[0].read_text(encoding="utf-8-sig"))["log"]["entries"]
        results = {}
        with patch("requests.sessions.Session.request", side_effect=AssertionError("offline only")):
            for entry in entries:
                request = entry["request"]
                if request["method"] != "POST":
                    continue
                form = {
                    p["name"]: p["value"] for p in request.get("postData", {}).get("params", [])
                }
                source = entry["response"]["content"].get("text", "")
                if "/QUY/QUY15W001.do" in request["url"]:
                    info = parse_patient_basic_info(source, form["patno"])
                    results["basic"] = len(info.fields)
                    self.assertEqual(len(info.admission_diagnosis), 2)
                    self.assertEqual(len(info.notices), 1)
                elif "/RSV/RSV11W001.do" in request["url"]:
                    results["registration"] = len(parse_registration_records(source, form["patno"]))
        self.assertEqual(results, {"basic": 33, "registration": 7})


if __name__ == "__main__":
    unittest.main()
