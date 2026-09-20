"""SDK-owned TLS selection and recovery; all network traffic is localhost."""

from __future__ import annotations

import io
import json
import ssl
import tempfile
import unittest
import warnings
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import patch

import requests
import test_tls as tls_fixtures

from vghks_sdk import PortalCredentials, RequestPolicy, SDKSettings, VghksSDK
from vghks_sdk.core.errors import ConfigurationError, RequestError
from vghks_sdk.core.tls import TLS12_COMPAT, TLS_DEFAULT
from vghks_sdk.live.bundle import LiveTestBundleManager
from vghks_sdk.live.capture import RawCaptureRecorder
from vghks_sdk.offline.analyze import inspect_bundle
from vghks_sdk.offline.bundle import BundleReader


def policy(**changes):
    return RequestPolicy(min_delay_seconds=0, max_delay_seconds=0, backoff_base_seconds=0, **changes)


class AutomaticTLSPolicyTests(unittest.TestCase):
    def test_sdk_initializes_verified_compat_without_network_or_caller_configuration(self):
        with (
            patch("requests.sessions.Session.request") as network,
            VghksSDK(settings=SDKSettings(), credentials=PortalCredentials("SYNTHETIC", "ONLY")) as sdk,
        ):
            status = sdk.connection_status()
            for app in ("prq", "sectord", "webmaas", "oppl", "oppl_records"):
                self.assertEqual(status[app]["tls_profile"], TLS12_COMPAT)
            self.assertEqual(status["portal"]["tls_profile"], TLS_DEFAULT)
            self.assertTrue(all(row["certificate_verification"] for row in status.values()))
            self.assertFalse(any(row["confirmed"] for row in status.values()))
            session = sdk._runtime.transport.session
            self.assertIs(
                session.get_adapter("https://zwmc01p.vghks.gov.tw:4430/SectOrdWeb/"),
                session.get_adapter("https://zwmc01p.vghks.gov.tw:4430/OPPLWeb/"),
            )
        network.assert_not_called()

    def test_strict_and_manual_policies_are_explicit_booleans(self):
        for change in ({"auto_tls": "false"}, {"allow_unverified_tls": 0}):
            with self.subTest(change=change), self.assertRaises(ConfigurationError):
                SDKSettings(**change)
        with patch.dict("os.environ", {"VGHKS_AUTO_TLS": "false", "VGHKS_ALLOW_UNVERIFIED_TLS": "0"}):
            settings = SDKSettings.from_env()
        with VghksSDK(settings=settings, credentials=PortalCredentials("SYNTHETIC", "ONLY")) as sdk:
            self.assertTrue(all(row["tls_profile"] == TLS_DEFAULT for row in sdk.connection_status().values()))
            self.assertFalse(settings.allow_unverified_tls)

    def test_read_post_recovers_but_login_or_mutation_is_never_resent(self):
        for retry_safe, expected in ((True, 2), (False, 1)):
            with (
                self.subTest(retry_safe=retry_safe),
                VghksSDK(
                    settings=SDKSettings(request_policy=policy()),
                    credentials=PortalCredentials("SYNTHETIC", "ONLY"),
                ) as sdk,
            ):
                sdk.configure_connection("prq", tls_profile=TLS_DEFAULT)
                url = sdk._runtime.settings.prq_base_url + "/read-or-write"
                response = requests.Response()
                response.status_code = 200
                response.url = url
                response._content = b"synthetic"
                failure = requests.exceptions.SSLError(ssl.SSLEOFError(8, "unexpected_eof"))
                with patch.object(sdk._runtime.transport.session, "request", side_effect=[failure, response]) as request:
                    if retry_safe:
                        sdk._runtime.transport.request("POST", url, retry_safe=True, data={"only": "synthetic"})
                    else:
                        with self.assertRaises(RequestError):
                            sdk._runtime.transport.request("POST", url, data={"only": "synthetic"})
                        self.assertFalse(sdk.connection_status()["prq"]["confirmed"])
                    self.assertEqual(request.call_count, expected)
                if not retry_safe:
                    with (
                        patch.object(sdk._runtime.transport, "_confirm_connection") as probe,
                        patch.object(sdk._runtime.transport.session, "request", return_value=response),
                    ):
                        sdk._runtime.transport.request("POST", url, data={"next": "operation"})
                    probe.assert_called_once()

    def test_tls_failures_are_bounded_and_unknown_redirect_hosts_never_downgrade(self):
        with VghksSDK(
            settings=SDKSettings(request_policy=policy()),
            credentials=PortalCredentials("SYNTHETIC", "ONLY"),
        ) as sdk:
            url = sdk._runtime.settings.prq_base_url + "/query"
            failure = requests.exceptions.SSLError(ssl.SSLEOFError(8, "unexpected_eof"))
            with patch.object(sdk._runtime.transport.session, "request", side_effect=failure) as request:
                with self.assertRaises(RequestError):
                    sdk._runtime.transport.request("GET", url)
                self.assertEqual(request.call_count, 3)
            self.assertTrue(all(row["certificate_verification"] for row in sdk.connection_status().values()))
            redirected = requests.exceptions.SSLError(
                ssl.SSLCertVerificationError(1, "CERTIFICATE_VERIFY_FAILED"),
                request=requests.Request("GET", "https://outside.invalid/redirect").prepare(),
            )
            with patch.object(sdk._runtime.transport.session, "request", side_effect=redirected) as request:
                with self.assertRaises(RequestError):
                    sdk._runtime.transport.request("GET", url)
                self.assertEqual(request.call_count, 1)
            self.assertTrue(all(row["certificate_verification"] for row in sdk.connection_status().values()))


class RecordingHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.respond(404 if self.path == "/" else 200)

    def do_POST(self):
        self.respond(self.server.post_status)

    def respond(self, status):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.seen.append((self.command, self.path, dict(self.headers), body))
        self.send_response(status)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


class AutomaticTLSLoopbackTests(unittest.TestCase):
    def setUp(self):
        self.tls = tls_fixtures.LegacyAesLoopbackTests()
        self.tls.setUp()
        self.addCleanup(self.tls.doCleanups)
        self.tls.server.RequestHandlerClass = RecordingHandler
        self.tls.server.seen = []
        self.tls.server.post_status = 200

    def sdk(self, **settings):
        sdk = VghksSDK(
            settings=SDKSettings(
                ca_bundle=self.tls.ca,
                request_policy=policy(),
                **settings,
            ),
            credentials=PortalCredentials("SYNTHETIC", "ONLY"),
        )
        self.addCleanup(sdk.close)
        sdk._runtime.transport.session.trust_env = False
        return sdk

    def test_known_service_uses_compat_on_first_request_and_retains_cookies(self):
        sdk = self.sdk(prq_base_url=self.tls.url)
        transport = sdk._runtime.transport
        transport.session.cookies.set("synthetic", "retained")
        for _ in range(2):
            response = transport.request("GET", self.tls.url)
            self.assertEqual(response.tls_details["cipher"], "AES128-SHA")
            self.assertTrue(response.tls_details["certificate_verification"])
            response.close()
        self.assertEqual(len(self.tls.server.seen), 2)
        self.assertEqual(sdk.connection_status()["prq"]["source"], "PREFERRED")
        self.assertTrue(sdk.connection_status()["prq"]["confirmed"])
        self.assertEqual(transport.session.cookies.get("synthetic"), "retained")

    def test_compat_can_recover_to_tls13_without_losing_certificate_checks(self):
        self.tls.server_context.minimum_version = ssl.TLSVersion.TLSv1_3
        self.tls.server_context.maximum_version = ssl.TLSVersion.TLSv1_3
        sdk = self.sdk(prq_base_url=self.tls.url)
        response = sdk._runtime.transport.request("GET", self.tls.url)
        self.assertEqual(response.tls_details["negotiated_protocol"], "TLSv1.3")
        self.assertTrue(response.tls_details["certificate_verification"])
        self.assertEqual(sdk.connection_status()["prq"]["tls_profile"], TLS_DEFAULT)
        response.close()

    def test_unknown_service_negotiates_anonymously_before_single_unsafe_post(self):
        sdk = self.sdk(portal_base_url=self.tls.url)
        transport = sdk._runtime.transport
        transport.session.cookies.set("synthetic", "retained")
        transport.session.auth = ("SYNTHETIC", "ONLY")
        transport.session.trust_env = True
        with (
            patch("requests.sessions.get_netrc_auth", return_value=("NETRC", "SECRET")) as netrc,
            patch.dict("os.environ", {"NO_PROXY": "localhost,127.0.0.1"}),
        ):
            response = transport.request("POST", self.tls.url, data={"password": "SYNTHETIC-ONLY"})
        netrc.assert_not_called()
        response.close()
        requests_seen = self.tls.server.seen
        self.assertEqual([item[:2] for item in requests_seen], [("GET", "/"), ("POST", "/PRQWeb/")])
        self.assertNotIn("Authorization", requests_seen[0][2])
        self.assertNotIn("Cookie", requests_seen[0][2])
        self.assertEqual(requests_seen[0][3], b"")
        self.assertIn("synthetic=retained", requests_seen[1][2]["Cookie"])
        self.assertIn(b"SYNTHETIC-ONLY", requests_seen[1][3])
        self.assertEqual(sdk.connection_status()["portal"]["tls_profile"], TLS12_COMPAT)
        self.tls.server.post_status = 503
        with self.assertRaises(RequestError) as failure:
            transport.request("POST", self.tls.url, data={"mutation": "SYNTHETIC-ONLY"})
        self.assertEqual(failure.exception.status_code, 503)
        self.assertEqual(len(self.tls.server.seen), 3)

    def test_certificate_fallback_is_scoped_cached_and_strict_mode_still_rejects(self):
        url = self.tls.url.replace("localhost", "127.0.0.1")
        sdk = self.sdk(prq_base_url=url)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", requests.packages.urllib3.exceptions.InsecureRequestWarning)
            for _ in range(2):
                response = sdk._runtime.transport.request("GET", url)
                self.assertFalse(response.tls_details["certificate_verification"])
                self.assertEqual(response.tls_details["negotiated_protocol"], "TLSv1.2")
                response.close()
        self.assertFalse(sdk.connection_status()["prq"]["certificate_verification"])
        self.assertTrue(sdk.connection_status()["portal"]["certificate_verification"])
        strict = self.sdk(prq_base_url=url, allow_unverified_tls=False)
        with self.assertRaises(RequestError) as failure:
            strict._runtime.transport.request("GET", url)
        self.assertEqual(failure.exception.info.code, "TLS_VERIFY_FAILED")
        self.assertTrue(strict.connection_status()["prq"]["certificate_verification"])
        self.assertEqual(len(self.tls.server.seen), 2)

    def test_recovered_probe_and_read_retry_remain_evidence_without_false_analysis_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = LiveTestBundleManager(Path(temp) / "run")
            sdk = self.sdk(portal_base_url=self.tls.url)
            with RawCaptureRecorder(manager.run_directory, prepare_root=False) as capture:
                sdk._runtime.transport.raw_capture = capture
                response = sdk._runtime.transport.request("POST", self.tls.url, data={"synthetic": "only"})
                response.close()
            with redirect_stdout(io.StringIO()):
                archive = manager.finalize(status="OK", summary={"schema_version": 6, "status": "OK", "steps": []})
            with BundleReader(archive.archive_path) as reader:
                report, _ = inspect_bundle(reader)
                exchanges = reader.jsonl("capture_manifest.jsonl")
            self.assertEqual(report["analysis_status"], "OK")
            self.assertIsNone(report["root_cause"])
            self.assertTrue(report["recovered_requests"])
            self.assertTrue(any(row.get("connection_probe") for row in exchanges))
            self.assertNotIn("SYNTHETIC-ONLY", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
