from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import requests

from vghks_sdk import (
    EarningsCredentials,
    PortalCredentials,
    RequestPolicy,
    SDKSettings,
    SurgeryScheduleProcedure,
)
from vghks_sdk.adapters.auth import AuthenticationAdapter
from vghks_sdk.adapters.earnings import EarningsAdapter
from vghks_sdk.adapters.oppl import OpplAdapter
from vghks_sdk.adapters.prq_extensions import parse_patient_flags, parse_text_history
from vghks_sdk.core.errors import (
    AuthenticationError,
    ConfigurationError,
    LoginRejectedError,
    ParseError,
    RequestError,
)
from vghks_sdk.core.operations import OPERATIONS, operation_spec
from vghks_sdk.core.transport import SafeSessionTransport
from vghks_sdk.live.atomic import build_test_plan
from vghks_sdk.live.config import LiveTestConfig, resolve_live_test_config
from vghks_sdk.models import PdfAttachmentRef
from vghks_sdk.operation_models import EarningsReportContext, FormSnapshot
from vghks_sdk.parsing.documents import parse_form
from vghks_sdk.parsing.oppl import parse_surgery_records
from vghks_sdk.queries import QUERY_SPECS
from vghks_sdk.runtime import SDKRuntime
from vghks_sdk.surgery_commands import prepare_command

ROOT = Path(__file__).resolve().parents[1]
HAR_ROOT = ROOT / "data/recordings/2026-09-19"
POLICY = RequestPolicy(
    min_delay_seconds=0, max_delay_seconds=0, max_attempts=3, backoff_base_seconds=0
)


def response(body: str, status=200, url="https://example.test/OPPLWeb/surgAction.do"):
    result = requests.Response()
    result.status_code = status
    result.url = url
    result.headers["Content-Type"] = "text/plain; charset=utf-8"
    result._content = body.encode("utf-8")
    result._content_consumed = True
    return result


def runtime_with(*responses):
    session = requests.Session()
    session.request = MagicMock(side_effect=responses)
    auth = MagicMock(spec=AuthenticationAdapter)
    auth.assert_not_expired = MagicMock()
    auth.credentials = PortalCredentials("SYNTHETIC", "portal-test-secret")
    auth.hid_for.return_value = "FRESH-HID"
    auth.generation = 1
    transport = SafeSessionTransport(policy=POLICY, session=session, sleeper=lambda _: None)
    runtime = SDKRuntime(
        settings=SDKSettings(request_policy=POLICY), auth=auth, transport=transport
    )
    return runtime, session, auth


def schedule_fields():
    return dict(
        orhisnum="SYNTHETIC",
        orcasetp="O",
        orcaseno="CASE",
        opdate="2026-09-19",
        optime="0900",
        opsectselect="OPH",
        doctId="SYNTHETIC",
        searchOPCode1="TEST",
    )


