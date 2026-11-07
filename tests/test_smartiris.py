"""Explicit SmartIris handoff, using synthetic contexts and no hospital traffic."""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

from vghks_sdk.core.errors import ConfigurationError
from vghks_sdk.integrations import smartiris
from vghks_sdk.integrations.smartiris import SmartIrisClient
from vghks_sdk.models import PacsLaunchContext, PacsLaunchRef, to_jsonable
from vghks_sdk.models.pacs_launch import SMARTWONDER_BASE_URL, SMARTWONDER_WADO_URL

ROOT = Path(__file__).resolve().parents[1]


def launch_context() -> PacsLaunchContext:
    return PacsLaunchContext(
        reference=PacsLaunchRef("1234567", "SYNTHETIC_ORDER"),
        login_id="SYNTHETIC_LOGIN", client_ip="192.0.2.50", study_uid="1.2.826.0.100.7",
        dicom_server="SK", level="STUDY", hospital_id="",
        wonder_url=SMARTWONDER_BASE_URL,
        wado_url=SMARTWONDER_WADO_URL + "?" + urlencode({
            "requestType": "WADO", "contentType": "application/dicom;text/plain",
            "DicomServer": "SK", "PID": "vwrIris", "Dcm00110010": "SYNTHETIC_LOGIN",
            "Dcm00110011": "192.0.2.50",
        }) + "&",
    )


class LocalSession(requests.Session):
    def __init__(self, replies=None, observer=None):
        super().__init__()
        self.calls = []
        self.replies = list(replies or [])
        self.observer = observer
        self.closed = False

    def request(self, method, url, **kwargs):
        prepared = self.prepare_request(requests.Request(
            method, url, params=kwargs.get("params"), headers=kwargs.get("headers"),
        ))
        self.calls.append((method, url, kwargs, prepared))
        if self.observer:
            self.observer(urlsplit(url).path)
        reply = self.replies.pop(0) if self.replies else (200, b"OK", {})
        if isinstance(reply, BaseException):
            raise reply
        status, content, headers = reply
        response = requests.Response()
        response.status_code = status
        response._content, response._content_consumed = content, True
        response.headers.update(headers)
        response.url, response.request = prepared.url, prepared
        return response

    def close(self):
        self.closed = True
        super().close()


