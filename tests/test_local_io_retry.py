"""Windows rename denials preserve atomicity, limits and entry-point error codes."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vghks_sdk.core.errors import ConfigurationError
from vghks_sdk.core.readiness import make_auth_report
from vghks_sdk.local_io import write_auth_check_report, write_bytes_atomic, write_json_atomic


def windows_denial(code):
    error = PermissionError("synthetic Windows rename denial")
    error.winerror = code
    return error


class LocalRenameTests(unittest.TestCase):
    def test_transient_windows_denial_commits_one_complete_json_without_rewriting_it(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "state.json"
            target.write_text('{"old":true}', "utf-8")
            replace = os.replace
            attempts = []

            def rename(source, destination):
                attempts.append(source)
                self.assertEqual(json.loads(target.read_text("utf-8")), {"old": True})
                if len(attempts) <= 2:
                    raise windows_denial(5 if len(attempts) == 1 else 32)
                return replace(source, destination)

            with patch("vghks_sdk.local_io.os.replace", side_effect=rename), patch("vghks_sdk.local_io.sleep"):
                self.assertEqual(write_json_atomic(target, {"new": [1, 2, 3]}), target)
            self.assertEqual(json.loads(target.read_text("utf-8")), {"new": [1, 2, 3]})
            self.assertEqual(len(set(attempts)), 1)
            self.assertEqual(len(attempts), 3)
            self.assertFalse(list(target.parent.glob("*.tmp")))

    def test_binary_retry_keeps_exact_bytes_and_atomic_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "asset.bin"
            target.write_bytes(b"old")
            replace = os.replace
            attempts = 0

            def rename(source, destination):
                nonlocal attempts
                attempts += 1
                self.assertEqual(target.read_bytes(), b"old")
                if attempts == 1:
                    raise windows_denial(33)
                return replace(source, destination)

            with patch("vghks_sdk.local_io.os.replace", side_effect=rename), patch("vghks_sdk.local_io.sleep"):
                write_bytes_atomic(target, b"\x00synthetic\xff")
            self.assertEqual(target.read_bytes(), b"\x00synthetic\xff")
            self.assertEqual(attempts, 2)

    def test_permanent_denial_stops_with_original_codes_and_cleans_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "state.json"
            target.write_bytes(b"original")
            for write, code in (
                (lambda: write_json_atomic(target, {}), "OUTPUT_WRITE_FAILED"),
                (lambda: write_bytes_atomic(target, b"new"), "OUTPUT_WRITE_FAILED"),
                (lambda: write_auth_check_report(target, make_auth_report(())), "AUTH_REPORT_WRITE_FAILED"),
            ):
                with (self.subTest(code=code),
                      patch("vghks_sdk.local_io.os.replace", side_effect=windows_denial(5)) as replace,
                      patch("vghks_sdk.local_io.sleep") as sleep,
                      self.assertRaises(ConfigurationError) as caught):
                    write()
                self.assertEqual(caught.exception.info.code, code)
                self.assertEqual(replace.call_count, 4)
                self.assertEqual(sleep.call_count, 3)
                self.assertEqual(target.read_bytes(), b"original")
                self.assertFalse(list(target.parent.glob("*.tmp")))

    def test_other_write_errors_fail_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "state.json"
            for error in (PermissionError("other permission error"), OSError("disk failure")):
                with (self.subTest(error=type(error).__name__),
                      patch("vghks_sdk.local_io.os.replace", side_effect=error) as replace,
                      patch("vghks_sdk.local_io.sleep") as sleep,
                      self.assertRaises(ConfigurationError) as caught):
                    write_json_atomic(target, {})
                self.assertEqual(caught.exception.info.code, "OUTPUT_WRITE_FAILED")
                replace.assert_called_once()
                sleep.assert_not_called()
                self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
