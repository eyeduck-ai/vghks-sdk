from __future__ import annotations

import json
import socket
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit

from test_webmaas import TOKEN, basic_info, landing
from test_webmaas_session import TIMEOUT_HTML

from vghks_sdk.offline.analyze import inspect_bundle
from vghks_sdk.offline.bundle import BundleReader
from vghks_sdk.offline.recovery import verify_webmaas_recoveries
from vghks_sdk.offline.replay import replay_bundle

SECRET = "SENSITIVE_SYNTHETIC_CONTENT"
BASE = "https://synthetic.test"
NAME = "get_patient_basic_info"
RAW_ID = "op-000107"  # Deliberately different from the diagnostic counter.


class ApplicationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.records, self.events = [], []
        self.write("run_config.json", {"schema_version": 7, "profile": "session",
                                     "endpoint_overrides": {"webmaas_base_url": BASE + "/webmaas"}})
        self.steps = [
            {"name": "session.after_cookie_loss.auth_check", "status": "ERROR",
             "first_capture_id": "000001", "last_capture_id": "000001",
             "issue": {"code": "WEBMAAS_SESSION_TIMEOUT", "operation": "webmaas.session_check"}},
            {"name": "session.direct_after_cookie_loss.webmaas.basic_info", "status": "OK",
             "operation": "webmaas.basic_info", "first_capture_id": "000002", "last_capture_id": "000007"},
        ]
        self.write("run_summary.json", {"schema_version": 6, "status": "COMPLETED_WITH_ERRORS",
                                       "test_mrn": "0000000", "steps": self.steps})
        self.write("parsed/session/comparison.json", {"challenge": {"evidence": "LOCAL_COOKIE_LOSS"},
                                                      "natural_ttl": {"verified": False}})
        for phase in ("baseline", "after_cookie_loss", "direct_after_cookie_loss"):
            self.write(f"parsed/session/{phase}/webmaas.basic_info.json",
                       {"synthetic": SECRET, "raw_html": phase})
        self.capture("GET", "/webmaas/QUY/QUY15W001.do", TIMEOUT_HTML,
                     raw_id="op-000009", response_path="/webmaas/comm/pageTimeOut.do")
        self.capture("GET", "/webmaas/QUY/QUY15W001.do", "", status=302)
        self.capture("GET", "/webmaas/comm/pageTimeOut.do", TIMEOUT_HTML, redirect=True)
        self.capture("GET", "/SectOrdWeb/so.do", "ssID=fresh&keyOne=1&keyTwo=2&keyThree=3",
                     query={"CardNO": "SYNTHETIC", "reqCode": "getRSAInfo"})
        self.capture("GET", "/webmaas/WPSAutoLogon", "", status=302, query={
            "externalRoles": "maas_QRY15", "targetURL": BASE + "/webmaas/QUY/QUY15W001.do",
            "keyOne": SECRET, "keyTwo": "2", "keyThree": "3", "ssID": SECRET,
            "singlePage": "true", "uid": "SYNTHETIC",
        })
        self.capture("GET", "/webmaas/QUY/QUY15W001.do", landing(), redirect=True)
        self.capture("POST", "/webmaas/QUY/QUY15W001.do", basic_info("0000000"),
                     form={"QRY": "", TOKEN: SECRET, "pageid": "QUY15W001", "patno": "0000000", "type": "A"})
        self.events = [{"event": "operation_started", "operation_id": 7,
                        "operation": NAME, "application": "webmaas"}]
        for record in self.records[1:]:
            if record.get("redirect_index"):
                continue
            self.events.append({"event": "http_request", "operation_id": 7,
                                "method": record["request"]["method"],
                                "path": urlsplit(record["request"]["url"]).path})
            if record["capture_id"] == "000002":
                self.events.append({"event": "application_session_recovery_started", "operation_id": 7,
                                    "application": "webmaas", "recovery": "APPLICATION_SSO",
                                    "capture_operation_id": RAW_ID,
                                    "issue": {"code": "WEBMAAS_SESSION_TIMEOUT"}})
        self.events.append({"event": "operation_finished", "operation_id": 7,
                            "operation": NAME, "status": "OK", "capture_operation_id": RAW_ID})

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def capture(self, method, path, body, *, status=200, raw_id=RAW_ID,
                response_path=None, query=None, form=None, redirect=False):
        capture_id = f"{len(self.records) + 1:06d}"
        group = self.records[-1]["request_group_id"] if redirect else "req-" + capture_id
        url = BASE + path + ("?" + urlencode(query) if query else "")
        record = {"capture_id": capture_id, "kind": "HTTP_EXCHANGE", "sdk_operation_id": raw_id,
                  "sdk_operation": NAME if raw_id == RAW_ID else "auth_check", "request_group_id": group,
                  "redirect_index": 1 if redirect else 0,
                  "request": {"method": method, "url": url, "body": {"text": urlencode(form or {})}},
                  "response": {"status_code": status, "url": BASE + (response_path or path),
                               "headers": [["Content-Type", "text/html; charset=utf-8"]]},
                  "response_file": f"responses/{capture_id}.html"}
        destination = self.root / record["response_file"]
        destination.parent.mkdir(exist_ok=True)
        destination.write_text(body, encoding="utf-8")
        self.records.append(record)

    def flush(self):
        for name, values in (("capture_manifest.jsonl", self.records),
                             ("diagnostics/diagnostics.jsonl", self.events)):
            path = self.root / name
            path.parent.mkdir(exist_ok=True)
            path.write_text("\n".join(json.dumps(v) for v in values), encoding="utf-8")

    def verify(self, rows=None):
        self.flush()
        with BundleReader(self.root) as reader:
            rows = replay_bundle(reader) if rows is None else rows
            evidence = verify_webmaas_recoveries(reader, rows)
        return evidence, rows

    def test_analysis_retains_original_failure_and_verifies_direct_recovery_without_socket(self):
        self.flush()
        with (patch.object(socket.socket, "connect", side_effect=AssertionError("offline network")),
              BundleReader(self.root) as reader):
            report, retest = inspect_bundle(reader)
        self.assertEqual([p["capture_id"] for p in report["problems"]], ["000001"])
        self.assertEqual([p["capture_id"] for p in report["recovered_requests"]], ["000003"])
        self.assertEqual(report["bundle_status"], "COMPLETED_WITH_ERRORS")
        self.assertEqual(report["analysis_status"], "NEEDS_ATTENTION")
        session = report["session_test"]
        self.assertEqual(session["direct_api_recovery"]["status"], "VERIFIED")
        self.assertTrue(session["direct_patient_values_equal"])
        self.assertFalse(session["natural_ttl_verified"])
        self.assertEqual(retest["profile"], "session")
        self.assertEqual(retest["login_negative_attempts"], 0)
        self.assertNotIn(SECRET, json.dumps(report))
        self.assertEqual(report["application_session_recoveries"][0]["capture_operation_id"], RAW_ID)

    def test_legacy_correlates_requests_without_assuming_counters_are_equal(self):
        for event in self.events:
            event.pop("capture_operation_id", None)
        evidence, rows = self.verify()
        self.assertEqual(evidence[0]["correlation"], "LEGACY_REQUEST_SEQUENCE")
        self.assertTrue(rows[2]["recovered"])
        self.assertFalse(rows[0].get("recovered", False))

    def test_ambiguous_legacy_operations_do_not_claim_recovery(self):
        for event in self.events:
            event.pop("capture_operation_id", None)
        copies = deepcopy(self.records[1:])
        for record in copies:
            record["sdk_operation_id"] = "op-000108"
            record["capture_id"] = f"{int(record['capture_id']) + 10:06d}"
        self.records.extend(copies)
        evidence, rows = self.verify()
        self.assertEqual(evidence[0]["status"], "UNVERIFIED")
        self.assertFalse(any(r.get("recovered") for r in rows))

    def test_incomplete_failed_or_mismatched_diagnostics_do_not_hide_timeout(self):
        original = deepcopy(self.events)
        for mode in ("missing_finish", "failed", "wrong_link", "wrong_path", "duplicate_recovery", "portal_relogin"):
            with self.subTest(mode=mode):
                self.events = deepcopy(original)
                if mode == "missing_finish":
                    self.events.pop()
                elif mode == "failed":
                    self.events[-1]["status"] = "ERROR"
                elif mode == "wrong_link":
                    self.events[2]["capture_operation_id"] = "op-000777"
                elif mode == "wrong_path":
                    self.events[1]["path"] = "/webmaas/RSV/RSV11W001.do"
                elif mode == "duplicate_recovery":
                    self.events.insert(3, deepcopy(self.events[2]))
                else:
                    self.events.insert(-1, {"event": "reauthentication_started", "operation_id": 7})
                evidence, rows = self.verify()
                self.assertEqual(evidence[0]["status"], "FAILED" if mode == "failed" else "UNVERIFIED")
                self.assertFalse(rows[2].get("recovered", False))

    def test_other_operation_or_later_parse_error_does_not_prove_success(self):
        original = deepcopy(self.records)
        for mode in ("other_operation", "missing_token", "bad_result", "wrong_role", "foreign_sso", "foreign_target"):
            with self.subTest(mode=mode):
                self.records = deepcopy(original)
                if mode == "other_operation":
                    self.records[-1]["sdk_operation_id"] = "op-000108"
                elif mode == "wrong_role":
                    self.records[4]["request"]["url"] = self.records[4]["request"]["url"].replace("maas_QRY15", "maas_RSV11")
                elif mode == "foreign_sso":
                    self.records[4]["request"]["url"] = self.records[4]["request"]["url"].replace(BASE, "https://foreign.test")
                elif mode == "foreign_target":
                    self.records[4]["request"]["url"] = self.records[4]["request"]["url"].replace(
                        urlencode({"targetURL": BASE}), urlencode({"targetURL": "https://foreign.test"}))
                    self.records[5]["response"]["url"] = "https://foreign.test/webmaas/QUY/QUY15W001.do"
                else:
                    record = self.records[5] if mode == "missing_token" else self.records[-1]
                    (self.root / record["response_file"]).write_text("<p>unknown schema</p>", encoding="utf-8")
                evidence, rows = self.verify()
                self.assertEqual(evidence[0]["status"], "UNVERIFIED")
                self.assertFalse(rows[2].get("recovered", False))
                (self.root / original[5]["response_file"]).write_text(landing(), encoding="utf-8")
                (self.root / original[-1]["response_file"]).write_text(basic_info("0000000"), encoding="utf-8")

    def test_direct_step_range_and_parsed_values_are_checked_separately(self):
        self.steps[-1]["last_capture_id"] = "000006"
        self.write("run_summary.json", {"schema_version": 6, "status": "COMPLETED_WITH_ERRORS",
                                       "test_mrn": "0000000", "steps": self.steps})
        self.write("parsed/session/direct_after_cookie_loss/webmaas.basic_info.json", {"synthetic": "changed"})
        self.flush()
        with BundleReader(self.root) as reader:
            report, _ = inspect_bundle(reader)
        self.assertEqual(report["application_session_recoveries"][0]["status"], "RECOVERED")
        self.assertEqual(report["session_test"]["direct_api_recovery"]["status"], "UNVERIFIED")
        self.assertFalse(report["session_test"]["direct_patient_values_equal"])

    def test_read_success_without_recovery_diagnostic_is_unverified(self):
        self.events = []
        self.flush()
        with BundleReader(self.root) as reader:
            report, _ = inspect_bundle(reader)
        self.assertEqual(report["session_test"]["direct_read_status"], "OK")
        self.assertEqual(report["session_test"]["direct_api_recovery"]["status"], "UNVERIFIED")
        self.assertEqual([p["capture_id"] for p in report["problems"]], ["000001", "000003"])

    def test_session_readiness_uses_latest_executed_check_and_keeps_original_failure(self):
        good = {"targets": [{"target": key, "status": "OK"}
                            for key in ("portal", "sectord", "webmaas")]}
        failed = deepcopy(good)
        failed["targets"][-1].update(status="ERROR", issue={"code": "WEBMAAS_SESSION_TIMEOUT"})
        self.write("parsed/session/baseline/auth_check.json", good)
        self.write("parsed/session/after_cookie_loss/auth_check.json", failed)
        self.write("parsed/session/after_cookie_loss/sso_recheck.json", good)
        self.flush()
        with BundleReader(self.root) as reader:
            before, _ = inspect_bundle(reader)
        targets = {t["target"]: t["status"] for t in before["authentication"]}
        self.assertEqual(targets["portal"], "OK")
        self.assertEqual(targets["webmaas"], "ERROR")
        self.steps.insert(1, {"name": "session.after_cookie_loss.sso_recheck", "status": "OK"})
        self.write("run_summary.json", {"schema_version": 6, "status": "COMPLETED_WITH_ERRORS",
                                       "test_mrn": "0000000", "steps": self.steps})
        with BundleReader(self.root) as reader:
            after, _ = inspect_bundle(reader)
        self.assertEqual(next(t for t in after["authentication"] if t["target"] == "webmaas")["status"], "OK")
        self.assertEqual(before["problems"], after["problems"])
        # A subsequent failed check with no returned report cannot reuse good.
        self.steps.append({"name": "session.after_idle.auth_check", "status": "ERROR"})
        self.write("run_summary.json", {"schema_version": 6, "status": "COMPLETED_WITH_ERRORS",
                                       "test_mrn": "0000000", "steps": self.steps})
        with BundleReader(self.root) as reader:
            missing, _ = inspect_bundle(reader)
        self.assertTrue(all(t["status"] == "NOT_TESTED" for t in missing["authentication"]))


if __name__ == "__main__":
    unittest.main()
