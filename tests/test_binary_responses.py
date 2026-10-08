"""Synthetic attachment bytes through production runtime, auth and replay."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import requests

from vghks_sdk import PortalCredentials, RequestPolicy, SDKSettings, VghksSDK
from vghks_sdk.adapters.auth import AppSession
from vghks_sdk.core.diagnostics import _response_structure
from vghks_sdk.core.errors import (
    AuthExpiredError,
    ParseError,
    PasswordChangeRequiredError,
    RequestError,
)
from vghks_sdk.core.operations import operation_spec
from vghks_sdk.core.transport import SafeSessionTransport
from vghks_sdk.models import PacsImageRef, PdfAttachmentRef
from vghks_sdk.offline.replay import replay_response
from vghks_sdk.parsing.assets import parse_binary_asset

# HTMLParser rejects this declaration inside a binary stream. Public fixtures
# are manufactured here; no medical PDF or compressed patient data is copied.
PDF = b"%PDF-1.4\n1 0 obj\nstream\n<![synthetic-binary[\x00\xff\nendstream\nendobj\n%%EOF\n"
JPEG = b"\xff\xd8<![synthetic-binary[\x00\xff\xff\xd9"
LOGIN = b'<form><input name="muid"><input name="mpassword" type="password"></form>'
MRN = "0000000"


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


class BinaryResponseTests(unittest.TestCase):
    def sdk(self, session):
        policy = RequestPolicy(min_delay_seconds=0, max_delay_seconds=0, max_attempts=1)
        transport = SafeSessionTransport(policy=policy, session=session)
        sdk = VghksSDK(settings=SDKSettings(request_policy=policy),
                       credentials=PortalCredentials("SYNTHETIC", "SYNTHETIC"), transport=transport)
        self.addCleanup(sdk.close)
        auth = sdk._runtime.auth
        auth._portal_authenticated = True
        auth._generation = 1
        auth._apps["prq"] = AppSession("prq", "SYNTHETIC-HID", "https://example.test/landing")
        return sdk

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
        response = sdk._runtime.transport.session.request("GET", "https://example.test/file")
        response.status_code = 302
        response.headers["Location"] = sdk._runtime.settings.portal_base_url + "/index.do"
        sdk._runtime.transport.request = Mock(return_value=response)
        spec = operation_spec("prq.pdf_attachment")
        with self.assertRaises(AuthExpiredError):
            sdk._runtime.request_binary(spec, sdk._runtime.settings.prq_base_url + "/Page/JSP/showPDF.jsp", max_bytes=1024, allow_redirects=False)

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
