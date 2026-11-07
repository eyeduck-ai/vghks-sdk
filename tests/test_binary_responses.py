"""Synthetic attachment bytes through production runtime, auth and replay."""

from __future__ import annotations

import json
import tempfile
import unittest
from hashlib import sha256
from itertools import repeat
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from urllib3.exceptions import ReadTimeoutError

from vghks_sdk import PortalCredentials, RequestPolicy, SDKSettings, VghksSDK
from vghks_sdk.adapters.auth import AppSession
from vghks_sdk.core.diagnostics import DiagnosticRecorder, _response_structure
from vghks_sdk.core.errors import (
    AuthExpiredError,
    ParseError,
    PasswordChangeRequiredError,
    RequestError,
)
from vghks_sdk.core.operations import operation_spec
from vghks_sdk.core.transport import SafeSessionTransport
from vghks_sdk.live.capture import RawCaptureRecorder
from vghks_sdk.models import PacsImageRef, PdfAttachmentRef
from vghks_sdk.offline.bundle import BundleReader
from vghks_sdk.offline.replay import replay_bundle, replay_response
from vghks_sdk.parsing.assets import parse_binary_asset

# HTMLParser rejects this declaration inside a binary stream. Public fixtures
# are manufactured here; no medical PDF or compressed patient data is copied.
PDF = b"%PDF-1.4\n1 0 obj\nstream\n<![synthetic-binary[\x00\xff\nendstream\nendobj\n%%EOF\n"
JPEG = b"\xff\xd8<![synthetic-binary[\x00\xff\xff\xd9"
LOGIN = b'<form><input name="muid"><input name="mpassword" type="password"></form>'
MRN = "0000000"
PDF_REF = PdfAttachmentRef(MRN, f"//nfs01p/EMRU/{MRN}/report.pdf")
MIB = 1024 * 1024


class BinarySession:
    def __init__(self, content=PDF, mime="application/pdf", *, status=200, url=""):
        self.headers = {}
        self.cookies = requests.cookies.RequestsCookieJar()
        self.content, self.mime, self.status, self.url = content, mime, status, url
        self.calls = []
        self.last_response = None

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = requests.Response()
        response.status_code = self.status
        response._content = self.content
        response._content_consumed = True
        response.url = self.url or url
        response.headers["Content-Type"] = self.mime
        response.headers["Content-Length"] = str(len(self.content))
        response.request = requests.Request(method, url, params=kwargs.get("params")).prepare()
        self.last_response = response
        return response

    def close(self):
        pass


class StreamingBody:
    def __init__(self, chunks):
        self.chunks = chunks
        self.bytes_read = 0
        self.finished = False
        self.closed = False

    def stream(self, chunk_size, *, decode_content):
        for chunk in self.chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            self.bytes_read += len(chunk)
            yield chunk
        self.finished = True

    def close(self):
        self.closed = True


def streamed_response(body, *, status=200, **headers):
    response = requests.Response()
    response.status_code = status
    response.headers.update(headers)
    response.raw = body
    return response


