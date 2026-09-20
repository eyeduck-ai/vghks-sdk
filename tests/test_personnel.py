"""Synthetic directory fixtures: never copy employee rows from a real HAR."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from html import escape
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from test_atomic_queries import readiness
from test_webmaas import adapter_with, response

from vghks_sdk import PersonnelFilter, PortalCredentials, SDKSettings, VghksSDK, VisitFilter
from vghks_sdk.adapters.auth import AuthenticationAdapter
from vghks_sdk.adapters.personnel import PersonnelAdapter
from vghks_sdk.contracts.har import load_har
from vghks_sdk.core.errors import AuthenticationError, ConfigurationError, ParseError
from vghks_sdk.live.atomic import _query_inputs, build_test_plan, run_atomic_test
from vghks_sdk.live.config import LiveTestConfig
from vghks_sdk.models.personnel import personnel_employee_id
from vghks_sdk.offline.replay import identify_operation, replay_response
from vghks_sdk.parsing.personnel import parse_personnel_options, parse_personnel_records
from vghks_sdk.queries import Queries, query_spec
from vghks_sdk.services.personnel import PersonnelService

EMPLOYEE = "TST1"
HEADERS = (
    "傳呼 簡訊",
    "姓名",
    "醫師章號",
    "醫師類別",
    "單位",
    "門號",
    "院內分機",
    "電話一",
    "電話二",
    "電話三",
    "詳細 資料",
)


def options_page():
    return """<html><form method="post" action="/DDPortal/dRDoctor.do" target="drInfo">
    <input name="reqCode" value="showAllDoctors"><input name="value(source)" value="DR">
    <input name="value(name)"><input name="value(usrId)">
    <select name="value(title)"><option value=""></option><option value="D">主治醫師</option>
    <option value="R">住院醫師</option><option value=""></option></select>
    <select name="value(costId)"><option value=""></option><option value="TEST">合成單位</option></select>
    <input type="checkbox" name="value(subOU)" value="Y">
    <input type="submit" name="action" value="開始搜尋"></form></html>"""


def directory_row(employee=EMPLOYEE, name="合成醫師", stamp="STMP1", detail_id=None):
    values = [
        f'<input type="checkbox" name="valueA(usrId)" value="{escape(employee)}">',
        escape(name),
        escape(stamp),
        "主治醫師",
        "合成單位",
        "<script>var gsm='0000000000'; document.write(gsm);</script>",
        "0000",
        "000-0000000",
        "",
        "",
        f'<img onclick="listDetail()" name="{escape(detail_id or employee)}" src="images/detail.gif">',
    ]
    return "<tr>" + "".join(f"<td>{v}</td>" for v in values) + "</tr>"


def results_page(*rows):
    return (
        '<html><form action="/DDPortal/dRDoctor.do" method="post"><table id="drlistTb"><tr>'
        + "".join(f"<th>{h}</th>" for h in HEADERS)
        + "</tr>"
        + "".join(rows)
        + '<tr id="tfoot"><td colspan="11">查詢結果：共有 '
        + str(len(rows))
        + ' 筆資料</td></tr><tr><td colspan="11">'
        + '<span style="display:none"><input name="action" type="submit" value="傳呼簡訊"></span>'
        + "</td></tr></table></form></html>"
    )


class PersonnelTests(unittest.TestCase):
    def test_filter_multiple_conditions_and_no_implicit_all(self):
        f = PersonnelFilter(
            name=" 合成 ", employee_id="tst1", title="D", unit="TEST", include_subunits=True
        )
        self.assertEqual(f.to_form()["value(name)"], "合成")
        self.assertEqual(f.to_form()["value(usrId)"], EMPLOYEE)
        self.assertEqual(f.to_form()["value(subOU)"], "Y")
        self.assertEqual(f.to_form()["reqCode"], "showAllDoctors")
        self.assertNotIn("value(subOU)", PersonnelFilter(allow_all=True).to_form())
        for fields in (
            {},
            {"include_subunits": True},
            {"name": "x" * 11},
            {"employee_id": "x" * 7},
            {"name": "x", "allow_all": "false"},
        ):
            with self.subTest(fields=fields), self.assertRaises(ConfigurationError):
                PersonnelFilter(**fields)

    def test_dynamic_options_and_unknown_form_rejected(self):
        options = parse_personnel_options(options_page())
        self.assertEqual([item.value for item in options.titles], ["D", "R"])
        self.assertEqual(options.units[0].label, "合成單位")
        for text in (
            "<html>login</html>",
            options_page().replace("showAllDoctors", "sendPhs"),
            options_page().replace('name="value(costId)"', 'name="different"'),
        ):
            with self.assertRaises(ParseError):
                parse_personnel_options(text)

    def test_table_preserves_identifiers_fields_and_script_gap(self):
        rows = parse_personnel_records(
            results_page(directory_row(), directory_row("TST2", name="同名測試", stamp=""))
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[0].employee_id, rows[0].doctor_stamp_no), (EMPLOYEE, "STMP1"))
        self.assertEqual(rows[0].extension, "0000")
        self.assertEqual(rows[0].phone_numbers, ("000-0000000", "", ""))
        self.assertEqual(rows[0].fields["門號"], "")
        self.assertEqual(rows[0].unrendered_fields, ("門號",))
        self.assertEqual(rows[1].doctor_stamp_no, "")
        self.assertEqual(parse_personnel_records(results_page()), [])

    def test_malformed_and_conflicting_identity_never_become_empty(self):
        for text in (
            "<html>查無資料!</html>",
            results_page(directory_row(detail_id="TST2")),
            results_page(directory_row(), directory_row()),
            results_page(directory_row()).replace("醫師章號", "unknown"),
            results_page(directory_row()).replace("<td>0000</td>", ""),
        ):
            with self.subTest(shape=len(text)), self.assertRaises(ParseError):
                parse_personnel_records(text)

    def test_big5_request_utf8_response_readonly_and_same_runtime(self):
        original, session, auth = adapter_with(
            response(options_page()), response(results_page(directory_row()))
        )
        adapter = PersonnelAdapter(original.runtime)
        rows = adapter.search(
            PersonnelFilter(
                name="合成醫師", employee_id=EMPLOYEE, title="D", unit="TEST", include_subunits=True
            )
        )
        self.assertEqual(rows[0].name, "合成醫師")
        calls = session.request.call_args_list
        self.assertEqual([call.args[0] for call in calls], ["GET", "POST"])
        self.assertEqual(
            [urlsplit(call.args[1]).path for call in calls],
            ["/DDPortal/DRQuerySql.jsp", "/DDPortal/dRDoctor.do"],
        )
        form = parse_qs(calls[1].kwargs["data"].decode("ascii"), encoding="cp950")
        self.assertEqual(form["value(name)"], ["合成醫師"])
        self.assertEqual(form["action"], ["開始搜尋"])
        self.assertEqual(form["value(subOU)"], ["Y"])
        self.assertFalse(calls[1].kwargs["allow_redirects"])
        auth.ensure.assert_called_once_with("personnel")
        self.assertNotIn("valueA(usrId)", form)

    def test_unknown_option_blocks_post_and_unencodable_name_blocks_network(self):
        original, session, _ = adapter_with(response(options_page()))
        adapter = PersonnelAdapter(original.runtime)
        with self.assertRaises(ConfigurationError):
            adapter.search(PersonnelFilter(title="UNKNOWN"))
        self.assertEqual(session.request.call_count, 1)
        session.request.reset_mock()
        with self.assertRaises(ConfigurationError):
            adapter.search(PersonnelFilter(name="\U0001f600"))
        session.request.assert_not_called()

    def test_expired_session_recovers_once_without_resending_login_post(self):
        original, session, auth = adapter_with(
            response("redirect", status=302),
            response(options_page()),
            response(results_page(directory_row())),
        )
        rows = PersonnelAdapter(original.runtime).search(PersonnelFilter(employee_id=EMPLOYEE))
        self.assertEqual(len(rows), 1)
        auth.login.assert_called_once_with(force=True)
        self.assertEqual(
            [call.args[0] for call in session.request.call_args_list], ["GET", "GET", "POST"]
        )

    def test_card_lookup_uses_employee_exact_match_not_stamp_or_first_result(self):
        records = parse_personnel_records(
            results_page(directory_row("TST2", stamp=EMPLOYEE), directory_row())
        )
        adapter = SimpleNamespace(search=Mock(return_value=records))
        service = PersonnelService(adapter)
        self.assertEqual(service.get_by_card("tst1f").employee_id, EMPLOYEE)
        adapter.search.assert_called_once_with(PersonnelFilter(employee_id=EMPLOYEE))
        self.assertEqual(personnel_employee_id("ABCDEF"), "ABCDEF")
        self.assertIsNone(service.get_by_card("NONE"))
        adapter.search.return_value = [records[1], records[1]]
        with self.assertRaises(ParseError):
            service.get_by_card(EMPLOYEE)

    def test_resolved_name_composes_with_visit_filter_without_raw_card(self):
        from datetime import date

        from vghks_sdk import VisitCase

        doctor = parse_personnel_records(results_page(directory_row()))[0]
        case = VisitCase(
            "SYNTHETIC", date(2026, 1, 1), "O", "CASE", "TEST", "合成科別", doctor_name=doctor.name
        )
        self.assertEqual(
            VisitFilter(all_sections=True, doctor_names=(doctor.name,)).select([case]), [case]
        )

    def test_catalog_live_discovery_and_offline_replay(self):
        config = LiveTestConfig(
            profile="atomic", only_operations=("personnel.options", "personnel.search")
        ).with_default_doctor(EMPLOYEE)
        self.assertEqual(build_test_plan(config)["auth_targets"], ["personnel"])
        inputs = _query_inputs(query_spec("personnel.search"), config, {})[0]
        self.assertEqual(inputs["filter"].employee_id, EMPLOYEE)
        sdk = SimpleNamespace(personnel=SimpleNamespace(search=Mock(return_value=[])))
        self.assertEqual(Queries(sdk).run("personnel.search", **inputs), [])
        for key, body in (
            ("personnel.options", options_page()),
            ("personnel.search", results_page(directory_row())),
        ):
            replay = replay_response(key, body.encode("utf-8"), {}, mime="text/html; charset=utf-8")
            self.assertEqual(replay["status"], "PARSED")
        form = PersonnelFilter(employee_id=EMPLOYEE).to_form()
        request = {
            "method": "POST",
            "url": "https://synthetic.test/DDPortal/dRDoctor.do",
            "body": {"value": form},
        }
        self.assertEqual(identify_operation(request), "personnel.search")
        self.assertEqual(
            identify_operation({**request, "body": {"value": {**form, "reqCode": "sendPhs"}}}), ""
        )

    def test_personnel_test_continues_after_options_failure(self):
        sdk = SimpleNamespace(
            auth=SimpleNamespace(check=Mock(return_value=readiness("personnel"))),
            personnel=SimpleNamespace(
                get_options=Mock(side_effect=ParseError("synthetic")), search=Mock(return_value=[])
            ),
        )
        config = LiveTestConfig(
            profile="atomic", only_operations=("personnel.options", "personnel.search")
        ).with_default_doctor(EMPLOYEE)
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            result = run_atomic_test(sdk, config, output_dir=Path(folder))
        self.assertEqual(result.status, "COMPLETED_WITH_ERRORS")
        sdk.personnel.search.assert_called_once()

    def test_sdk_facade_import_construction_does_not_connect(self):
        with (
            patch("requests.sessions.Session.request") as network,
            VghksSDK(
                settings=SDKSettings(), credentials=PortalCredentials(EMPLOYEE, "synthetic")
            ) as sdk,
        ):
            self.assertIs(sdk.personnel._adapter.runtime, sdk._runtime)
            self.assertIn("personnel", sdk.connection_status())
        network.assert_not_called()

    def test_real_sso_uses_fresh_fields_and_validates_query_frame(self):
        original, session, _ = adapter_with()
        settings = original.runtime.settings
        base = settings.personnel_base_url
        form = (
            '<form action="'
            + base
            + '/WPSAutoLogon">'
            + "".join(
                f'<input name="{key}" value="{escape(value)}">'
                for key, value in {
                    "HID": "fresh-hid",
                    "ssID": "fresh-session",
                    "keyOne": "one",
                    "keyTwo": "two",
                    "keyThree": "three",
                    "USR_ID": EMPLOYEE,
                    "wpsHost": base,
                    "targetURL": base + "/DRQuery.jsp",
                }.items()
            )
            + "</form>"
        )

        def dispatch(method, url, **kwargs):
            result = response(form if url.endswith("/ssoFromDn.do") else "ok")
            result.url = url
            if url.endswith("/WPSAutoLogon"):
                self.assertEqual(kwargs["data"]["HID"], "fresh-hid")
                self.assertEqual(kwargs["data"]["USR_ID"], EMPLOYEE)
                result = response(
                    '<frameset><frame src="DRQuerySql.jsp"><frame src="blank.htm"></frameset>'
                )
                result.url = base + "/DRQuery.jsp"
            return result

        session.request.side_effect = dispatch
        auth = AuthenticationAdapter(
            settings=settings,
            credentials=PortalCredentials(EMPLOYEE, "synthetic"),
            transport=original.runtime.transport,
        )
        auth._portal_authenticated = True
        first = auth.ensure("personnel")
        self.assertEqual(first.hid, "fresh-hid")
        self.assertIs(auth.ensure("personnel"), first)
        self.assertEqual(session.request.call_count, 4)
        self.assertEqual(
            sum(
                call.args[1].endswith("/DDPortal/WPSAutoLogon")
                for call in session.request.call_args_list
            ),
            1,
        )
        foreign = replace(settings.profiles["personnel"], expected_base_url="https://foreign.test")
        with self.assertRaises(AuthenticationError):
            auth._parse_sso_form(form, foreign)

        def broken_landing(method, url, **kwargs):
            result = dispatch(method, url, **kwargs)
            if url.endswith("/WPSAutoLogon"):
                result._content = b"<html>temporary error</html>"
            return result

        auth._apps.clear()
        session.request.side_effect = broken_landing
        with self.assertRaises(AuthenticationError):
            auth.ensure("personnel")
        self.assertNotIn("personnel", auth._apps)

    def test_result_total_detects_truncation_without_executing_javascript(self):
        body = results_page(directory_row())
        with self.assertRaises(ParseError):
            parse_personnel_records(body.replace("共有 1 筆", "共有 2 筆"))
        counter = '<script>document.write("~"+eval(0+1)+"~");</script>'
        self.assertEqual(
            len(parse_personnel_records(body.replace("共有 1 筆", "共有 " + counter + " 筆"))), 1
        )

    def test_big5_query_form_decodes_chinese_options(self):
        page = response(options_page())
        page._content = options_page().encode("cp950")
        page.headers["Content-Type"] = "text/html; charset=BIG5"
        original, _, _ = adapter_with(page)
        options = PersonnelAdapter(original.runtime).get_options()
        self.assertEqual(options.titles[0].label, "主治醫師")

    @unittest.skipUnless(os.environ.get("VGHKS_RUN_HAR_CONTRACT") == "1", "private HAR opt-in")
    def test_private_recordings(self):
        folder = Path(__file__).resolve().parents[1] / "data/recordings/2026-09-21"
        if not folder.is_dir():
            self.skipTest("private recordings unavailable")
        counts, options = [], []
        for path in folder.glob("*.har"):
            for entry in load_har(path).entries:
                if entry.path.endswith("DRQuerySql.jsp") and entry.has_body:
                    value = parse_personnel_options(entry.text())
                    options.append((len(value.titles), len(value.units)))
                if entry.path.endswith("dRDoctor.do") and entry.has_body:
                    counts.append(len(parse_personnel_records(entry.text())))
        self.assertEqual(counts, [323])
        self.assertEqual(options, [(8, 73)])


if __name__ == "__main__":
    unittest.main()
