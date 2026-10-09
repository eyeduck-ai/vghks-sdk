"""Synthetic attendance evidence and sequencing; no real account or socket."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

from vghks_sdk import (
    AttendanceQuery,
    PortalCredentials,
    RequestPolicy,
    SDKSettings,
    VghksSDK,
    acquire,
)
from vghks_sdk.adapters.attendance import AttendanceAdapter
from vghks_sdk.adapters.auth import AuthenticationAdapter
from vghks_sdk.core.connections import TLSConnectionManager
from vghks_sdk.core.errors import (
    AuthenticationError,
    ConfigurationError,
    ParseError,
    RequestError,
    error_info,
)
from vghks_sdk.core.operations import operation_spec
from vghks_sdk.core.transport import SafeSessionTransport
from vghks_sdk.live.atomic import _query_inputs, build_test_plan
from vghks_sdk.live.config import LiveTestConfig
from vghks_sdk.offline.replay import identify_operation, replay_response
from vghks_sdk.parsing.attendance import (
    parse_attendance_history,
    parse_attendance_punch,
    parse_attendance_state,
    parse_attendance_wait_target,
)
from vghks_sdk.queries import Queries, query_spec
from vghks_sdk.runtime import SDKRuntime
from vghks_sdk.services.attendance import AttendanceService

ACCOUNT = "TST1"
NAME = "合成員工"
PATH = "/PSPDPortal/oFSchedule.do"
BASE = "https://wac01p.vghks.gov.tw:4430/PSPDPortal"
START = date(2026, 1, 1)
END = date(2026, 1, 2)


def status_page(account=ACCOUNT, name=NAME, last="2026-01-01 08:00 地點:TESTROOM1"):
    return f'''<html><form name="pSPDForm" method="post" action="{PATH}">
    <input type="hidden" name="reqCode" value="setPCClockInLog">
    <fieldset><legend>[999-合成單位　{name}({account})]</legend>
    <table><tr><td><input type="submit" value="請按我"></td></tr>
    <tr><td>上次電腦簽到退記錄:{last}<br>電腦序號:TESTPC1</td></tr></table>
    </fieldset></form></html>'''


def history_page(rows=None, *, name=NAME, start=START, end=END, checked=None, ack=False):
    if rows is None:
        rows = (("2026-01-01 08:00", "電腦:TESTROOM1"), ("2026-01-01 17:00", "電腦:TESTROOM1"))
    entries = "".join(
        f'<tr id="row0"><td>{stamp}</td><td>{description}</td></tr>' for stamp, description in rows
    )

    def date_value(value):
        return value.isoformat() if value else ""

    return f'''<html><form name="pSPDForm" method="post" action="{PATH}">
    <fieldset><legend>[<span id="spanUsrName">{name}</span>出勤簽到退查詢]</legend>
    <input type="hidden" name="reqCode" value="getProcessedFingerLog">
    <input name="value(begDate)" value="{date_value(start)}">
    <input name="value(endDate)" value="{date_value(end)}"><input name="b1" value="查詢" type="submit">
    <input type="radio" name="value(qryType)" value="qryProcess" {"checked" if checked == "processed" else ""}>
    <input type="radio" name="value(qryType)" value="qryFinMachine" {"checked" if checked == "raw" else ""}>
    <ul>{"<li>簽到退成功:2026-01-01 17:00 地點:TESTROOM1</li>" if ack else ""}</ul>
    <div><span id="empname">{name}</span></div><table id="drlistTb">
    <tr id="head"><th>日期時間</th><th>說明</th></tr>{entries}
    <tr id="tfoot"><td colspan="2">查詢結果：共有~ {len(rows)} ~筆資料</td></tr>
    </table></fieldset></form></html>'''


def response(body, *, status=200, url=BASE + "/oFSchedule.do", encoding="utf-8", location=None):
    result = requests.Response()
    result.status_code = status
    result.url = url
    result.headers["Content-Type"] = (
        f"text/html; charset={'BIG5' if encoding == 'cp950' else 'UTF-8'}"
    )
    if location:
        result.headers["Location"] = location
    result._content = body.encode(encoding)
    result._content_consumed = True
    return result


def adapter_with(*responses, attempts=3):
    session = requests.Session()
    session.request = MagicMock(side_effect=responses)
    auth = MagicMock(spec=AuthenticationAdapter)
    auth.assert_not_expired = MagicMock()
    auth.generation = 1
    auth.credentials = PortalCredentials(ACCOUNT, "synthetic-secret")
    auth.take_attendance_landing.return_value = ""
    policy = RequestPolicy(min_delay_seconds=0, max_delay_seconds=0, max_attempts=attempts)
    transport = SafeSessionTransport(policy=policy, session=session, sleeper=lambda _: None)
    runtime = SDKRuntime(settings=SDKSettings(), transport=transport, auth=auth)
    return AttendanceAdapter(runtime), session, auth


class AttendanceParserTests(unittest.TestCase):
    def test_dates_and_modes_validate_before_network(self):
        self.assertEqual(
            AttendanceQuery(START, START + timedelta(days=180), "raw").to_form()["value(qryType)"],
            "qryFinMachine",
        )
        for args in (
            (END, START),
            (START, START + timedelta(days=181)),
            ("2026-01-01", END),
            (datetime(2026, 1, 1), END),
            (START, END, "unknown"),
            (START, END, []),
        ):
            with self.subTest(args=args), self.assertRaises(ConfigurationError):
                AttendanceQuery(*args)

    def test_state_account_location_and_minute_precision(self):
        state = parse_attendance_state(status_page(), expected_employee_id="tst1")
        self.assertEqual(state.last_punch_at, datetime(2026, 1, 1, 8))
        self.assertEqual(state.last_location_code, "TESTROOM1")
        self.assertEqual(state.computer_serial, "TESTPC1")
        self.assertIsNone(state.last_punch_at.tzinfo)
        self.assertNotIn(ACCOUNT, repr(state))
        self.assertIsNone(parse_attendance_state(status_page(last="地點:")).last_punch_at)

    def test_state_schema_and_account_fail_closed(self):
        samples = (
            status_page(account="OTHER"),
            status_page().replace("setPCClockInLog", "unknown"),
            status_page().replace('type="submit"', 'type="submit" disabled'),
            status_page().replace("</form>", '<input name="location" value="CLIENT"></form>'),
            "<html>unexpected page</html>",
        )
        for page in samples:
            with self.subTest(page_size=len(page)), self.assertRaises(ParseError):
                parse_attendance_state(page, expected_employee_id=ACCOUNT)

    def test_both_requested_modes_remain_distinct_from_unreported_mode(self):
        for mode in ("processed", "raw"):
            query = AttendanceQuery(START, END, mode)
            history = parse_attendance_history(
                history_page(), query=query, expected_state=parse_attendance_state(status_page())
            )
            self.assertEqual(history.query.mode, mode)
            self.assertIsNone(history.reported_mode)
            self.assertEqual(history.account_context, ACCOUNT)
            self.assertEqual(len(history.records), 2)
            self.assertEqual(history.total_count, 2)
            self.assertEqual(history.records[1].location_code, "TESTROOM1")

    def test_duplicate_row_ids_are_retained_and_footer_is_not_a_record(self):
        history = parse_attendance_history(history_page())
        self.assertEqual([r.occurred_at.hour for r in history.records], [8, 17])

    def test_empty_table_is_empty_result_without_identity_inference(self):
        result = acquire(lambda: parse_attendance_history(history_page(rows=())))
        self.assertEqual((result.status, result.data.item_count), ("EMPTY", 0))
        self.assertIsNone(result.value.account_context)
        self.assertEqual(result.value.employee_name, NAME)

    def test_unknown_record_description_keeps_source_without_location_guess(self):
        history = parse_attendance_history(
            history_page(rows=(("2026-01-01 08:00", "合成來源說明"),))
        )
        self.assertEqual(history.records[0].description, "合成來源說明")
        self.assertIsNone(history.records[0].location_code)

    def test_history_unknown_header_row_count_identity_or_period_is_error(self):
        samples = (
            history_page().replace("日期時間", "unknown"),
            history_page().replace("共有~ 2", "共有~ 3"),
            history_page().replace("2026-01-01 08:00", "not-a-date"),
            history_page().replace(f'<span id="empname">{NAME}', '<span id="empname">另一員工'),
            history_page(start=date(2026, 1, 2)),
            history_page(checked="raw"),
            "<html>查無資料</html>",
        )
        for page in samples:
            with self.subTest(size=len(page)), self.assertRaises(ParseError):
                parse_attendance_history(page, query=AttendanceQuery(START, END))
        with self.assertRaises(ParseError):
            parse_attendance_history(
                history_page(name="另一員工"), expected_state=parse_attendance_state(status_page())
            )

    def test_ack_needs_visible_success_and_corresponding_record(self):
        receipt = parse_attendance_punch(history_page(ack=True, start=None, end=None))
        self.assertEqual(receipt.status, "ACKNOWLEDGED")
        self.assertEqual(receipt.record.occurred_at.hour, 17)
        for page in (
            history_page(),
            history_page(ack=True).replace("<li>", '<li style="display:none">'),
            history_page(ack=True).replace(
                "簽到退成功:2026-01-01 17:00", "簽到退成功:2026-01-01 18:00"
            ),
            history_page(ack=True).replace("地點:TESTROOM1", "地點:OTHERROOM"),
        ):
            with self.subTest(size=len(page)), self.assertRaises(ParseError):
                parse_attendance_punch(page)

    def test_wait_page_only_accepts_fixed_literal_read_target(self):
        prefix = '<html><body onload="'
        suffix = '"></body></html>'
        literal = "window.location.replace('/PSPDPortal/oFSchedule.do?reqCode=getPCClockInLog');"
        target = parse_attendance_wait_target(
            prefix + literal + suffix, response_url=BASE + "/access_wait.jsp", base_url=BASE
        )
        self.assertEqual(target, BASE + "/oFSchedule.do?reqCode=getPCClockInLog")
        for candidate in (
            literal.replace("getPCClockInLog", "setPCClockInLog"),
            literal.replace("/PSPDPortal/", "https://untrusted.test/PSPDPortal/"),
            literal + "submit();",
            "callback();",
            literal.replace("getPCClockInLog", "getPCClockInLog#fragment"),
        ):
            with self.subTest(candidate=candidate), self.assertRaises(ParseError):
                parse_attendance_wait_target(
                    prefix + candidate + suffix,
                    response_url=BASE + "/access_wait.jsp",
                    base_url=BASE,
                )


class AttendanceAdapterTests(unittest.TestCase):
    def test_landing_consumed_once_then_status_is_fresh(self):
        adapter, session, auth = adapter_with(response(status_page()))
        auth.take_attendance_landing.side_effect = [status_page(), ""]
        adapter.get_status()
        session.request.assert_not_called()
        adapter.get_status()
        self.assertEqual(session.request.call_count, 1)
        self.assertEqual(session.request.call_args.args[0], "GET")

    def test_query_big5_encoding_and_no_location_payload(self):
        for mode in ("processed", "raw"):
            adapter, session, auth = adapter_with(
                response(status_page()), response(history_page(), encoding="cp950")
            )
            result = adapter.get_records(AttendanceQuery(START, END, mode))
            self.assertEqual(result.query.mode, mode)
            calls = session.request.call_args_list
            self.assertEqual([call.args[0] for call in calls], ["GET", "POST"])
            form = parse_qs(calls[-1].kwargs["data"].decode("ascii"), encoding="cp950")
            self.assertEqual(form["b1"], ["查詢"])
            self.assertEqual(
                set(form), {"reqCode", "value(begDate)", "value(endDate)", "value(qryType)", "b1"}
            )
            self.assertFalse(calls[-1].kwargs["allow_redirects"])
            auth.ensure.assert_called_once_with("attendance")

    def test_account_mismatch_blocks_query_and_punch_before_post(self):
        for operation in (
            lambda a: a.get_records(AttendanceQuery(START, END)),
            lambda a: a.punch(),
        ):
            adapter, session, _ = adapter_with(response(status_page(account="OTHER")))
            with self.assertRaises(ParseError):
                operation(adapter)
            self.assertEqual([c.args[0] for c in session.request.call_args_list], ["GET"])

    def test_read_authentication_recovery_once(self):
        redirect = response("", status=302, location="https://portal.vghks.gov.tw/login.do")
        adapter, session, auth = adapter_with(
            response(status_page()), redirect, response(status_page()), response(history_page())
        )
        result = adapter.get_records(AttendanceQuery(START, END))
        self.assertEqual(len(result.records), 2)
        self.assertEqual(session.request.call_count, 4)
        auth.login.assert_called_once_with(force=True)

    def test_punch_body_only_reqcode_and_single_mutation(self):
        adapter, session, auth = adapter_with(
            response(status_page()),
            response(history_page(ack=True, start=None, end=None), encoding="cp950"),
        )
        receipt = adapter.punch()
        self.assertEqual(receipt.history.account_context, ACCOUNT)
        calls = session.request.call_args_list
        self.assertEqual(calls[-1].kwargs["data"], {"reqCode": "setPCClockInLog"})
        self.assertFalse(calls[-1].kwargs["allow_redirects"])
        auth.login.assert_not_called()

    def test_mutation_network_parser_or_redirect_failure_never_replays(self):
        bad = (
            requests.ConnectionError("synthetic network loss"),
            response(history_page()),
            response("", status=302, location="https://portal.vghks.gov.tw/login.do"),
            response("", status=307, location=BASE + "/oFSchedule.do"),
            response("denied", status=401),
            response("error", status=503),
        )
        for failure in bad:
            adapter, session, auth = adapter_with(response(status_page()), failure)
            with (
                self.subTest(failure_type=type(failure).__name__),
                self.assertRaises(RequestError) as caught,
            ):
                adapter.punch()
            self.assertEqual(caught.exception.info.code, "MUTATION_OUTCOME_UNKNOWN")
            self.assertFalse(caught.exception.info.retry_safe)
            self.assertFalse(caught.exception.info.retry_recommended)
            self.assertIsNotNone(error_info(caught.exception).cause)
            self.assertEqual([c.args[0] for c in session.request.call_args_list], ["GET", "POST"])
            auth.login.assert_not_called()

    def test_service_query_catalog_empty_assessment_and_mutation_exclusion(self):
        history = parse_attendance_history(history_page(rows=()))
        adapter = SimpleNamespace(
            get_records=MagicMock(return_value=history), get_status=MagicMock(), punch=MagicMock()
        )
        service = AttendanceService(adapter)
        sdk = SimpleNamespace(attendance=service)
        result = Queries(sdk).run_result("attendance.records", start=START, end=END, mode="raw")
        self.assertEqual(result.status, "EMPTY")
        self.assertEqual(adapter.get_records.call_args.args[0].mode, "raw")
        with self.assertRaises(ConfigurationError):
            Queries(sdk).run("attendance.punch")
        adapter.punch.assert_not_called()

    def test_live_plan_is_readonly_and_discovers_both_modes(self):
        config = LiveTestConfig(
            profile="atomic",
            only_operations=("attendance.status", "attendance.records"),
            range_start=START,
            range_end=END,
        )
        plan = build_test_plan(config)
        self.assertEqual(plan["auth_targets"], ["attendance"])
        self.assertIn("attendance.punch", plan["excluded_write_operations"])
        self.assertEqual(_query_inputs(query_spec("attendance.status"), config, {}), [{}])
        self.assertEqual(
            [row["mode"] for row in _query_inputs(query_spec("attendance.records"), config, {})],
            ["processed", "raw"],
        )

    def test_own_account_config_has_no_doctor_default_and_allows_endpoint_override(self):
        config = LiveTestConfig(
            profile="atomic",
            only_operations=("attendance.records",),
            range_start=START,
            range_end=END,
            endpoint_overrides={"attendance_base_url": "https://127.0.0.1:18443/PSPDPortal"},
        )
        self.assertIs(config.with_default_doctor(ACCOUNT), config)
        self.assertEqual(
            config.build_settings().attendance_base_url, "https://127.0.0.1:18443/PSPDPortal"
        )
        with self.assertRaises(ConfigurationError):
            LiveTestConfig(
                profile="atomic",
                only_operations=("attendance.records", "prq.opd_patients"),
                range_start=START,
                range_end=END,
            ).validate_for_execution()

    def test_offline_registry_parses_both_queries_and_ack_without_socket(self):
        with patch("socket.socket.connect", side_effect=AssertionError("no network")):
            for mode in ("processed", "raw"):
                params = AttendanceQuery(START, END, mode).to_form()
                request = {
                    "method": "POST",
                    "url": BASE + "/oFSchedule.do",
                    "body": {"text": urlencode(params, encoding="cp950")},
                }
                self.assertEqual(identify_operation(request), "attendance.records")
                result = replay_response(
                    "attendance.records",
                    history_page().encode("cp950"),
                    params,
                    mime="text/html; charset=BIG5",
                )
                self.assertEqual((result["status"], result["record_count"]), ("PARSED", 2))
            ack = replay_response(
                "attendance.punch",
                history_page(ack=True).encode(),
                {"reqCode": "setPCClockInLog"},
                mime="text/html; charset=UTF-8",
            )
            self.assertEqual(ack["status"], "RECORDED_ACK")

    def test_connection_origin_shared_and_constructing_sdk_does_not_connect(self):
        settings = SDKSettings()
        connections = TLSConnectionManager(settings)
        self.assertEqual(connections.origins["attendance"], connections.origins["personnel"])
        with (
            patch("socket.socket.connect", side_effect=AssertionError("no network")),
            VghksSDK(
                settings=settings, credentials=PortalCredentials(ACCOUNT, "synthetic-secret")
            ) as sdk,
        ):
            self.assertIs(sdk.attendance._adapter.runtime, sdk._runtime)
            self.assertFalse(operation_spec("attendance.punch").retry_safe)
            self.assertTrue(operation_spec("attendance.records").retry_safe)


class AttendanceSsoTests(unittest.TestCase):
    def make_sdk(self, *, target=None, redirect_status=302, wait_script=None):
        session = requests.Session()
        session.cookies.set("JSESSIONID", "synthetic-cookie")
        target = target or PATH + "?reqCode=getPCClockInLog"
        self.calls = []

        def dispatch(method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            path = urlsplit(url).path
            if path == "/ssoFromDn.do":
                values = {
                    "HID": "synthetic-hid",
                    "ssID": "fresh-sso",
                    "keyOne": "fresh1",
                    "keyTwo": "fresh2",
                    "keyThree": "fresh3",
                    "USR_ID": ACCOUNT,
                    "wpsHost": "https://wac01p.vghks.gov.tw:4430",
                    "targetURL": target,
                }
                body = (
                    f'<form action="{BASE}/WPSAutoLogon" method="post">'
                    + "".join(
                        f'<input name="{key}" value="{value}">' for key, value in values.items()
                    )
                    + "</form>"
                )
                return response(body, url=url)
            if path == "/PSPDPortal/WPSAutoLogon":
                return response(
                    "",
                    status=redirect_status,
                    url=url,
                    location=BASE + "/access_wait.jsp?targetURL=" + target,
                )
            if path == "/PSPDPortal/access_wait.jsp":
                script = (
                    wait_script
                    or "window.location.replace('/PSPDPortal/oFSchedule.do?reqCode=getPCClockInLog');"
                )
                return response(f'<html><body onload="{script}"></body></html>', url=url)
            if path == PATH:
                actual = url + ("?" + urlencode(kwargs["params"]) if kwargs.get("params") else "")
                return response(status_page(), url=actual)
            return response("ok", url=url)

        session.request = MagicMock(side_effect=dispatch)
        policy = RequestPolicy(min_delay_seconds=0, max_delay_seconds=0)
        transport = SafeSessionTransport(policy=policy, session=session, sleeper=lambda _: None)
        sdk = VghksSDK(
            settings=SDKSettings(),
            credentials=PortalCredentials(ACCOUNT, "synthetic-secret"),
            transport=transport,
        )
        sdk._runtime.auth._portal_authenticated = True
        sdk._runtime.auth._generation = 1
        return sdk

    def test_sso_post_wait_literal_get_and_reuse_once(self):
        sdk = self.make_sdk()
        self.addCleanup(sdk.close)
        sdk.attendance.get_status()
        self.assertEqual(
            [urlsplit(c[1]).path for c in self.calls],
            [
                "/ssoLogAdd.do",
                "/aptreePath.do",
                "/ssoFromDn.do",
                "/PSPDPortal/WPSAutoLogon",
                "/PSPDPortal/access_wait.jsp",
                PATH,
            ],
        )
        post = next(c for c in self.calls if urlsplit(c[1]).path.endswith("/WPSAutoLogon"))
        self.assertEqual(post[2]["data"]["keyOne"], "fresh1")
        self.assertFalse(post[2]["allow_redirects"])
        sdk.attendance.get_status()
        self.assertEqual(sum(urlsplit(c[1]).path == PATH for c in self.calls), 2)

    def test_unknown_wait_target_or_sso_307_never_resends_post(self):
        cases = (
            {"redirect_status": 307},
            {"target": "/PSPDPortal/AP_INFO.jsp"},
            {
                "wait_script": "window.location.replace('/PSPDPortal/oFSchedule.do?reqCode=setPCClockInLog');"
            },
        )
        for fields in cases:
            sdk = self.make_sdk(**fields)
            try:
                with (
                    self.subTest(fields=fields),
                    self.assertRaises((AuthenticationError, ParseError)),
                ):
                    sdk.attendance.get_status()
                self.assertLessEqual(
                    sum(urlsplit(c[1]).path.endswith("/WPSAutoLogon") for c in self.calls), 1
                )
                self.assertFalse(
                    any(c[0] == "POST" and urlsplit(c[1]).path == PATH for c in self.calls)
                )
            finally:
                sdk.close()

    def test_cached_readiness_fetches_status_and_never_punches(self):
        sdk = self.make_sdk()
        self.addCleanup(sdk.close)
        sdk.attendance.get_status()
        report = sdk.auth.check(only=("attendance",))
        self.assertTrue(report.ok)
        self.assertEqual(sum(urlsplit(c[1]).path == PATH for c in self.calls), 2)
        self.assertFalse(any(c[0] == "POST" and urlsplit(c[1]).path == PATH for c in self.calls))


if __name__ == "__main__":
    unittest.main()
