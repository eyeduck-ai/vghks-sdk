"""Compatibility and state boundaries across the extracted modules."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vghks_sdk.adapters.prq import PrqAdapter
from vghks_sdk.core.errors import AuthExpiredError
from vghks_sdk.models import OrderHistoryFilter
from vghks_sdk.runtime import SDKRuntime


class ModuleCompatibilityTests(unittest.TestCase):
    def test_existing_live_and_analysis_imports_are_the_same_objects(self):
        aliases = {
            "live.profile": {
                "live.steps": ("LIVE_TEST_MRN", "LIVE_TEST_SCHEMA_VERSION", "LiveTestStep",
                               "LiveTestResult", "_run_step", "_overall_status", "_write_json"),
                "live.patient": ("_FetchedCase", "_fetch_case", "_run_patient_histories"),
            },
            "offline.analyze": {
                "offline.presentation": ("_markdown", "compare_reports"),
                "offline.evidence": ("_step_query", "_no_sample_steps", "_phase",
                                     "_structured_root_issue", "_report_content_summary"),
            },
        }
        for old_name, groups in aliases.items():
            old = importlib.import_module("vghks_sdk." + old_name)
            for new_name, names in groups.items():
                new = importlib.import_module("vghks_sdk." + new_name)
                for name in names:
                    with self.subTest(module=old_name, name=name):
                        self.assertIs(getattr(old, name), getattr(new, name))

    def test_common_live_steps_do_not_load_patient_or_profile_flows(self):
        code = """
import importlib
import socket
import sys

def forbidden(*args, **kwargs):
    raise AssertionError('import attempted a connection')

socket.socket.connect = forbidden
live = importlib.import_module('vghks_sdk.live')
assert live.LiveTestResult is importlib.import_module('vghks_sdk.live.steps').LiveTestResult
importlib.import_module('vghks_sdk.live.atomic')
for name in ('profile', 'patient', 'runner', 'login'):
    assert 'vghks_sdk.live.' + name not in sys.modules, name