class BinaryResponseTests(unittest.TestCase):
    def sdk(self, session, *, max_attempts=1, **recorders):
        policy = RequestPolicy(min_delay_seconds=0, max_delay_seconds=0, max_attempts=max_attempts)
        transport = SafeSessionTransport(policy=policy, session=session, sleeper=lambda _: None)
        sdk = VghksSDK(settings=SDKSettings(request_policy=policy),
                       credentials=PortalCredentials("SYNTHETIC", "SYNTHETIC"), transport=transport,
                       **recorders)
        self.addCleanup(sdk.close)
        auth = sdk._runtime.auth
        auth._portal_authenticated = True
        auth._generation = 1
        auth._apps["prq"] = AppSession("prq", "SYNTHETIC-HID", "https://example.test/landing")
        return sdk

    def streaming_session(self, *responses):
        # Exercise Requests' real Session, including hooks and redirect handling.
        session = requests.Session()
        session.trust_env = False
        pending = iter(responses)

        def send(request, **kwargs):
            self.assertIs(kwargs["stream"], True)
            response = next(pending)
            response.request = request
            response.url = request.url
            return response

        adapter_send = patch.object(session.get_adapter("https://example.test"), "send", side_effect=send)
        self.addCleanup(adapter_send.stop)
        return session, adapter_send.start()

    def test_pdf_and_jpeg_keep_original_bytes_without_text_or_policy_parsing(self):
        for content, mime, method, ref in (
            (PDF, "application/pdf", "download_pdf", PdfAttachmentRef(MRN, f"//nfs01p/EMRU/{MRN}/report.pdf")),
            (JPEG, "image/jpeg", "download_pacs_image", PacsImageRef(MRN, "REQ", "", "", "IMG")),
        ):
            for header in (mime, "", "application/octet-stream", "text/html; charset=utf-8"):
                with self.subTest(method=method, header=header):
                    session = BinarySession(content, header)
                    sdk = self.sdk(session)
                    with patch.object(sdk._runtime.transport, "text", side_effect=AssertionError("binary decoded")), \
                         patch("vghks_sdk.adapters.auth.parse_password_status", side_effect=AssertionError("binary parsed as HTML")):
                        asset = getattr(sdk.orders, method)(ref)
                    self.assertEqual((asset.content, asset.media_type), (content, mime))
                    self.assertEqual(len(session.calls), 1)
                    self.assertEqual(sdk.auth.password_status.status, "NO_NOTICE")
                    shape = _response_structure(session.last_response)
                    self.assertEqual(shape["kind"], "binary")
                    self.assertEqual(shape["decoded_as"], "")
                    self.assertNotIn("html_shape", shape)

    def test_header_alone_does_not_hide_login_or_forced_password_html(self):
        for body, error in ((LOGIN, AuthExpiredError),
                            ("<p>密碼已過期，必須先變更密碼才能登入</p>".encode(), PasswordChangeRequiredError)):
            with self.subTest(error=error.__name__):
                sdk = self.sdk(BinarySession(body, "application/pdf"))
                runtime = sdk._runtime
                spec = operation_spec("prq.pdf_attachment")
                with self.assertRaises(error):
                    runtime.request_binary(spec, runtime.settings.prq_base_url + "/Page/JSP/showPDF.jsp", max_bytes=1024)

    def test_redirect_final_url_and_http_denials_still_challenge_authentication(self):
        for status, url in ((200, "https://example.test/login.do"),
                            (200, "https://example.test/Page/SysErrorException.jsp"),
                            (401, ""), (403, "")):
            with self.subTest(status=status, url=url):
                sdk = self.sdk(BinarySession(PDF, status=status, url=url))
                runtime = sdk._runtime
                spec = operation_spec("prq.pdf_attachment")
                with self.assertRaises(AuthExpiredError):
                    runtime.request_binary(spec, runtime.settings.prq_base_url + "/Page/JSP/showPDF.jsp", max_bytes=1024)

    def test_explicit_login_redirect_is_checked_before_binary_return(self):
        sdk = self.sdk(BinarySession())
        runtime = sdk._runtime
        url = runtime.settings.prq_base_url + "/Page/JSP/showPDF.jsp"
        spec = operation_spec("prq.pdf_attachment")
        for location in (runtime.settings.portal_base_url + "/index.do", "/PRQWeb/Page/SysErrorException.jsp"):
            response = runtime.transport.session.request("GET", url)
            response.status_code = 302
            response.headers["Location"] = location
            runtime.transport.request = Mock(return_value=response)
            with self.subTest(location=location), self.assertRaises(AuthExpiredError):
                runtime.request_binary(spec, url, max_bytes=1024)
            self.assertIs(runtime.transport.request.call_args.kwargs["allow_redirects"], False)

    def test_attachment_redirect_does_not_forward_the_request_or_accept_its_body(self):
        session = BinarySession(JPEG, "image/jpeg", status=307)
        sdk = self.sdk(session)
        request = session.request

        def redirect(method, url, **kwargs):
            response = request(method, url, **kwargs)
            response.headers["Location"] = "https://outside.invalid/image"
            return response

        with patch.object(session, "request", side_effect=redirect), self.assertRaises(ParseError) as caught:
            sdk.orders.download_pacs_image(PacsImageRef(MRN, "REQ", "", "", "IMG"))
        self.assertEqual(caught.exception.info.code, "BINARY_ASSET_REDIRECT_UNRECOGNIZED")
        self.assertEqual(caught.exception.info.http_status, 307)
        self.assertEqual(len(session.calls), 1)
        self.assertIs(session.calls[0][2]["allow_redirects"], False)

    def test_truncated_and_html_viewer_files_keep_binary_validation_codes(self):
        for body, method, ref, code in (
            (PDF.removesuffix(b"%%EOF\n"), "download_pdf", PdfAttachmentRef(MRN, f"//nfs01p/EMRU/{MRN}/report.pdf"), "PDF_BINARY_INVALID"),
            (b'<html><embed type="application/pdf"></html>', "download_pdf", PdfAttachmentRef(MRN, f"//nfs01p/EMRU/{MRN}/report.pdf"), "PDF_BINARY_INVALID"),
            (JPEG[:-2], "download_pacs_image", PacsImageRef(MRN, "REQ", "", "", "IMG"), "PACS_JPEG_INVALID"),
        ):
            with self.subTest(code=code, body=body):
                session = BinarySession(body)
                sdk = self.sdk(session)
                with self.assertRaises(ParseError) as caught:
                    getattr(sdk.orders, method)(ref)
                self.assertEqual(caught.exception.info.code, code)
                self.assertEqual(len(session.calls), 1)

    def test_both_size_limits_remain_enforced_for_binary_payloads(self):
        for header in (str(len(PDF)), "", "invalid"):
            with self.subTest(header=header):
                sdk = self.sdk(BinarySession())
                response = sdk._runtime.transport.session.request("GET", "https://example.test/file")
                response.headers["Content-Length"] = header
                sdk._runtime.transport.request = Mock(return_value=response)
                spec = operation_spec("prq.pdf_attachment")
                with self.assertRaises(RequestError) as caught:
                    sdk._runtime.request_binary(spec, sdk._runtime.settings.prq_base_url + "/Page/JSP/showPDF.jsp", max_bytes=4)
                self.assertEqual(caught.exception.info.code, "BINARY_ASSET_TOO_LARGE")
        with self.assertRaises(ParseError) as caught:
            parse_binary_asset(b"%PDF" + b"x" * (64 * 1024 * 1024), media_type="application/pdf")
        self.assertEqual(caught.exception.info.code, "ASSET_TOO_LARGE")

    def test_streaming_pdf_without_content_length_stops_over_64_mib_with_recorders(self):
        chunk = b"x" * (64 * 1024)
        body = StreamingBody(repeat(chunk, 80 * MIB // len(chunk)))
        response = streamed_response(body, **{"Transfer-Encoding": "chunked"})
        session, send = self.streaming_session(response)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with RawCaptureRecorder(root) as capture:
                diagnostics = DiagnosticRecorder(root / "diagnostics")
                try:
                    sdk = self.sdk(session, raw_capture=capture, diagnostics=diagnostics)
                    with self.assertRaises(RequestError) as caught:
                        sdk.orders.download_pdf(PDF_REF)
                    self.assertEqual(caught.exception.info.code, "BINARY_ASSET_TOO_LARGE")
                    self.assertEqual(caught.exception.info.http_status, 200)
                    self.assertTrue(body.closed)
                    self.assertFalse(body.finished)
                    self.assertEqual(body.bytes_read, 64 * MIB + len(chunk))
                    send.assert_called_once()
                finally:
                    diagnostics.finalize(command="synthetic", status="ERROR", exit_code=1)
            (root / "run_summary.json").write_text('{"schema_version":6}', encoding="utf-8")
            with BundleReader(root) as reader:
                row, = reader.jsonl("capture_manifest.jsonl")
                self.assertTrue(row["response"]["body_limit_exceeded"])
                self.assertIsNone(row["response_file"])
                replayed, = replay_bundle(reader)
            self.assertEqual((replayed["status"], replayed["error_code"]),
                             ("UNAVAILABLE", "BINARY_ASSET_TOO_LARGE"))
            events = [json.loads(line) for line in diagnostics.trace_path.read_text(encoding="utf-8").splitlines()]
            event, = [event for event in events if event["event"] == "http_response"]
            self.assertEqual(event["status_code"], 200)
            self.assertTrue(event["response"]["body_limit_exceeded"])

    def test_streamed_pdf_at_limit_keeps_bytes_size_and_hash(self):
        body = StreamingBody((PDF[:3], PDF[3:]))
        response = streamed_response(body, **{"Content-Type": "text/html"})
        session, send = self.streaming_session(response)
        sdk = self.sdk(session)
        with patch("vghks_sdk.adapters.prq_assets.MAX_BINARY_ASSET_BYTES", len(PDF)):
            asset = sdk.orders.download_pdf(PDF_REF)
        self.assertTrue(body.finished)
        self.assertEqual((asset.content, asset.size, asset.sha256),
                         (PDF, len(PDF), sha256(PDF).hexdigest()))
        send.assert_called_once()

    def test_over_limit_length_closes_redirect_without_requests_draining_body(self):
        body = StreamingBody((PDF,))
        response = streamed_response(body, status=307, Location="/foreign-attachment",
                                     **{"Content-Length": str(64 * MIB + 1)})
        session, send = self.streaming_session(response)
        sdk = self.sdk(session)
        with self.assertRaises(ParseError) as caught:
            sdk.orders.download_pdf(PDF_REF)
        self.assertEqual(caught.exception.info.code, "BINARY_ASSET_REDIRECT_UNRECOGNIZED")
        self.assertEqual(body.bytes_read, 0)
        self.assertTrue(body.closed)
        send.assert_called_once()

    def test_stream_timeout_closes_connection_and_retries_the_get(self):
        interrupted = StreamingBody((PDF[:4], ReadTimeoutError(None, "/synthetic", "Read timed out.")))
        session, send = self.streaming_session(
            streamed_response(interrupted), streamed_response(StreamingBody((PDF,))),
        )
        sdk = self.sdk(session, max_attempts=2)
        self.assertEqual(sdk.orders.download_pdf(PDF_REF).content, PDF)
        self.assertTrue(interrupted.closed)
        self.assertEqual(send.call_count, 2)

    def test_pdf_tail_validation_handles_whitespace_across_slices(self):
        for suffix in (b"%%EOF", b"%%EOF\r%Avision"):
            content = b"%PDF-1.4\n" + suffix + b" \r\t\n" * 2049
            with self.subTest(suffix=suffix):
                self.assertIs(parse_binary_asset(content, media_type="application/pdf").content, content)

    def test_offline_binary_replay_matches_live_without_running_html_parsers(self):
        for operation, body in (("prq.pdf_attachment", PDF), ("oppl_records.pdf", PDF), ("prq.pacs_image", JPEG)):
            with self.subTest(operation=operation), \
                 patch("vghks_sdk.offline.replay.parse_password_status", side_effect=AssertionError("binary parsed as HTML")):
                result = replay_response(operation, body, {}, mime="text/html")
            self.assertEqual(result["status"], "PARSED")
            self.assertEqual(result["data_availability"], "BINARY_AVAILABLE")
        result = replay_response("prq.pdf_attachment", LOGIN, {}, mime="application/pdf")
        self.assertEqual(result["error_code"], "AUTH_SESSION_LOGIN_FORM")
        result = replay_response("prq.pdf_attachment", PDF, {}, response_url="https://example.test/login.do")
        self.assertEqual(result["error_code"], "AUTH_SESSION_LOGIN_PAGE")
        result = replay_response("prq.pdf_attachment", PDF[:-6], {})
        self.assertEqual(result["error_code"], "PDF_BINARY_INVALID")


if __name__ == "__main__":
    unittest.main()
