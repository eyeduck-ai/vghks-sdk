from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import requests

from vghks_sdk import RequestPolicy
from vghks_sdk.cli import main
from vghks_sdk.core.errors import ConfigurationError, RequestError
from vghks_sdk.core.transport import SafeSessionTransport
from vghks_sdk.live.capture import RawCaptureRecorder


class PreparedFakeSession:
    def __init__(self, *, body: bytes, content_type: str = "text/html") -> None:
        self.headers: dict[str, str] = {}
        self.cookies = requests.cookies.RequestsCookieJar()
        self.cookies.set("JSESSIONID", "FULL-COOKIE-VALUE", domain="example.test", path="/")
        self.body = body
        self.content_type = content_type

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        helper = requests.Session()
        helper.headers.update(self.headers)
        helper.cookies.update(self.cookies)
        request = requests.Request(
            method,
            url,
            headers=kwargs.get("headers"),
            params=kwargs.get("params"),
            data=kwargs.get("data"),
            json=kwargs.get("json"),
            cookies=kwargs.get("cookies"),
        )
        prepared = helper.prepare_request(request)
        response = requests.Response()
        response.status_code = 200
        response.reason = "OK"
        response.url = prepared.url
        response.headers["Content-Type"] = self.content_type
        response.headers["X-Full-Token"] = "RESPONSE-TOKEN-VALUE"
        response._content = self.body
        response.encoding = None
        response.request = prepared
        response.history = []
        response.cookies.set("SSO", "RESPONSE-COOKIE-VALUE")
        return response

    def close(self) -> None:
        pass


class RetryingPreparedSession(PreparedFakeSession):
    def __init__(self) -> None:
        super().__init__(body=b"retry")
        self.calls = 0

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self.calls += 1
        response = super().request(method, url, **kwargs)
        response.status_code = 503 if self.calls == 1 else 200
        response.reason = "Service Unavailable" if self.calls == 1 else "OK"
        return response


class FailingSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.cookies = requests.cookies.RequestsCookieJar()

    def request(self, *_: Any, **__: Any) -> requests.Response:
        raise requests.ConnectionError("FULL-NETWORK-ERROR")

    def close(self) -> None:
        pass


