from __future__ import annotations

import ctypes
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vghks_sdk.core.errors import RequestError
from vghks_sdk.live.schannel import probe_schannel


class SchannelTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "context.json"
        self.options = []

        def option(handle, key, value, size):
            if key == 47:
                self.options.append((handle, key, value))
                self.assertIsNone(value)
                self.assertEqual(size, 0)
                return 1
            self.options.append(
                (handle, key, ctypes.cast(value, ctypes.POINTER(ctypes.c_uint32))[0])
            )
            return 1

        def status(handle, query, name, value, length, index):
            ctypes.cast(value, ctypes.POINTER(ctypes.c_uint32))[0] = 401
            return 1

        self.api = SimpleNamespace(
            WinHttpOpen=Mock(return_value=1),
            WinHttpConnect=Mock(return_value=2),
            WinHttpOpenRequest=Mock(return_value=3),
            WinHttpSetOption=Mock(side_effect=option),
            WinHttpSetTimeouts=Mock(return_value=1),
            WinHttpSendRequest=Mock(return_value=1),
            WinHttpReceiveResponse=Mock(return_value=1),
            WinHttpQueryHeaders=Mock(side_effect=status),
            WinHttpCloseHandle=Mock(return_value=1),
        )

    def call(self):
        with (
            patch("vghks_sdk.live.schannel.platform.system", return_value="Windows"),
            patch(
                "vghks_sdk.live.schannel._load_winhttp",
                return_value=self.api,
            ),
        ):
            return probe_schannel(
                "https://example.invalid:4434/app/", timeout_seconds=10, context_path=self.path
            )

    def test_probe_is_get_only_no_redirects_cookies_automatic_auth_or_certificate_bypass(self):
        result = self.call()
        self.assertEqual(result["http_status"], 401)
        self.assertTrue(result["reachable"])
        self.assertEqual(self.api.WinHttpOpen.call_args.args[1], 1)
        self.assertEqual(self.api.WinHttpOpenRequest.call_args.args[1:3], ("GET", "/app/"))
        self.assertEqual(self.options, [(1, 84, 0x800), (3, 63, 7), (3, 77, 2), (3, 47, None)])
        self.assertEqual(self.api.WinHttpCloseHandle.call_count, 3)

    def test_failure_has_windows_code_and_closes_all_handles(self):
        self.api.WinHttpSendRequest.return_value = 0
        with (
            patch("ctypes.get_last_error", return_value=12175, create=True),
            self.assertRaises(RequestError) as failure,
        ):
            self.call()
        self.assertEqual(failure.exception.info.code, "NETWORK_SCHANNEL_12175")
        self.assertEqual(json.loads(self.path.read_text())["windows_error"], 12175)
        self.api.WinHttpQueryHeaders.assert_not_called()
        self.assertEqual(self.api.WinHttpCloseHandle.call_count, 3)

    def test_configuration_failure_prevents_any_request(self):
        self.api.WinHttpSetOption.return_value = 0
        self.api.WinHttpSetOption.side_effect = None
        with (
            patch("ctypes.get_last_error", return_value=87, create=True),
            self.assertRaises(RequestError),
        ):
            self.call()
        self.api.WinHttpSendRequest.assert_not_called()


if __name__ == "__main__":
    unittest.main()
