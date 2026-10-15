"""Operation lookup and response reuse preserve recorded evidence semantics."""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup

from vghks_sdk.core.operations import OperationSpec
from vghks_sdk.offline.analyze import inspect_bundle
from vghks_sdk.offline.bundle import BundleReader
from vghks_sdk.offline.cache import _AnalysisReader
from vghks_sdk.offline.replay import identify_operation, replay_response, request_parts
from vghks_sdk.offline.requests import _signature_index, captured_request_parts
from vghks_sdk.parsing.portal import parse_password_status


class ReplayEfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        guard = patch("socket.socket.connect", side_effect=AssertionError("offline only"))
        guard.start()
        self.addCleanup(guard.stop)

    def request(self, code="getUdhcdsps"):
        return {"method": "GET", "url": "https://synthetic.invalid/PRQWeb/QueryMrData.do"
                f"?hhisnum=0000000&hid=synthetic&reqCode={code}"}

    def bundle(self, count=20):
        manifest = [{"kind": "HTTP_EXCHANGE", "capture_id": f"{index:06d}",
                     "request": self.request(), "response_file": "response.json",
                     "response": {"status_code": 200, "headers": [["Content-Type", "application/json"]]}}
                    for index in range(1, count + 1)]
        path = self.root / "synthetic.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("run_summary.json", json.dumps({"schema_version": 6, "status": "OK"}))
            archive.writestr("capture_manifest.jsonl", "\n".join(json.dumps(row) for row in manifest))
            archive.writestr("response.json", '{"hhisnum":"0000000","allergy":false,"allergyMsg":""}')
        return path

    def test_prebuilt_signatures_keep_constants_pagination_and_stable_specificity(self):
        with patch("vghks_sdk.contracts.har.HarSignature.from_operation", side_effect=AssertionError("rebuilt")):
            self.assertEqual(identify_operation(self.request()), "prq.allergy")
            self.assertEqual(identify_operation(self.request("unknown")), "")
            self.assertEqual(identify_operation({"method": "GET", "url":
                "https://synthetic.invalid/webmaas/RSV/RSV11W001.do?patno=0000000&d-123-p=2"}),
                "webmaas.registration_query")
        broad = OperationSpec("broad", "synthetic", "GET", "/same")
        first = OperationSpec("first", "synthetic", "GET", "/same", query_keys=frozenset({"id"}))
        second = OperationSpec("second", "synthetic", "GET", "/same", query_keys=frozenset({"id"}))
        index = _signature_index((broad, first, second))
        self.assertEqual([spec.key for spec, _ in index["GET", "/same"]], ["first", "second", "broad"])
        with self.assertRaises(TypeError):
            index["GET", "/other"] = ()

    def test_request_metadata_is_parsed_once_per_analysis_and_released(self):
        with BundleReader(self.bundle()) as source:
            with patch("vghks_sdk.offline.cache.request_parts", wraps=request_parts) as parse:
                first = inspect_bundle(source)
                self.assertEqual(parse.call_count, 1)
                second = inspect_bundle(source)
                self.assertEqual(parse.call_count, 2)
            self.assertEqual(first, second)
            self.assertIsNotNone(source.archive.fp)
            request = source.jsonl("capture_manifest.jsonl")[0]["request"]
            first_parts = request_parts(request)
            first_parts[2].clear()
            self.assertIn("hhisnum", request_parts(request)[2])
            with _AnalysisReader(source) as reader:
                captured_request_parts(reader, request)
                self.assertTrue(reader._requests)
            self.assertEqual(reader._requests, {})
            self.assertEqual(reader._request_bytes, 0)

    def test_request_cache_limits_and_directory_bypass_preserve_fresh_results(self):
        with BundleReader(self.bundle()) as source, _AnalysisReader(source, max_bytes=4096) as reader:
            with patch("vghks_sdk.offline.cache._MAX_REQUEST_ENTRIES", 1):
                first, second = self.request(), self.request("unknown")
                captured_request_parts(reader, first)
                captured_request_parts(reader, second)
                self.assertEqual(len(reader._requests), 1)
                self.assertLessEqual(reader._cache_bytes + reader._request_bytes, 4096)
                self.assertEqual(captured_request_parts(reader, first), request_parts(first))
            with patch("vghks_sdk.offline.cache._MAX_REQUEST_BYTES", 1):
                before = len(reader._requests)
                captured_request_parts(reader, self.request())
                self.assertEqual(len(reader._requests), before)
        directory = self.root / "directory"
        directory.mkdir()
        (directory / "run_summary.json").write_text('{"schema_version":6}', "utf-8")
        (directory / "capture_manifest.jsonl").write_text("", "utf-8")
        with BundleReader(directory) as source, _AnalysisReader(source) as reader:
            request = self.request()
            captured_request_parts(reader, request)
            request["url"] = self.request("unknown")["url"]
            self.assertEqual(captured_request_parts(reader, request), request_parts(request))
            self.assertEqual(reader._requests, {})

    def test_invalid_request_bodies_fail_without_entering_metadata_cache(self):
        with BundleReader(self.bundle()) as source, _AnalysisReader(source) as reader:
            request = {**self.request(), "body": {"base64": "invalid-base64"}}
            with self.assertRaises(ValueError):
                captured_request_parts(reader, request)
            self.assertEqual(reader._requests, {})

    def test_plain_json_avoids_html_work_and_unknown_schema_stays_an_error(self):
        with (patch("vghks_sdk.offline.replay.BeautifulSoup", side_effect=AssertionError("JSON DOM")),
              patch("vghks_sdk.parsing.portal.BeautifulSoup", side_effect=AssertionError("JSON policy DOM"))):
            value = replay_response("prq.allergy",
                b'{"hhisnum":"0000000","allergy":false,"allergyMsg":"","extra":"location"}',
                {"hhisnum": "0000000"})
            self.assertEqual(value["status"], "PARSED")
            invalid = replay_response("prq.allergy", b"{}", {"hhisnum": "0000000"})
            self.assertEqual(invalid["error_code"], "PATIENT_JSON_SHAPE_INVALID")

    def test_html_dom_is_reused_and_json_mime_cannot_hide_login_html(self):
        with patch("vghks_sdk.offline.replay.BeautifulSoup", wraps=BeautifulSoup) as parse:
            value = replay_response("prq.soap", b'<div id="data"><div class="soap"><pre>S: synthetic</pre></div></div>',
                                    {"hhisnum": "0000000", "caseDT": "2026-01-02"})
            self.assertEqual(value["status"], "PARSED")
            self.assertEqual(parse.call_count, 1)
        value = replay_response("prq.allergy", b'<div name="muid"></div><div name="mpassword"></div>',
                                {"hhisnum": "0000000"}, mime="application/json")
        self.assertEqual(value["error_code"], "AUTH_SESSION_LOGIN_FORM")

    def test_plain_encoded_script_and_hidden_password_notices_keep_their_meaning(self):
        for text, expected in (
            ("密 碼剩餘3天到期", "EXPIRING"),
            ("password has expired", "CHANGE_REQUIRED"),
            ("&#23494;&#30908;已過期", "CHANGE_REQUIRED"),
            ('<script>alert("密碼已過期");</script>', "CHANGE_REQUIRED"),
            ('<div hidden>密碼已過期</div>', "NO_NOTICE"),
            ('<script>function callback(){alert("密碼已過期");}</script>', "NO_NOTICE"),
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_password_status(text).status, expected)
        with patch("vghks_sdk.parsing.portal.BeautifulSoup", wraps=BeautifulSoup) as parse:
            parse_password_status('<p>密碼剩餘3天到期</p><script>targetUrl="myPortal.do";</script>')
            self.assertEqual(parse.call_count, 1)
