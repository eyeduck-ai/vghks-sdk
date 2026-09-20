"""Keep private artifacts out of both the Git tree and built distributions."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "public_tree", Path(__file__).resolve().parents[1] / "tools/check_public_tree.py"
)
assert spec and spec.loader
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class PublicTreeTests(unittest.TestCase):
    def test_private_outputs_remain_blocked_in_nested_distribution_paths(self):
        for path in (
            "release/private/settings.json",
            "release/data/fixture.txt",
            "vghks_sdk/live/_live_defaults.json",
            "dist/report.zip",
            "output/diagnostics/result.json",
            "docs/patient.har",
        ):
            with self.subTest(path=path):
                self.assertTrue(guard.check_content(path, b"{}"))

    def test_diagnostics_source_is_public_but_captures_are_not(self):
        for path in ("src/vghks_sdk/diagnostics/probe.py", "vghks_sdk/diagnostics/__init__.py"):
            self.assertEqual(guard.check_content(path, b'"""public code"""'), [])
        self.assertIn("private_path", guard.check_content("vghks_sdk/diagnostics/raw.json", b"{}"))

    def test_local_values_are_detected_after_unicode_normalization_without_echoing(self):
        result = guard.check_content(
            "tests/example.py", 'patient="９０００００１"'.encode(), denylist=("9000001",)
        )
        self.assertEqual(result, ["local_sensitive_value"])
        self.assertNotIn("9000001", str(result))

    def test_only_named_localhost_fixture_may_contain_a_private_key(self):
        key = b"-----BEGIN " + b"PRIVATE KEY-----"
        self.assertEqual(guard.check_content("tests/fixtures/tls/localhost-key.pem", key), [])
        self.assertIn("private_key", guard.check_content("tests/fixtures/server.pem", key))


if __name__ == "__main__":
    unittest.main()