class AtomicRecordedTests(unittest.TestCase):
    def test_mutations_never_enter_default_or_selected_query_tests(self):
        writes = {spec.key for spec in OPERATIONS if spec.mutates}
        self.assertEqual(len(writes), 4)
        self.assertFalse(writes & {spec.key for spec in QUERY_SPECS})
        self.assertEqual(
            set(
                build_test_plan(LiveTestConfig(profile="comprehensive"))[
                    "excluded_write_operations"
                ]
            ),
            writes,
        )
        for key in writes:
            with self.assertRaises(ConfigurationError):
                LiveTestConfig(profile="atomic", only_operations=(key,))

    def test_timeout_http_failure_redirect_or_unknown_reply_never_resends_write(self):
        for result in (
            requests.Timeout(),
            response("", 503),
            response("", 403),
            response("redirect", 307),
            response("unknown acknowledgment"),
        ):
            with self.subTest(result=type(result).__name__):
                runtime, session, auth = runtime_with(result)
                adapter = OpplAdapter(runtime)
                command = prepare_command("create_schedule", schedule_fields())
                with self.assertRaises(RequestError) as caught:
                    adapter.submit_command(command)
                self.assertEqual(caught.exception.info.code, "MUTATION_OUTCOME_UNKNOWN")
                self.assertEqual(session.request.call_count, 1)
                self.assertIs(session.request.call_args.kwargs["allow_redirects"], False)
                auth.login.assert_not_called()
                with self.assertRaises(ConfigurationError):
                    adapter.submit_command(command)
                self.assertEqual(session.request.call_count, 1)

    def test_success_receipt_requires_server_id_and_uses_fresh_hid(self):
        body = json.dumps({"msg": "手術排程新增成功!!!", "orreqno": "SYNTHETIC-ID"})
        runtime, session, _ = runtime_with(response(body))
        command = prepare_command(
            "create_schedule", {**schedule_fields(), "hid": "OLD-HID", "method": "doCancel"}
        )
        receipt = OpplAdapter(runtime).submit_command(command)
        self.assertEqual(receipt.status, "ACKNOWLEDGED")
        self.assertEqual(receipt.command_id, command.command_id)
        data = dict(session.request.call_args.kwargs["data"])
        self.assertEqual(data["hid"], "FRESH-HID")
        self.assertEqual(data["method"], "doSave")

    def test_prepare_preserves_supply_arrays_and_requires_cancel_reason(self):
        pairs = [
            *schedule_fields().items(),
            ("ncopaplynisin", "A"),
            ("ncopaplyprice", "1"),
            ("ncopaplynum", "2"),
            ("ncopaplynisin", "B"),
            ("ncopaplyprice", "3"),
            ("ncopaplynum", "4"),
        ]
        command = prepare_command("create_schedule", pairs)
        self.assertEqual([v for k, v in command.fields if k == "ncopaplynisin"], ["A", "B"])
        with self.assertRaises(ConfigurationError):
            prepare_command("create_schedule", pairs[:-1])
        with self.assertRaises(ConfigurationError):
            prepare_command(
                "cancel_schedule", {**schedule_fields(), "orreqno": "REQ", "ordseqno": "1"}
            )

    def test_new_consent_cannot_be_used_to_delete_or_edit_existing_consent(self):
        fields = dict(
            hhisnum="SYNTHETIC",
            hcasetyp="O",
            hcaseno="CASE",
            formCode="FORM",
            opdoctId="SYNTHETIC",
            drsect="OPH",
            surgreqno="REQ",
            status="D",
        )
        with self.assertRaises(ConfigurationError):
            prepare_command("create_consent", fields)
        with self.assertRaises(ConfigurationError):
            prepare_command("create_consent", {**fields, "status": "A", "rwrecno": "EXISTING"})

    def test_form_snapshot_retains_checked_repeated_fields_and_options(self):
        html = '<form id="openForm"><input name="x" value="old" disabled><input name="declare" value="1" type="checkbox" checked><input name="declare" value="2" type="checkbox" checked><input name="declare" value="3" type="checkbox"><select name="choice"><option value="a">A</option><option value="b" selected>B</option></select></form>'
        snapshot = parse_form(html, "openForm")
        self.assertEqual(snapshot.fields, (("declare", "1"), ("declare", "2"), ("choice", "b")))
        self.assertEqual(len(snapshot.choices["choice"]), 2)

    def test_schedule_date_uses_planned_date_and_does_not_hide_invalid_json(self):
        rows = parse_surgery_records(
            {
                "surgs": [
                    {
                        "orhisnum": "SYNTHETIC",
                        "orbgndt": {"dts": "2026-10-01"},
                        "ordate": {"dts": "2026-09-19"},
                        "orstatus": "SCHEDULED",
                    }
                ]
            }
        )
        self.assertEqual(rows[0].surgery_date, "2026-10-01")
        self.assertEqual(rows[0].status, "SCHEDULED")
        self.assertEqual((rows[0].extra or {})["orstatus"], "SCHEDULED")
        with self.assertRaises(ParseError):
            parse_surgery_records({"error": "not allowed"})

    def test_schedule_matches_visible_columns_and_keeps_tf_unconfirmed(self):
        rows = parse_surgery_records(
            {
                "surgs": [
                    {
                        "orhisnum": "SYNTHETIC-1",
                        "orbgndt": {"dts": "2026-10-01"},
                        "orbgntm": {"dts": "23:59:00"},
                        "optime": "TF1",
                        "oproom": "VISIBLE-ROOM",
                        "oroproom": "INTERNAL-ROOM",
                        "oropamed": "LA",
                        "orfreqnc": "routine",
                        "orcatgy": "OPH",
                        "ordocnm": "VISIBLE-DOCTOR",
                        "ordocnam": "OTHER-DOCTOR",
                        "patient": {
                            "hnursta": "W1",
                            "hbedno": "02",
                            "hnamec": "SYNTHETIC-PATIENT",
                            "hsexc": "F",
                        },
                    },
                    {
                        "orhisnum": "SYNTHETIC-2",
                        "orbgntm": {"dts": "08:30:00"},
                        "optime": "0830",
                        "oproom": "A6",
                        "patient": {"hnursta": "OPD"},
                    },
                ]
            }
        )
        unconfirmed, clock_time = rows
        self.assertEqual(unconfirmed.surgery_date, "2026-10-01")
        self.assertEqual(unconfirmed.patient_mrn, "SYNTHETIC-1")
        self.assertEqual(unconfirmed.ward, "W1-02")
        self.assertEqual(unconfirmed.room, "VISIBLE-ROOM")
        self.assertEqual(unconfirmed.anesthesia, "LA")
        self.assertEqual(unconfirmed.category, "routine")
        self.assertEqual(unconfirmed.patient_name, "SYNTHETIC-PATIENT")
        self.assertEqual(unconfirmed.patient_sex, "F")
        self.assertEqual(unconfirmed.department, "OPH")
        self.assertEqual(unconfirmed.doctor_name, "VISIBLE-DOCTOR")
        self.assertEqual(unconfirmed.schedule_time, "TF1")
        self.assertEqual(unconfirmed.time_status, "UNCONFIRMED")
        self.assertEqual(unconfirmed.start_time, "")
        self.assertEqual(clock_time.ward, "OPD")
        self.assertEqual(clock_time.schedule_time, "0830")
        self.assertEqual(clock_time.time_status, "CLOCK_TIME")
        self.assertEqual(clock_time.start_time, "08:30:00")

    def test_schedule_exposes_additional_identifiers_codes_and_full_source(self):
        raw = {
            "orhisnum": "SYNTHETIC",
            "orreqno": "REQ-1",
            "ordseqno": "2",
            "orcasetp": "O",
            "oproom": "VISIBLE-ROOM",
            "oroproom": "INTERNAL-ROOM",
            "oropnc1": "PROC-1",
            "oropnm1": "FIRST PROCEDURE",
            "oropnm3": "THIRD PROCEDURE",
            "oropicd1": "D1",
            "oropicd2": "D2",
            "ordiag": "SYNTHETIC DIAGNOSIS",
            "patient": {"hnursta": "OPD", "hnamec": "SYNTHETIC PATIENT"},
            "unknown": {"detail": "kept"},
        }
        (record,) = parse_surgery_records({"surgs": [raw]})
        self.assertEqual(
            (record.request_no, record.sequence_no, record.case_type), ("REQ-1", "2", "O")
        )
        self.assertEqual(record.internal_room_code, "INTERNAL-ROOM")
        self.assertEqual(
            record.procedures,
            (
                SurgeryScheduleProcedure(1, "PROC-1", "FIRST PROCEDURE"),
                SurgeryScheduleProcedure(3, "", "THIRD PROCEDURE"),
            ),
        )
        self.assertEqual(record.diagnosis_codes, ("D1", "D2"))
        self.assertEqual(record.diagnosis_text, "SYNTHETIC DIAGNOSIS")
        self.assertEqual(record.source_fields, raw)
        self.assertNotIn("oroproom", record.extra or {})
        raw["patient"]["hnamec"] = "CHANGED AFTER PARSE"
        self.assertEqual(record.source_fields["patient"]["hnamec"], "SYNTHETIC PATIENT")
        self.assertNotIn("source_fields", repr(record))

    def test_schedule_rejects_mismatched_nested_patient(self):
        with self.assertRaises(ParseError) as caught:
            parse_surgery_records(
                {
                    "surgs": [
                        {
                            "orhisnum": "SYNTHETIC-1",
                            "patient": {"hhisnum": "SYNTHETIC-2", "hnamec": "OTHER"},
                        }
                    ]
                }
            )
        self.assertEqual(caught.exception.info.code, "OPPL_SURGERY_PATIENT_MISMATCH")

    def test_patient_json_and_text_links_reject_wrong_patient(self):
        with self.assertRaises(ParseError):
            parse_patient_flags(
                "allergy", {"hhisnum": "OTHER", "allergy": "", "allergyMsg": ""}, "SYNTHETIC"
            )
        with self.assertRaises(ParseError):
            parse_text_history(
                "QueryResText.do?hhisnum=OTHER&caseNo=C&caseType=O&seqNo=1", "SYNTHETIC", "RAD"
            )

    def test_new_emru_paths_keep_patient_and_host_validation(self):
        self.assertEqual(
            PdfAttachmentRef("SYNTHETIC", "//nfs01p/EMRU/SYNTHETIC/record.pdf").mrn, "SYNTHETIC"
        )
        for path in (
            "//evil.test/EMRU/SYNTHETIC/record.pdf",
            "//nfs01p/EMRU/OTHER/record.pdf",
            "//nfs01p/EMRU/SYNTHETIC/../record.pdf",
        ):
            with self.assertRaises(ConfigurationError):
                PdfAttachmentRef("SYNTHETIC", path)

    def test_earnings_config_roundtrip_contains_no_password(self):
        config = LiveTestConfig(profile="comprehensive", include_earnings=True)
        restored = resolve_live_test_config(json_values=config.to_safe_dict(), environ={})
        self.assertTrue(restored.include_earnings)
        self.assertNotIn("password", config.to_safe_dict())

    def test_form_open_binds_patient_before_each_request_and_preserves_mode(self):
        responses = []
        for mrn in ("SYNTHETIC-A", "SYNTHETIC-B"):
            responses.extend(
                (
                    response(json.dumps({"patient": {"hhisnum": mrn}, "surgs": []})),
                    response(
                        '<form id="openForm"><input name="orhisnum" value="' + mrn + '"></form>'
                    ),
                )
            )
        runtime, session, _ = runtime_with(*responses)
        adapter = OpplAdapter(runtime)
        for mrn, mode in (("SYNTHETIC-A", "create"), ("SYNTHETIC-B", "cancel")):
            snapshot = adapter.open_schedule_form(
                {"orhisnum": mrn, "orreqno": "REQ", "orbgndt": {"dts": "2026-09-19"}}, mode=mode
            )
            self.assertEqual(dict(snapshot.fields)["orhisnum"], mrn)
        calls = session.request.call_args_list
        self.assertEqual(calls[0].kwargs["data"]["hhisnum"], "SYNTHETIC-A")
        self.assertEqual(calls[2].kwargs["data"]["hhisnum"], "SYNTHETIC-B")
        self.assertEqual(dict(calls[1].kwargs["data"])["status"], "S")
        self.assertEqual(dict(calls[3].kwargs["data"])["status"], "C")
        self.assertEqual(dict(calls[3].kwargs["data"])["orbgndt[dts]"], "2026-09-19")

    def test_mis_cross_host_redirect_or_password_redirect_never_forwards_credentials(self):
        for key, status, destination in (
            ("mis.performance_entry", 302, "https://other.test/VGHK/Pswdchk.asp"),
            ("mis.password", 307, "https://other.test/receive"),
            ("mis.password", 302, "/VGHK/Pswdchk.asp"),
        ):
            with self.subTest(key=key, status=status):
                reply = response("", status)
                reply.headers["Location"] = destination
                runtime, session, _ = runtime_with(reply)
                spec = operation_spec(key)
                with self.assertRaises(AuthenticationError):
                    EarningsAdapter(runtime)._request(
                        spec, runtime.settings.mis_base_url + spec.path, data={"test": "SYNTHETIC"}
                    )
                self.assertEqual(session.request.call_count, 1)
                self.assertIs(session.request.call_args.kwargs["allow_redirects"], False)

    def test_mis_report_must_be_report_not_http_200_login_or_error_table(self):
        context = EarningsReportContext(
            "performance",
            1,
            FormSnapshot(
                "",
                (("IBIF_ex", "PMO003R1"), ("BEGYM", "202609")),
                {"BEGYM": (("202609", "September"),)},
                "",
            ),
        )
        for body, error in (
            ('<form><input type="password"></form><table></table>', AuthenticationError),
            ("<table><tr><td>error</td></tr></table>", ParseError),
        ):
            runtime, session, _ = runtime_with(
                response(body, url="https://mis01p.vghks.gov.tw/ibi_apps/WFServlet")
            )
            with self.assertRaises(error):
                EarningsAdapter(runtime).get_report(context)
            self.assertEqual(session.request.call_count, 1)

    def test_mis_rejected_password_uses_typed_error_and_is_submitted_once(self):
        page = '<form action="/VGHK/PAswd2db.asp"><input type="password" name="txtPAPSWD"></form>'
        runtime, session, auth = runtime_with(
            response(page, url=SDKSettings().mis_base_url + "/VGHK/PAswd2db.asp")
        )
        auth.ensure.return_value = SimpleNamespace(
            landing_html=page,
            landing_url=runtime.settings.mis_base_url + "/VGHK/Pswdchk.asp",
        )
        with self.assertRaises(LoginRejectedError) as caught:
            EarningsAdapter(runtime).open_report(
                "performance", EarningsCredentials("SYNTHETIC", "TEST-SECRET")
            )
        self.assertEqual(caught.exception.info.code, "EARNINGS_PASSWORD_REJECTED")
        self.assertEqual(session.request.call_count, 1)
        auth.login.assert_not_called()

    def test_optional_earnings_analysis_requires_report_not_only_login_and_keeps_retest(self):
        from vghks_sdk.offline.analyze import _earnings_results, _retest_config

        steps = [{"name": "mis.performance.open", "operation": "mis.performance", "status": "OK"}]
        self.assertEqual(_earnings_results(steps)[0]["live_status"], "NOT_TESTED")
        steps.append(
            {"name": "mis.performance.report", "operation": "mis.performance", "status": "OK"}
        )
        self.assertEqual(_earnings_results(steps)[0]["live_status"], "VERIFIED")
        retest = _retest_config(
            {"profile": "atomic", "include_earnings": True},
            [],
            {"phase": "AUTHENTICATION"},
            "COMPLETED_WITH_ERRORS",
        )
        self.assertTrue(retest["include_earnings"])
        self.assertEqual(retest["profile"], "comprehensive")