"""
        package = importlib.import_module("vghks_sdk")
        package_root = Path(package.__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-c", code], cwd=directory,
                env={**os.environ, "PYTHONPATH": str(package_root)},
                capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr)


class AuthenticationContextTests(unittest.TestCase):
    def make_auth(self):
        from test_auth import ScriptedTransport

        from vghks_sdk import PortalCredentials, SDKSettings
        from vghks_sdk.adapters.auth import AuthenticationAdapter

        transport = ScriptedTransport()
        return AuthenticationAdapter(settings=SDKSettings(),
            credentials=PortalCredentials("U001", "synthetic-password"), transport=transport), transport

    def test_portal_profile_and_webmaas_share_only_their_own_facade_state(self):
        from urllib.parse import urlsplit

        auth, transport = self.make_auth()
        other, other_transport = self.make_auth()
        with patch("socket.socket.connect", side_effect=AssertionError("no sockets")):
            auth.ensure("prq")
            auth.ensure_webmaas_page("RSV11W001")
            auth.ensure("attendance")
            self.assertTrue(auth.take_webmaas_landing("RSV11W001"))
            self.assertEqual(auth.take_webmaas_landing("RSV11W001"), "")
            other_prq = other.ensure("prq")
            auth.login(force=True)
        self.assertEqual(auth.generation, 2)
        self.assertEqual(auth._apps, {})
        self.assertEqual(auth._webmaas_page, "")
        self.assertEqual(other.generation, 1)
        self.assertIs(other._apps["prq"], other_prq)
        self.assertEqual(sum(urlsplit(url).path == "/login.do" for _, url, _ in transport.calls), 2)
        self.assertEqual(sum(urlsplit(url).path == "/login.do" for _, url, _ in other_transport.calls), 1)

    def test_webmaas_timeout_recovery_keeps_portal_and_other_sso_sessions(self):
        from urllib.parse import urlsplit

        from vghks_sdk.core.errors import ApplicationSessionExpiredError

        auth, transport = self.make_auth()
        with patch("socket.socket.connect", side_effect=AssertionError("no sockets")):
            prq = auth.ensure("prq")
            auth.ensure_webmaas_page("RSV11W001")
            sectord = auth._apps["sectord"]
            with self.assertRaises(ApplicationSessionExpiredError):
                auth.assert_not_expired('<p>Page time out, Please 重新登入</p>',
                    auth.settings.webmaas_base_url + "/comm/pageTimeOut.do")
            recovered = auth.recover_webmaas_session()
        self.assertTrue(auth.portal_authenticated)
        self.assertEqual(auth.generation, 1)
        self.assertIs(auth._apps["prq"], prq)
        self.assertIs(auth._apps["sectord"], sectord)
        self.assertIs(auth._apps["webmaas"], recovered)
        self.assertEqual(auth._webmaas_page, "RSV11W001")
        self.assertEqual(sum(urlsplit(url).path == "/login.do" for _, url, _ in transport.calls), 1)
        self.assertEqual(sum("externalRoles" in call.get("params", {}) for _, _, call in transport.calls), 2)


class PrqSharedContextTests(unittest.TestCase):
    def setUp(self):
        self.runtime = object.__new__(SDKRuntime)
        self.runtime.auth = Mock()
        self.runtime.auth.generation = 1
        self.runtime.auth.hid_for.return_value = "CURRENT-HID"
        self.runtime.settings = SimpleNamespace(prq_base_url="https://internal.test/PRQWeb")
        self.runtime.operation_lock = threading.RLock()
        self.runtime._operation_write_attempts = []
        self.runtime.diagnostics = None
        self.runtime.raw_capture = None
        self.adapter = PrqAdapter(self.runtime)

    def test_visit_and_history_operations_cannot_interleave_patient_context(self):
        first_context = threading.Event()
        second_lock_attempt = threading.Event()
        release_context = threading.Event()
        lock = self.runtime.operation_lock

        class ObservedLock:
            def __enter__(self):
                if first_context.is_set():
                    second_lock_attempt.set()
                lock.acquire()

            def __exit__(self, *_):
                lock.release()

        self.runtime.operation_lock = ObservedLock()
        calls = []

        def request(spec, _url, **kwargs):
            self.assertTrue(lock._is_owned())
            calls.append(spec.key)
            fields = kwargs.get("data", {}) | kwargs.get("params", {})
            self.assertEqual(fields.get("hid", "CURRENT-HID"), "CURRENT-HID")
            if spec.key == "prq.patient_context":
                self.assertEqual(fields["queryPtID"], "00000001")
                first_context.set()
                self.assertTrue(release_context.wait(5))
                return ('<frameset id="hFrameset">'
                        '<frame name="lPatientData" src="/PRQWeb/Page/JSP/KS_Patient.jsp">'
                        '<frame name="lList" src="/PRQWeb/QueryCaseList.do"></frameset>')
            if spec.key == "prq.visit_cases":
                return '<input id="typeO"><script>var aryCase=[];</script>'
            if spec.key == "prq.patient_history_context":
                self.assertEqual(fields["id"], "00000002")
            if spec.key == "prq.order_history":
                self.assertEqual(fields["hhisnum"], "00000002")
                return '<script>var aryCase=[];</script>'
            return "<html>synthetic</html>"

        with (
            patch("socket.socket.connect", side_effect=AssertionError("no network")),
            patch.object(self.runtime, "request_text", side_effect=request),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            first = pool.submit(self.adapter.get_visit_cases, "00000001")
            try:
                self.assertTrue(first_context.wait(5))
                second = pool.submit(self.adapter.get_order_history, "00000002", OrderHistoryFilter())
                self.assertTrue(second_lock_attempt.wait(5))
                self.assertEqual(calls, ["prq.patient_context"])
            finally:
                release_context.set()
            self.assertEqual(first.result(timeout=5), [])
            self.assertEqual(second.result(timeout=5), [])
        self.assertEqual(calls, [
            "prq.patient_context", "prq.visit_cases", "prq.patient_history_context",
            "prq.order_history_page", "prq.order_history_select", "prq.order_history",
        ])

    def test_history_failure_after_context_review_does_not_replay_or_relogin(self):
        # Use the recorded synthetic form shape from the existing review tests.
        from test_visit_search import CONTEXT, access_review_form

        calls = []

        def request(spec, _url, **_kwargs):
            self.assertTrue(self.runtime.operation_lock._is_owned())
            calls.append(spec.key)
            if spec.mutates:
                for index in range(len(self.runtime._operation_write_attempts)):
                    self.runtime._operation_write_attempts[index] = True
            if spec.key == "prq.patient_history_context":
                return access_review_form(hid="CURRENT-HID")
            if spec.key == "prq.access_review":
                return CONTEXT
            if spec.key == "prq.order_history":
                raise AuthExpiredError("synthetic failure after review")
            return "<html>synthetic</html>"

        with (
            patch("socket.socket.connect", side_effect=AssertionError("no network")),
            patch.object(self.runtime, "request_text", side_effect=request),
            self.assertRaises(AuthExpiredError),
        ):
            self.adapter.get_order_history("00000000", OrderHistoryFilter())
        self.assertEqual(calls, [
            "prq.patient_history_context", "prq.access_review", "prq.order_history_page",
            "prq.order_history_select", "prq.order_history",
        ])
        self.runtime.auth.login.assert_not_called()
        self.assertEqual(self.runtime._operation_write_attempts, [])


if __name__ == "__main__":
    unittest.main()
