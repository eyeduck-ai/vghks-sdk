"""Scoped ZIP cache behavior, bounded retention and original reader semantics."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from vghks_sdk.core.errors import ConfigurationError
from vghks_sdk.offline.analyze import inspect_bundle
from vghks_sdk.offline.bundle import BundleReader
from vghks_sdk.offline.cache import _AnalysisReader


class AnalysisCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def bundle(self):
        source = self.root / "synthetic.zip"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("run_summary.json", '{"schema_version":6,"status":"OK","steps":[]}')
            archive.writestr("capture_manifest.jsonl", "")
            archive.writestr("a.json", '{"rows":[{"value":"original"}]}')
            archive.writestr("b.json", '{"rows":[{"value":"other"}]}')
            archive.writestr("bad.json", "{unknown")
            archive.writestr("bad.jsonl", "{}\n[]\n")
        return source

    def test_cached_analysis_reads_once_and_leaves_public_reader_values_independent(self):
        with BundleReader(self.bundle()) as source, patch.object(source, "read", wraps=source.read) as reads:
            with _AnalysisReader(source) as cache:
                first = cache.json("a.json")
                self.assertIs(cache.json("a.json"), first)
                self.assertEqual(reads.call_count, 1)
                first["rows"][0]["value"] = "analysis-local"
            self.assertEqual(cache._cache_bytes, 0)
            self.assertEqual(cache._cache, {})
            outside = source.json("a.json")
            self.assertEqual(outside["rows"][0]["value"], "original")
            outside["rows"].clear()
            self.assertTrue(source.json("a.json")["rows"])
            with _AnalysisReader(source) as another:
                self.assertEqual(another.json("a.json")["rows"][0]["value"], "original")

    def test_eviction_and_entry_limits_bound_retention_without_losing_results(self):
        with BundleReader(self.bundle()) as source:
            with _AnalysisReader(source, max_bytes=2048, max_entries=1) as cache:
                a = cache.json("a.json")
                cache.json("b.json")
                self.assertEqual(len(cache._cache), 1)
                self.assertLessEqual(cache._cache_bytes, 2048)
                self.assertEqual(cache.json("a.json"), a)
                self.assertIsNot(cache.json("a.json"), a)
            with _AnalysisReader(source, max_entry_bytes=1) as cache:
                self.assertEqual(cache.json("a.json"), source.json("a.json"))
                self.assertEqual(cache._cache_bytes, 0)

    def test_directory_and_custom_readers_continue_to_read_changed_content(self):
        directory = self.root / "folder"
        directory.mkdir()
        (directory / "run_summary.json").write_text('{"schema_version":6}', "utf-8")
        (directory / "capture_manifest.jsonl").write_text("", "utf-8")
        value = directory / "value.json"
        value.write_text('{"value":1}', "utf-8")
        with BundleReader(directory) as source, _AnalysisReader(source) as cache:
            self.assertEqual(cache.json("value.json"), {"value": 1})
            value.write_text('{"value":2}', "utf-8")
            self.assertEqual(cache.json("value.json"), {"value": 2})
            self.assertEqual(cache._cache_bytes, 0)
        class CustomReader(BundleReader):
            def json(self, name):
                return super().json(name)
        with (CustomReader(self.bundle()) as source, _AnalysisReader(source) as cache,
              patch.object(source, "json", wraps=source.json) as reads):
            cache.json("a.json")
            cache.json("a.json")
            self.assertEqual(reads.call_count, 2)

    def test_missing_invalid_and_oversized_json_keep_original_failures(self):
        with BundleReader(self.bundle()) as source, _AnalysisReader(source) as cache:
            for name, code, reader in (("missing.json", "BUNDLE_FILE_MISSING", cache.json),
                                       ("../a.json", "BUNDLE_PATH_INVALID", cache.json),
                                       ("bad.json", "BUNDLE_JSON_INVALID", cache.json),
                                       ("bad.jsonl", "BUNDLE_JSONL_INVALID", cache.jsonl)):
                with self.subTest(name=name), self.assertRaises(ConfigurationError) as caught:
                    reader(name)
                self.assertEqual(caught.exception.info.code, code)
            self.assertEqual(cache.jsonl("missing.jsonl"), [])
            with patch("vghks_sdk.offline.bundle.MAX_FILE_BYTES", 8), self.assertRaises(ConfigurationError) as caught:
                cache.json("a.json")
            self.assertEqual(caught.exception.info.code, "BUNDLE_FILE_TOO_LARGE")
            self.assertEqual(cache._cache, {})

    def test_large_source_bypasses_cache_and_binary_reads_never_enter_it(self):
        with BundleReader(self.bundle()) as source, _AnalysisReader(source) as cache:
            with patch("vghks_sdk.offline.cache._MAX_SOURCE_BYTES", 1):
                self.assertEqual(cache.json("a.json"), source.json("a.json"))
                self.assertEqual(cache._cache, {})
            self.assertEqual(cache.read("a.json"), source.read("a.json"))
            self.assertEqual(cache._cache, {})

    def test_repeated_analysis_never_shares_mutable_report_or_retest_data(self):
        with patch("socket.socket.connect", side_effect=AssertionError("offline only")), BundleReader(self.bundle()) as source:
            first_report, first_retest = inspect_bundle(source)
            second_report, second_retest = inspect_bundle(source)
            self.assertEqual(first_report, second_report)
            self.assertEqual(first_retest, second_retest)
            first_report["operations"].clear()
            first_retest["profile"] = "changed"
            third_report, third_retest = inspect_bundle(source)
            self.assertEqual(third_report, second_report)
            self.assertEqual(third_retest, second_retest)

    def test_cache_is_cleared_on_failure_and_does_not_close_borrowed_archive(self):
        with BundleReader(self.bundle()) as source:
            with self.assertRaises(RuntimeError), _AnalysisReader(source) as cache:
                cache.json("a.json")
                raise RuntimeError("synthetic analysis failure")
            self.assertEqual(cache._cache_bytes, 0)
            self.assertIsNotNone(source.archive.fp)
            self.assertEqual(source.json("a.json")["rows"][0]["value"], "original")

    def test_benchmark_preserves_original_zip_and_only_reports_safe_metrics(self):
        from tools.benchmark_offline import benchmark

        source = self.bundle()
        original = source.read_bytes()
        result = benchmark(source, iterations=1)
        self.assertTrue(result["full_report_equal"])
        self.assertTrue(result["retest_equal"])
        self.assertTrue(result["original_zip_unchanged"])
        self.assertEqual(source.read_bytes(), original)
        self.assertLess(result["cached"]["file_reads"], result["uncached"]["file_reads"])
        self.assertNotIn(str(source), json.dumps(result))
        self.assertNotIn("original", json.dumps(result).replace("original_zip_unchanged", ""))

    def test_benchmark_cli_cannot_overwrite_its_source_zip(self):
        from contextlib import redirect_stderr

        from tools.benchmark_offline import main

        source = self.bundle()
        original = source.read_bytes()
        with (patch("sys.argv", ["benchmark_offline.py", "--input", str(source), "--output", str(source)]),
              redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught):
            main()
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(source.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