class RawCaptureTests(unittest.TestCase):
    def test_capture_preserves_password_cookie_token_and_exact_response_bytes(self) -> None:
        response_bytes = b"\x00RAW RESPONSE\xffTOKEN"
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "run"
            recorder = RawCaptureRecorder(output)
            session = PreparedFakeSession(
                body=response_bytes, content_type="application/octet-stream"
            )
            transport = SafeSessionTransport(
                policy=RequestPolicy(min_delay_seconds=0, max_delay_seconds=0, max_attempts=1),
                session=session,  # type: ignore[arg-type]
                sleeper=lambda _: None,
                raw_capture=recorder,
            )
            transport.request(
                "POST",
                "https://example.test/login.do",
                params={"HID": "FULL-HID", "ssID": "FULL-SSID"},
                data={
                    "muid": "TEST-USER",
                    "mpassword": "FULL-PASSWORD",
                    "keyOne": "FULL-KEY-ONE",
                },
                retry_safe=True,
            )
            recorder.close()

            manifest_text = (output / "capture_manifest.jsonl").read_text(encoding="utf-8")
            request_text = (output / "requests" / "000001.json").read_text(encoding="utf-8")
            manifest = json.loads(manifest_text.splitlines()[0])
            response_path = output / manifest["response_file"]
            combined = manifest_text + request_text
            for secret in (
                "FULL-PASSWORD",
                "FULL-COOKIE-VALUE",
                "FULL-HID",
                "FULL-SSID",
                "FULL-KEY-ONE",
                "RESPONSE-TOKEN-VALUE",
            ):
                self.assertIn(secret, combined)
            self.assertEqual(response_path.suffix, ".bin")
            self.assertEqual(response_path.read_bytes(), response_bytes)

    def test_transport_without_raw_recorder_creates_no_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session = PreparedFakeSession(body=b"ordinary response")
            transport = SafeSessionTransport(
                policy=RequestPolicy(min_delay_seconds=0, max_delay_seconds=0, max_attempts=1),
                session=session,  # type: ignore[arg-type]
                sleeper=lambda _: None,
            )
            transport.request("GET", "https://example.test/query.do")
            self.assertEqual(list(root.iterdir()), [])

    def test_normal_cli_command_never_constructs_raw_capture(self) -> None:
        fake_sdk = MagicMock()
        fake_sdk.__enter__.return_value = fake_sdk
        fake_sdk.__exit__.return_value = None
        from vghks_sdk.core.readiness import make_auth_report
        from vghks_sdk.models import AuthCheckTarget

        fake_sdk.auth.check.return_value = make_auth_report(
            (
                AuthCheckTarget(
                    "portal", ("Login", "Session"), "/sessionCheck.do", False, 1, 1.0, (), "OK"
                ),
            )
        )
        environment = {
            "VGHKS_USERNAME": "TEST-USER",
            "VGHKS_PASSWORD": "TEST-PASSWORD",
            "VGHKS_CA_BUNDLE": "",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = Path(temp_dir) / "auth.json"
            with (
                patch.dict("os.environ", environment, clear=False),
                patch("vghks_sdk.cli.VghksSDK", return_value=fake_sdk),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(main(["auth-check", "--output", str(report_path)]), 0)
            self.assertTrue(report_path.is_file())

    def test_nonempty_capture_directory_requires_overwrite_and_owner_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "existing"
            output.mkdir()
            (output / "sentinel.txt").write_text("keep", encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                RawCaptureRecorder(output)
            with self.assertRaises(ConfigurationError):
                RawCaptureRecorder(output, overwrite=True)
            self.assertEqual((output / "sentinel.txt").read_text(), "keep")

    def test_retry_attempts_and_backoff_are_written_to_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "run"
            recorder = RawCaptureRecorder(output)
            session = RetryingPreparedSession()
            transport = SafeSessionTransport(
                policy=RequestPolicy(
                    min_delay_seconds=0,
                    max_delay_seconds=0,
                    max_attempts=2,
                    backoff_base_seconds=0,
                ),
                session=session,  # type: ignore[arg-type]
                sleeper=lambda _: None,
                raw_capture=recorder,
            )
            transport.request("GET", "https://example.test/retry.do")
            recorder.close()
            rows = [
                json.loads(line)
                for line in (output / "capture_manifest.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                [row["kind"] for row in rows], ["HTTP_EXCHANGE", "RETRY", "HTTP_EXCHANGE"]
            )
            self.assertEqual(rows[0]["attempt"], 1)
            self.assertTrue(rows[0]["will_retry"])
            self.assertEqual(rows[1]["reason"], "HTTP_503")
            self.assertEqual(rows[1]["next_attempt"], 2)
            self.assertEqual(rows[2]["attempt"], 2)

    def test_network_error_preserves_unprepared_values_and_full_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "run"
            recorder = RawCaptureRecorder(output)
            transport = SafeSessionTransport(
                policy=RequestPolicy(min_delay_seconds=0, max_delay_seconds=0, max_attempts=1),
                session=FailingSession(),  # type: ignore[arg-type]
                sleeper=lambda _: None,
                raw_capture=recorder,
            )
            with self.assertRaises(RequestError):
                transport.request(
                    "POST",
                    "https://example.test/login.do",
                    data={"mpassword": "FULL-FAILED-PASSWORD"},
                )
            recorder.close()
            text = (output / "capture_manifest.jsonl").read_text(encoding="utf-8")
            self.assertIn("NETWORK_ERROR", text)
            self.assertIn("FULL-FAILED-PASSWORD", text)
            self.assertIn("FULL-NETWORK-ERROR", text)


if __name__ == "__main__":
    unittest.main()
