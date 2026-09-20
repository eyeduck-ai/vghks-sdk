from __future__ import annotations

import ssl
import tempfile
import threading
import unittest
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests
from requests.adapters import HTTPAdapter

from vghks_sdk import PortalCredentials, SDKSettings, VghksSDK
from vghks_sdk.core.errors import ConfigurationError
from vghks_sdk.core.network_errors import network_error_code, recorded_network_error_code
from vghks_sdk.core.readiness import AUTH_CHECK_REGISTRY
from vghks_sdk.core.tls import (
    CUSTOM_PEM,
    REQUESTS_DEFAULT,
    TLS12,
    TLS12_COMPAT,
    UNVERIFIED,
    WINDOWS_SYSTEM,
    WindowsSystemTrustAdapter,
    create_requests_session,
    mount_tls_profile,
    resolve_tls_trust_mode,
)
from vghks_sdk.live.environment import environment_report
from vghks_sdk.live.preflight import run_network_checks


class TlsTrustTests(unittest.TestCase):
    def test_compatibility_profile_keeps_security_level_and_validates_name_and_chain(self):
        session, _ = create_requests_session(tls_profile=TLS12_COMPAT)
        self.addCleanup(session.close)
        context = session.get_adapter("https://example.invalid/").ssl_context
        names = {item["name"] for item in context.get_ciphers()}
        self.assertIn("AES128-SHA", names)
        self.assertIn("AES256-GCM-SHA384", names)
        self.assertFalse(any("RC4" in name or "3DES" in name or "ADH" in name for name in names))
        self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertEqual(context.maximum_version, ssl.TLSVersion.TLSv1_2)
        self.assertEqual(context.security_level, 2)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_applied_profile_preserves_cookies_and_is_scoped_to_exact_origin(self):
        with VghksSDK(settings=SDKSettings(), credentials=PortalCredentials("TEST", "TEST")) as sdk:
            session = sdk._runtime.transport.session
            session.cookies.set("test-session", "synthetic", domain="example.invalid")
            portal = session.get_adapter("https://portal.vghks.gov.tw/index.do")
            sdk.configure_connection("prq", tls_profile=TLS12_COMPAT, direct=True)
            profile = session.get_adapter("https://zwmc01p.vghks.gov.tw:4434/PRQWeb/WPSAutoLogon")
            self.assertEqual(profile.tls_profile, TLS12_COMPAT)
            self.assertTrue(profile.direct)
            self.assertIs(session.get_adapter("https://portal.vghks.gov.tw/index.do"), portal)
            self.assertIsNot(session.get_adapter("https://zwmc01p.vghks.gov.tw:4430/"), profile)
            self.assertIsNot(
                session.get_adapter("https://zwmc01p.vghks.gov.tw:4434.evil.invalid/"), profile
            )
            self.assertEqual(session.cookies.get("test-session"), "synthetic")
            with self.assertRaises(ConfigurationError):
                sdk.configure_connection("unknown", tls_profile=TLS12_COMPAT)

    def test_unverified_origin_keeps_other_origins_verified_and_preserves_cookie(self):
        with VghksSDK(settings=SDKSettings(), credentials=PortalCredentials("TEST", "TEST")) as sdk:
            session = sdk._runtime.transport.session
            session.cookies.set("synthetic", "retained")
            portal = session.get_adapter("https://portal.vghks.gov.tw/")
            sdk.configure_connection("prq", tls_profile=TLS12_COMPAT, verify_certificate=False)
            adapter = session.get_adapter("https://zwmc01p.vghks.gov.tw:4434/PRQWeb/")
            self.assertFalse(adapter.verify_certificate)
            self.assertEqual(adapter.ssl_context.verify_mode, ssl.CERT_NONE)
            self.assertFalse(adapter.ssl_context.check_hostname)
            self.assertEqual(adapter.ssl_context.security_level, 2)
            self.assertIs(session.get_adapter("https://portal.vghks.gov.tw/"), portal)
            self.assertTrue(getattr(portal, "verify_certificate", True))
            self.assertIsNot(session.get_adapter("https://zwmc01p.vghks.gov.tw:4430/"), adapter)
            self.assertIsNot(
                session.get_adapter("https://zwmc01p.vghks.gov.tw:4434.evil.invalid/"), adapter
            )
            self.assertEqual(session.cookies.get("synthetic"), "retained")

    def test_unverified_adapter_keeps_proxy_route_and_overrides_environment_ca(self):
        session, mode = create_requests_session(verify_certificate=False)
        self.addCleanup(session.close)
        self.assertEqual(mode, UNVERIFIED)
        request = requests.Request("GET", "https://example.invalid/").prepare()
        adapter = session.get_adapter(request.url)
        with patch("requests.adapters.HTTPAdapter.send") as send:
            adapter.send(
                request, verify="environment.pem", proxies={"https": "http://proxy.invalid"}
            )
        self.assertIs(send.call_args.kwargs["verify"], False)
        self.assertEqual(send.call_args.kwargs["proxies"], {"https": "http://proxy.invalid"})
        self.assertGreaterEqual(adapter.ssl_context.minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_direct_adapter_removes_only_proxy_routing_not_verification(self):
        session, _ = create_requests_session()
        self.addCleanup(session.close)
        mount_tls_profile(
            session,
            "https://example.invalid/app",
            ca_bundle=None,
            tls_profile=TLS12_COMPAT,
            direct=True,
        )
        request = requests.Request("GET", "https://example.invalid:443/app").prepare()
        with patch("requests.adapters.HTTPAdapter.send") as send:
            session.get_adapter(request.url).send(
                request,
                proxies={"https": "http://proxy.invalid"},
                verify="custom.pem",
            )
        self.assertEqual(send.call_args.kwargs["proxies"], {})
        self.assertEqual(send.call_args.kwargs["verify"], "custom.pem")

    def test_eof_certificate_and_protocol_errors_are_distinct_live_and_offline(self) -> None:
        from urllib3.exceptions import MaxRetryError
        from urllib3.exceptions import SSLError as PoolSSLError

        for inner, expected in (
            (ssl.SSLEOFError(8, "EOF occurred in violation of protocol"), "TLS_EOF"),
            (ssl.SSLCertVerificationError(1, "CERTIFICATE_VERIFY_FAILED"), "TLS_VERIFY_FAILED"),
            (ssl.SSLError(1, "WRONG_VERSION_NUMBER"), "TLS_PROTOCOL_FAILED"),
            (ssl.SSLError(1, "unknown"), "NETWORK_TLS_FAILED"),
        ):
            with self.subTest(expected=expected):
                error = requests.exceptions.SSLError(
                    MaxRetryError(None, "/", reason=PoolSSLError(inner))
                )
                self.assertEqual(network_error_code(error), expected)
                self.assertEqual(
                    recorded_network_error_code({"type": "SSLError", "message": str(error)}),
                    expected,
                )

    def test_tls12_probe_keeps_hostname_and_ca_checks_through_requests_pool_selection(self):
        for platform_name in ("Windows", "Linux"):
            # Keep third-party TLS backend imports bound to the actual host OS.
            with (
                self.subTest(platform=platform_name),
                patch("vghks_sdk.core.tls.platform", SimpleNamespace(system=lambda value=platform_name: value)),
            ):
                session, _ = create_requests_session(tls12_only=True)
                try:
                    request = requests.Request("GET", "https://example.invalid/").prepare()
                    adapter = session.get_adapter(request.url)
                    pool = adapter.get_connection_with_tls_context(request, verify=True)
                    context = pool.conn_kw["ssl_context"]
                    self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
                    self.assertEqual(context.maximum_version, ssl.TLSVersion.TLSv1_2)
                    self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
                    self.assertTrue(context.check_hostname)
                finally:
                    session.close()

    def test_windows_defaults_to_cryptoapi_for_direct_and_proxy_https(self) -> None:
        with patch("vghks_sdk.core.tls.platform", SimpleNamespace(system=lambda value="Windows": value)):
            session, trust_mode = create_requests_session()
        try:
            adapter = session.get_adapter("https://portal.example")
            self.assertEqual(trust_mode, WINDOWS_SYSTEM)
            self.assertIsInstance(adapter, WindowsSystemTrustAdapter)
            self.assertEqual(adapter.ssl_context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(adapter.ssl_context.check_hostname)
            self.assertIs(
                adapter.poolmanager.connection_pool_kw["ssl_context"],
                adapter.ssl_context,
            )
            proxy = adapter.proxy_manager_for("http://proxy.example:8080")
            self.assertIs(
                proxy.connection_pool_kw["ssl_context"],
                adapter.ssl_context,
            )
        finally:
            session.close()

    def test_custom_pem_has_priority_and_uses_requests_adapter(self) -> None:
        with patch("vghks_sdk.core.tls.platform", SimpleNamespace(system=lambda value="Windows": value)):
            session, trust_mode = create_requests_session(ca_bundle="hospital-ca.pem")
        try:
            self.assertEqual(trust_mode, CUSTOM_PEM)
            self.assertIsInstance(session.get_adapter("https://portal.example"), HTTPAdapter)
            self.assertNotIsInstance(
                session.get_adapter("https://portal.example"),
                WindowsSystemTrustAdapter,
            )
        finally:
            session.close()

    def test_non_windows_keeps_requests_default(self) -> None:
        with patch("vghks_sdk.core.tls.platform", SimpleNamespace(system=lambda value="Linux": value)):
            session, trust_mode = create_requests_session()
        try:
            self.assertEqual(trust_mode, REQUESTS_DEFAULT)
            self.assertNotIsInstance(
                session.get_adapter("https://portal.example"),
                WindowsSystemTrustAdapter,
            )
        finally:
            session.close()

    def test_missing_windows_truststore_fails_closed(self) -> None:
        with (
            patch("vghks_sdk.core.tls.platform", SimpleNamespace(system=lambda value="Windows": value)),
            patch(
                "vghks_sdk.core.tls.importlib.import_module",
                side_effect=ImportError("missing"),
            ),
            self.assertRaises(ConfigurationError) as raised,
        ):
            create_requests_session()
        self.assertEqual(raised.exception.info.code, "WINDOWS_TRUSTSTORE_UNAVAILABLE")

    def test_environment_reports_effective_trust_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch("vghks_sdk.core.tls.platform", SimpleNamespace(system=lambda value="Windows": value)):
                report = environment_report(root)
            self.assertEqual(report["ca_mode"], WINDOWS_SYSTEM)

            invalid = root / "invalid.pem"
            invalid.write_text("not a certificate", encoding="ascii")
            invalid_report = environment_report(root, ca_bundle=invalid)
            self.assertEqual(invalid_report["ca_mode"], "INVALID")

    def test_trust_mode_resolution_never_disables_verification(self) -> None:
        with patch("vghks_sdk.core.tls.platform", SimpleNamespace(system=lambda value="Windows": value)):
            self.assertEqual(resolve_tls_trust_mode(None), WINDOWS_SYSTEM)
            self.assertEqual(resolve_tls_trust_mode("hospital.pem"), CUSTOM_PEM)


class LegacyAesLoopbackTests(unittest.TestCase):
    """Real TLS handshake regression; synthetic credentials, no intranet calls."""

    def setUp(self):
        self.fixture = Path(__file__).parent / "fixtures" / "tls"
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_2
        context.set_ciphers("AES128-SHA:@SECLEVEL=2")
        context.load_cert_chain(
            str(self.fixture / "localhost-cert.pem"), str(self.fixture / "localhost-key.pem")
        )
        self.server_context = context

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):
                pass

        class Server(ThreadingHTTPServer):
            def get_request(self):
                sock, address = super().get_request()
                sock.settimeout(5)
                try:
                    return context.wrap_socket(sock, server_side=True), address
                except BaseException:
                    sock.close()
                    raise

        self.server = Server(("127.0.0.1", 0), Handler)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.05), daemon=True
        ).start()
        self.url = f"https://localhost:{self.server.server_port}/PRQWeb/"
        self.ca = str(self.fixture / "localhost-cert.pem")

    def test_default_tls12_fails_but_compat_negotiates_aes_with_verified_certificate(self):
        default, _ = create_requests_session(ca_bundle=self.ca, tls_profile=TLS12)
        compat, _ = create_requests_session(ca_bundle=self.ca, tls_profile=TLS12_COMPAT)
        self.addCleanup(default.close)
        self.addCleanup(compat.close)
        default.trust_env = compat.trust_env = False
        with self.assertRaises(requests.exceptions.SSLError):
            default.get(self.url, verify=self.ca, timeout=3)
        response = compat.get(self.url, verify=self.ca, timeout=3)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.tls_details["cipher"], "AES128-SHA")
        self.assertEqual(response.tls_details["negotiated_protocol"], "TLSv1.2")
        response.close()
        compat.close()
        with self.assertRaises(requests.exceptions.SSLError):
            compat.get(self.url.replace("localhost", "127.0.0.1"), verify=self.ca, timeout=3)

    def test_compat_profile_does_not_accept_untrusted_certificate(self):
        # Empty native trust, avoiding any machine-specific Windows trust state.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        from vghks_sdk.core.tls import TLSContextAdapter

        context.set_ciphers("AES128-SHA:@SECLEVEL=2")
        context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_2
        session = requests.Session()
        self.addCleanup(session.close)
        session.trust_env = False
        session.mount("https://", TLSContextAdapter(context, tls_profile=TLS12_COMPAT))
        with self.assertRaises(requests.exceptions.SSLError) as raised:
            session.get(self.url, timeout=3)
        self.assertEqual(network_error_code(raised.exception), "TLS_VERIFY_FAILED")

    def test_unverified_https_still_needs_compatible_ciphers_and_keeps_encryption(self):
        # The synthetic certificate is not trusted and does not cover this IP.
        url = self.url.replace("localhost", "127.0.0.1")
        default, _ = create_requests_session(tls_profile=TLS12, verify_certificate=False)
        compat, _ = create_requests_session(tls_profile=TLS12_COMPAT, verify_certificate=False)
        self.addCleanup(default.close)
        self.addCleanup(compat.close)
        default.trust_env = compat.trust_env = False
        with self.assertRaises(requests.exceptions.SSLError):
            default.get(url, timeout=3)
        with warnings.catch_warnings():
            warnings.simplefilter(
                "ignore", requests.packages.urllib3.exceptions.InsecureRequestWarning
            )
            # Deliberately pass verify=True: the mounted policy must govern the
            # actual request even when Session/environment defaults say True.
            response = compat.get(url, verify=True, timeout=3)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.tls_details["cipher"], "AES128-SHA")
        self.assertEqual(response.tls_details["negotiated_protocol"], "TLSv1.2")
        self.assertFalse(response.tls_details["certificate_verification"])
        response.close()

    def test_real_preflight_adopts_unverified_https_then_continues_authenticated_transport(self):
        import io
        from contextlib import redirect_stdout

        from vghks_sdk import RequestPolicy
        from vghks_sdk.core.errors import RequestError

        url = self.url.replace("localhost", "127.0.0.1")
        settings = SDKSettings(
            prq_base_url=url.rstrip("/"),
            request_policy=RequestPolicy(min_delay_seconds=0, max_delay_seconds=0, max_attempts=1),
        )
        with (
            tempfile.TemporaryDirectory() as temp,
            VghksSDK(settings=settings, credentials=PortalCredentials("TEST", "TEST")) as sdk,
            patch(
                "vghks_sdk.live.preflight.AUTH_CHECK_REGISTRY",
                tuple(spec for spec in AUTH_CHECK_REGISTRY if spec.key == "prq"),
            ),
            patch("vghks_sdk.live.preflight.probe_schannel", return_value={}),
            patch.dict("os.environ", {"NO_PROXY": "127.0.0.1"}),
            redirect_stdout(io.StringIO()),
            warnings.catch_warnings(),
        ):
            warnings.simplefilter(
                "ignore", requests.packages.urllib3.exceptions.InsecureRequestWarning
            )
            selections = run_network_checks(
                settings,
                [],
                root=Path(temp),
                configure_connection=sdk.configure_connection,
                allow_unverified_tls=True,
            )
            response = sdk._runtime.transport.request("GET", url, allow_redirects=False)
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.tls_details["certificate_verification"])
            response.close()
            # A server-side 501 proves the POST passed TLS. It must not be
            # mislabeled as a connectivity failure or sent again automatically.
            with self.assertRaises(RequestError) as failure:
                sdk._runtime.transport.request("POST", url, data={"synthetic": "only"})
            self.assertEqual(failure.exception.status_code, 501)
        self.assertTrue(selections["prq"]["applied"])
        self.assertFalse(selections["prq"]["certificate_verification"])
        self.assertEqual(selections["prq"]["tls_profile"], TLS12_COMPAT)

    def test_real_preflight_applies_compat_to_existing_sdk_session(self):
        import io
        from contextlib import redirect_stdout

        from vghks_sdk import RequestPolicy

        settings = SDKSettings(
            prq_base_url=self.url.rstrip("/"),
            ca_bundle=self.ca,
            request_policy=RequestPolicy(min_delay_seconds=0, max_delay_seconds=0),
        )
        with (
            tempfile.TemporaryDirectory() as temp,
            VghksSDK(settings=settings, credentials=PortalCredentials("TEST", "TEST")) as sdk,
        ):
            sdk._runtime.transport.session.cookies.set("synthetic", "retained")
            with (
                patch(
                    "vghks_sdk.live.preflight.AUTH_CHECK_REGISTRY",
                    tuple(spec for spec in AUTH_CHECK_REGISTRY if spec.key == "prq"),
                ),
                patch.dict("os.environ", {"NO_PROXY": "localhost,127.0.0.1"}),
                redirect_stdout(io.StringIO()),
            ):
                selections = run_network_checks(
                    settings, [], root=Path(temp), configure_connection=sdk.configure_connection
                )
                response = sdk._runtime.transport.request("GET", self.url, allow_redirects=False)
            self.assertTrue(selections["prq"]["applied"])
            self.assertEqual(selections["prq"]["tls_profile"], TLS12_COMPAT)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(sdk._runtime.transport.session.cookies.get("synthetic"), "retained")
            response.close()

    @unittest.skipUnless(__import__("platform").system() == "Windows", "Windows native TLS only")
    def test_real_windows_probe_records_native_security_failure(self):
        import json

        from vghks_sdk.core.errors import RequestError
        from vghks_sdk.live.schannel import probe_schannel

        self.server_context.set_ciphers("ECDHE-RSA-AES128-GCM-SHA256:@SECLEVEL=2")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "native.json"
            with self.assertRaises(RequestError) as error:
                probe_schannel(self.url, timeout_seconds=3, context_path=path)
            recorded = json.loads(path.read_text())
        # WinHTTP can reject the certificate (12175), or fail earlier because
        # its local credential/private-key provider is unavailable (12185/6).
        # Preserve the actual native cause instead of relabeling it as a CA error.
        self.assertIn(recorded["windows_error"], {12175, 12185, 12186})
        self.assertEqual(error.exception.info.code, f"NETWORK_SCHANNEL_{recorded['windows_error']}")
        self.assertEqual(recorded["phase"], "send")
        self.assertFalse(recorded["reachable"])
        self.assertTrue(recorded["certificate_verification"])


if __name__ == "__main__":
    unittest.main()