class SmartIrisTests(unittest.TestCase):
    def setUp(self):
        self.mutex = patch.object(smartiris, "_windows_mutex", lambda port, timeout: nullcontext())
        self.mutex.start()
        self.addCleanup(self.mutex.stop)
        self.network = patch("socket.socket.connect", side_effect=AssertionError("network disabled"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def client(self, session=None, **kwargs):
        session = session if session is not None else LocalSession()
        client = SmartIrisClient(session=session, **kwargs)
        self.addCleanup(client.close)
        return client, session

    def test_four_steps_exact_encoding_isolated_transport_and_private_results(self):
        captured = []
        client, session = self.client(capture=captured.append)
        context = launch_context()
        with patch.object(smartiris.time, "time", return_value=100.0):
            result = client.open_study(context)
        self.assertEqual(result.status, "HANDOFF_SENT")
        self.assertIsNone(result.viewer_loaded)
        self.assertFalse(result.retry_safe)
        self.assertEqual([s.name for s in result.steps],
                         ["login", "wonderurl", "setanonymousmode", "cmove"])
        self.assertEqual([s.status for s in result.steps], ["HTTP_RESPONSE_RECEIVED"] * 4)
        self.assertEqual(len(captured), 4)
        self.assertFalse(session.trust_env)
        self.assertEqual(session.get_adapter("http://127.0.0.1").max_retries.total, 0)
        self.assertEqual(session.get_adapter("https://example.test").max_retries.total, 0)
        self.assertFalse(session.cookies)
        timestamps = []
        for call, exchange in zip(session.calls, captured):
            method, url, kwargs, prepared = call
            self.assertEqual(method, "GET")
            self.assertEqual(urlsplit(url).netloc, "127.0.0.1:15516")
            self.assertFalse(kwargs["allow_redirects"])
            self.assertEqual(kwargs["timeout"], (3.0, 10.0))
            self.assertEqual(kwargs["proxies"], {})
            self.assertNotIn("Cookie", prepared.headers)
            self.assertNotIn("Authorization", prepared.headers)
            self.assertEqual(exchange.request_url, prepared.url)
            self.assertEqual(exchange.body, b"OK")
            self.assertTrue(exchange.complete)
            timestamps.append(kwargs["params"]["timestamp"])
        self.assertEqual(len(set(timestamps)), 4)
        login = parse_qs(urlsplit(session.calls[0][3].url).query, keep_blank_values=True)
        self.assertEqual(login["pwd"], [""])
        self.assertEqual(login["id"], [context.login_id])
        wonder = parse_qs(urlsplit(session.calls[1][3].url).query)
        self.assertEqual(wonder["url"], [context.wonder_url + "/"])
        cmove = parse_qs(urlsplit(session.calls[3][3].url).query, keep_blank_values=True)
        self.assertEqual(cmove["cmoveurl"], [context.cmove_url])
        self.assertEqual(cmove["url"], [context.wado_url])
        self.assertEqual(cmove["uid"], [context.study_uid])
        self.assertEqual(cmove["command"], ["1"])
        self.assertEqual(set(cmove),
                         {"cmoveurl", "url", "dicomserver", "level", "uid", "command", "timestamp"})
        summaries = json.dumps(to_jsonable(result)) + repr(result) + repr(captured)
        for secret in (context.login_id, context.reference.mrn, context.reference.request_no,
                       context.study_uid, context.client_ip, context.wado_url, context.cmove_url):
            self.assertNotIn(secret, summaries)

    def test_unknown_two_byte_body_is_never_parsed_as_viewer_acknowledgement(self):
        client, _ = self.client(LocalSession([(200, b"??", {})] * 4))
        result = client.open_study(launch_context())
        self.assertEqual(result.status, "HANDOFF_SENT")
        self.assertIsNone(result.viewer_loaded)
        self.assertTrue(all(step.response_size == 2 for step in result.steps))

    def test_connection_refusal_and_connect_timeout_are_not_sent_or_retried(self):
        for exception, code in (
            (requests.ConnectionError(ConnectionRefusedError(errno.ECONNREFUSED, "synthetic")),
             "SMARTIRIS_CONNECTION_REFUSED"),
            (requests.ConnectTimeout("SENSITIVE_URL"), "SMARTIRIS_CONNECT_TIMEOUT"),
        ):
            with self.subTest(code=code):
                captured = []
                client, session = self.client(LocalSession([exception]), capture=captured.append)
                result = client.open_study(launch_context())
                self.assertEqual((result.status, result.error_code), ("FAILED", code))
                self.assertEqual(len(session.calls), 1)
                self.assertEqual(len(captured), 1)
                self.assertFalse(captured[0].complete)
                self.assertNotIn("SENSITIVE_URL", repr(result))

    def test_timeout_or_connection_drop_after_send_is_unknown_and_stops(self):
        for exception in (requests.ReadTimeout("SENSITIVE_URL"),
                          requests.ConnectionError("SENSITIVE_URL")):
            with self.subTest(kind=type(exception).__name__):
                client, session = self.client(LocalSession([(200, b"OK", {}), exception]))
                result = client.open_study(launch_context())
                self.assertEqual(result.status, "OUTCOME_UNKNOWN")
                self.assertEqual(len(session.calls), 2)
                self.assertFalse(result.retry_safe)
                self.assertNotIn("SENSITIVE_URL", repr(result))

    def test_redirects_and_http_failures_never_follow_or_continue(self):
        for status in (301, 302, 307, 308, 401, 403, 500):
            with self.subTest(status=status):
                session = LocalSession([(status, b"SENSITIVE_BODY", {
                    "Location": "http://example.test/do-not-send", "Set-Cookie": "secret=x",
                })])
                client, _ = self.client(session)
                result = client.open_study(launch_context())
                self.assertEqual(result.status, "OUTCOME_UNKNOWN")
                self.assertEqual(result.steps[0].http_status, status)
                self.assertEqual(len(session.calls), 1)
                self.assertNotIn("SENSITIVE_BODY", repr(result))

    def test_failure_at_each_step_stops_exactly_at_that_step(self):
        for index in range(4):
            with self.subTest(index=index):
                session = LocalSession([(200, b"OK", {})] * index + [(500, b"bad", {})])
                client, _ = self.client(session)
                result = client.open_study(launch_context())
                self.assertEqual(len(session.calls), index + 1)
                self.assertEqual(len(result.steps), index + 1)
                self.assertEqual(result.error_code, "SMARTIRIS_HTTP_ERROR")

    def test_capture_is_explicit_local_evidence_and_callback_failure_stops(self):
        def failing_capture(exchange):
            self.assertIn("id=SYNTHETIC_LOGIN", exchange.request_url)
            self.assertEqual(exchange.body, b"OK")
            raise ValueError("SENSITIVE_CALLBACK_DETAILS")

        client, session = self.client(capture=failing_capture)
        result = client.open_study(launch_context())
        self.assertEqual(result.error_code, "SMARTIRIS_CAPTURE_FAILED")
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(result.status, "OUTCOME_UNKNOWN")
        self.assertNotIn("SENSITIVE_CALLBACK_DETAILS", repr(result))

    def test_interrupt_records_partial_result_releases_locks_and_propagates(self):
        client, session = self.client(LocalSession([(200, b"OK", {}), KeyboardInterrupt()]))
        with self.assertRaises(KeyboardInterrupt):
            client.open_study(launch_context())
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(client.last_result.status, "OUTCOME_UNKNOWN")
        self.assertEqual(client.last_result.error_code, "SMARTIRIS_INTERRUPTED")
        # This is a new, explicit caller action, not an automatic retry.
        self.assertEqual(client.open_study(launch_context()).status, "HANDOFF_SENT")
        self.assertEqual(len(session.calls), 6)

    def test_response_size_and_time_budget_stop_streaming(self):
        for headers, body in (({"Content-Length": "99999999"}, b""),
                              ({}, b"x" * (smartiris._MAX_RESPONSE_BYTES + 1))):
            with self.subTest(headers=headers):
                client, session = self.client(LocalSession([(200, body, headers)]))
                result = client.open_study(launch_context())
                self.assertEqual(result.error_code, "SMARTIRIS_RESPONSE_TOO_LARGE")
                self.assertEqual(len(session.calls), 1)
        client, session = self.client()
        with patch.object(smartiris, "_OPERATION_SECONDS", -1):
            result = client.open_study(launch_context())
        self.assertEqual(result.error_code, "SMARTIRIS_TIME_BUDGET_EXCEEDED")
        self.assertEqual(session.calls, [])

    def test_interrupt_between_steps_retains_progress(self):
        client, session = self.client()
        completed = smartiris.SmartIrisStepResult("login", "HTTP_RESPONSE_RECEIVED", 200)
        with patch.object(client, "_send", side_effect=[(completed, False), KeyboardInterrupt()]), \
                self.assertRaises(KeyboardInterrupt):
            client.open_study(launch_context())
        self.assertEqual(session.calls, [])
        self.assertEqual(len(client.last_result.steps), 1)
        self.assertEqual(client.last_result.error_code, "SMARTIRIS_INTERRUPTED")

    def test_only_explicit_loopback_origins_and_valid_timeouts(self):
        for url in (
            "http://example.test:15516", "https://127.0.0.1:15516", "http://127.1:15516",
            "http://127.0.0.1", "http://127.0.0.1:15516/login", "http://127.0.0.1:15516?x=y",
            "http://127.0.0.1:15516#x", "http://user@127.0.0.1:15516",
            "http://127.0.0.1:0", "http://localhost.evil:15516", "http://[::1]:15516",
            "http://localhost:15516\n", None,
        ):
            with self.subTest(url=url), self.assertRaises(ConfigurationError):
                SmartIrisClient(bridge_url=url)
        for timeout in ((0, 10), (3, float("inf")), (3, float("nan")), (3, 31), (True, 5), 5):
            with self.subTest(timeout=timeout), self.assertRaises(ConfigurationError):
                SmartIrisClient(timeout=timeout)
        client, session = self.client(bridge_url="http://localhost:54321/")
        client.open_study(launch_context())
        self.assertTrue(all(call[1].startswith("http://127.0.0.1:54321/")
                            for call in session.calls))

    def test_reject_shared_portal_auth_cookies_and_modified_context(self):
        for mode in ("cookie", "auth", "header"):
            session = LocalSession()
            if mode == "cookie":
                session.cookies.set("PortalCookie", "SENSITIVE")
            elif mode == "auth":
                session.auth = ("SENSITIVE", "SENSITIVE")
            else:
                session.headers["Authorization"] = "SENSITIVE"
            with self.subTest(mode=mode), self.assertRaises(ConfigurationError):
                SmartIrisClient(session=session)
        client, session = self.client()
        with self.assertRaises(ConfigurationError):
            client.open_study(object())
        context = launch_context()
        object.__setattr__(context, "wado_url", "http://example.test/leak")
        with self.assertRaises(ConfigurationError):
            client.open_study(context)
        self.assertEqual(session.calls, [])

    def test_context_manager_close_prevents_later_commands(self):
        session = LocalSession()
        with SmartIrisClient(session=session) as client:
            self.assertEqual(client.open_study(launch_context()).status, "HANDOFF_SENT")
        self.assertTrue(session.closed)
        with self.assertRaises(ConfigurationError):
            client.open_study(launch_context())
        client.close()
        self.assertEqual(len(session.calls), 4)

    def test_specific_adapter_mount_cannot_bypass_zero_retry_transport(self):
        session = requests.Session()
        custom = requests.adapters.HTTPAdapter(max_retries=3)
        custom.send = Mock(side_effect=AssertionError("custom transport must not run"))
        # Requests chooses the longest matching prefix, even after mounting
        # a new generic http:// adapter. One adapter can own multiple mounts.
        session.mount("http://127.0.0.1:15516/", custom)
        session.mount("http://127.0.0.1:15516/login", custom)
        displaced = {id(adapter): adapter for adapter in session.adapters.values()}.values()
        for adapter in displaced:
            adapter.close = Mock(wraps=adapter.close)
        client, _ = self.client(session)
        for adapter in displaced:
            adapter.close.assert_not_called()  # Constructor cannot close live resources.

        def fail_once(adapter, prepared, **kwargs):
            self.assertEqual(adapter.max_retries.total, 0)
            self.assertEqual(prepared.path_url.split("?", 1)[0], "/login")
            response = requests.Response()
            response.status_code = 500
            response._content, response._content_consumed = b"synthetic failure", True
            response.url, response.request = prepared.url, prepared
            return response

        with patch.object(requests.adapters.HTTPAdapter, "send", autospec=True,
                          side_effect=fail_once) as sender:
            result = client.open_study(launch_context())
        self.assertEqual(result.error_code, "SMARTIRIS_HTTP_ERROR")
        self.assertEqual(sender.call_count, 1)
        custom.send.assert_not_called()
        client.close()
        client.close()
        for adapter in displaced:
            adapter.close.assert_called_once_with()

    def test_displaced_adapter_close_failure_still_releases_other_adapters(self):
        session = LocalSession()
        displaced = list(session.adapters.values())
        displaced[0].close = Mock(side_effect=RuntimeError("synthetic adapter close failure"))
        displaced[1].close = Mock()
        client, _ = self.client(session)
        with self.assertRaises(RuntimeError):
            client.close()
        self.assertTrue(session.closed)
        for adapter in displaced:
            adapter.close.assert_called_once_with()
        client.close()
        with self.assertRaises(ConfigurationError):
            client.open_study(launch_context())

    def test_two_clients_share_one_serial_handoff_lock(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def first_observer(path):
            calls.append(("first", path))
            if path == "/login":
                entered.set()
                if not release.wait(3):
                    raise AssertionError("test release timed out")

        first, _ = self.client(LocalSession(observer=first_observer))
        second, _ = self.client(LocalSession(observer=lambda path: calls.append(("second", path))))
        results = []
        t1 = threading.Thread(target=lambda: results.append(first.open_study(launch_context())))
        t2 = threading.Thread(target=lambda: results.append(second.open_study(launch_context())))
        t1.start()
        self.assertTrue(entered.wait(2))
        t2.start()
        release.set()
        t1.join(4)
        t2.join(4)
        self.assertFalse(t1.is_alive() or t2.is_alive())
        self.assertEqual([name for name, path in calls], ["first"] * 4 + ["second"] * 4)
        self.assertTrue(all(result.status == "HANDOFF_SENT" for result in results))

    def test_lock_timeout_prevents_any_request(self):
        client, session = self.client(lock_timeout=0)
        client._bridge_lock.acquire()
        try:
            result = client.open_study(launch_context())
        finally:
            client._bridge_lock.release()
        self.assertEqual(result.error_code, "SMARTIRIS_LOCK_TIMEOUT")
        self.assertEqual(session.calls, [])

    def test_same_client_lock_wait_is_also_bounded(self):
        client, session = self.client(lock_timeout=0)
        entered, release = threading.Event(), threading.Event()

        def hold_lock():
            with client._client_lock:
                entered.set()
                release.wait(3)

        worker = threading.Thread(target=hold_lock)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            result = client.open_study(launch_context())
            self.assertEqual(result.error_code, "SMARTIRIS_LOCK_TIMEOUT")
            self.assertEqual(session.calls, [])
        finally:
            release.set()
            worker.join(3)

    def test_unsupported_platform_never_falls_back_to_uncoordinated_requests(self):
        self.mutex.stop()
        client, session = self.client()
        with patch.object(smartiris.os, "name", "posix"):
            result = client.open_study(launch_context())
        self.assertEqual(result.error_code, "SMARTIRIS_WINDOWS_REQUIRED")
        self.assertEqual(session.calls, [])


class SmartIrisImportAndMutexTests(unittest.TestCase):
    def test_import_constructor_and_close_perform_no_io(self):
        code = r'''
import ctypes
import socket
from unittest.mock import patch
def forbidden(*args, **kwargs):
    raise AssertionError("unexpected I/O")
with patch.object(socket.socket, "connect", forbidden), \
     patch.object(ctypes, "WinDLL", forbidden, create=True):
    from vghks_sdk.integrations.smartiris import SmartIrisClient
    with SmartIrisClient():
        pass
'''
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                                text=True, timeout=15,
                                env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(os.name == "nt", "Windows named mutex")
    def test_named_mutex_blocks_another_process_and_then_releases(self):
        code = r'''
import sys
from vghks_sdk.integrations.smartiris import _windows_mutex
with _windows_mutex(54322, 5):
    print("READY", flush=True)
    sys.stdin.readline()
'''
        child = subprocess.Popen([sys.executable, "-u", "-c", code], cwd=ROOT,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True,
                                 env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
        try:
            self.assertEqual(child.stdout.readline().strip(), "READY")
            session = LocalSession()
            with SmartIrisClient(session=session, bridge_url="http://127.0.0.1:54322",
                                 lock_timeout=0.05) as client:
                result = client.open_study(launch_context())
                self.assertEqual(result.error_code, "SMARTIRIS_LOCK_TIMEOUT")
                self.assertEqual(session.calls, [])
                child.communicate("done\n", timeout=5)
                self.assertEqual(client.open_study(launch_context()).status, "HANDOFF_SENT")
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