@unittest.skipUnless(
    os.getenv("VGHKS_RUN_HAR_CONTRACT") == "1" and HAR_ROOT.is_dir(),
    "Requires authorized local recordings",
)
class SeptemberRecordingTests(unittest.TestCase):
    def test_all_available_responses_parse_and_four_write_acks_are_only_replayed(self):
        from vghks_sdk.offline.replay import replay_hars

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("requests.sessions.Session.request") as network,
        ):
            report = replay_hars(HAR_ROOT, output_path=Path(directory) / "replay.json")
        network.assert_not_called()
        self.assertEqual(report["archive_count"], 11)
        self.assertEqual(report["status_counts"].get("PARSE_ERROR", 0), 0)
        self.assertEqual(report["status_counts"]["RECORDED_ACK"], 4)
        self.assertEqual(report["status_counts"]["EXPECTED_NEGATIVE"], 1)
        self.assertEqual(report["status_counts"]["UNAVAILABLE"], 8)
        # Patient headers are now replayed too; one old capture omitted this
        # body, while the other recorded header must resolve successfully.
        self.assertCountEqual(
            [e["status"] for e in report["exchanges"] if e["operation"] == "prq.patient_identity"],
            ["UNAVAILABLE", "PARSED"],
        )
        self.assertEqual(report["status"], "INCOMPLETE_CAPTURE")
        dbr = [
            entry
            for entry in report["exchanges"]
            if entry["operation"] in {"prq.order_report", "prq.text_report"}
        ]
        self.assertEqual(len(dbr), 2)
        self.assertTrue(
            all(
                entry["report_data_status"] == "ATTACHMENT_ONLY"
                and entry["report_text_characters"] == 0
                for entry in dbr
            )
        )
        counts = [
            e["record_count"]
            for e in report["exchanges"]
            if e["operation"] == "prq.text_report_history"
        ]
        self.assertEqual(counts, [0, 27, 15])
        self.assertIn(
            30,
            [
                e["record_count"]
                for e in report["exchanges"]
                if e["operation"] == "prq.upload_history"
            ],
        )

    def test_both_mis_navigation_chains_use_fresh_secondary_credentials_offline(self):
        from vghks_sdk.contracts.har import load_har

        archive = load_har(HAR_ROOT / "查詢醫療業績與薪水.har")
        raw = json.loads((HAR_ROOT / "查詢醫療業績與薪水.har").read_text(encoding="utf8"))["log"][
            "entries"
        ]
        for kind, first, indexes in (
            ("performance", 23, (24, 26, 31, 32, 34, 35, 41)),
            ("payroll", 65, (66, 68, 73, 75, 76, 77, 83)),
        ):
            with self.subTest(kind=kind):
                responses = [
                    response(archive.entries[i].text(), url=raw[i]["request"]["url"])
                    for i in indexes
                ]
                redirect = response("", 302)
                redirect.headers["Location"] = "/VGHK/Pswdchk.asp"
                responses.insert(2, redirect)
                runtime, session, auth = runtime_with(*responses)
                auth.ensure.return_value = SimpleNamespace(
                    landing_html=archive.entries[first].text(),
                    landing_url=raw[first]["request"]["url"],
                )
                adapter = EarningsAdapter(runtime)
                context = adapter.open_report(
                    kind, EarningsCredentials("SYNTHETIC-NATIONAL-ID", "SYNTHETIC-SECONDARY-SECRET")
                )
                report = adapter.get_report(context)
                self.assertGreater(len(report.tables), 0)
                self.assertEqual(session.request.call_count, 8)
                posts = [
                    call
                    for call in session.request.call_args_list
                    if urlsplit(call.args[1]).path == "/VGHK/PAswd2db.asp"
                ]
                fields = parse_qs(posts[0].kwargs["data"], encoding="cp950")
                self.assertEqual(fields["txtPAPSWD"], ["SYNTHETIC-SECONDARY-SECRET"])
                self.assertEqual(fields["txtUsrId"], ["SYNTHETIC-NATIONAL-ID"])
                self.assertEqual(fields["sUSR_ID"], ["SYNTHETIC"])
                auth.generation += 1
                with self.assertRaises(ConfigurationError):
                    adapter.get_report(context)
                self.assertEqual(session.request.call_count, 8)


if __name__ == "__main__":
    unittest.main()
