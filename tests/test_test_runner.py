"""Exercise real serial/parallel runner selection, counts and failure exits."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class TestRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        shutil.copyfile(Path(__file__).resolve().parents[1] / "run_tests.py", self.root / "run_tests.py")
        (self.root / "tests").mkdir()
        self.write("test_one.py", '''import sys, unittest
class First(unittest.TestCase):
    def test_one(self):
        self.assertTrue(sys.stdout.encoding)
    @unittest.skip("synthetic skip")
    def test_skip(self): pass
''')
        self.write("test_two.py", '''import unittest
class Second(unittest.TestCase):
    def test_two(self): self.assertTrue(True)
''')

    def write(self, name, source):
        (self.root / "tests" / name).write_text(source, "utf-8")

    def run_cli(self, *arguments):
        return subprocess.run([sys.executable, "run_tests.py", *arguments], cwd=self.root,
                              capture_output=True, text=True, encoding="utf-8", timeout=40)

    def test_patterns_are_deduplicated_and_list_does_not_execute_tests(self):
        self.write("test_two.py", '''import unittest
class Second(unittest.TestCase):
    def test_two(self): raise RuntimeError("must not run")
''')
        listed = self.run_cli("--pattern", "test_two.py", "--pattern", "test_*.py", "--list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(len(listed.stdout.splitlines()), 3)
        self.assertEqual(len(set(listed.stdout.splitlines())), 3)
        empty = self.run_cli("--pattern", "missing_*.py")
        self.assertEqual(empty.returncode, 2)
        self.assertIn("no tests matched", empty.stderr)

    def test_serial_and_spawn_workers_execute_the_same_ids_and_skip_counts(self):
        for jobs in (1, 2):
            with self.subTest(jobs=jobs):
                result = self.run_cli("--jobs", str(jobs), "--timings", f"timings-{jobs}.json")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                metadata = json.loads((self.root / f"timings-{jobs}.json").read_text("utf-8"))
                self.assertEqual((metadata["tests"], metadata["skipped"]), (3, 1))
                self.assertTrue(metadata["success"])
        first = json.loads((self.root / "timings-1.json").read_text("utf-8"))
        second = json.loads((self.root / "timings-2.json").read_text("utf-8"))
        self.assertEqual(sorted(row["test"] for row in first["timings"]),
                         sorted(row["test"] for row in second["timings"]))

    def test_parallel_assertions_and_import_errors_cannot_report_success(self):
        self.write("test_two.py", '''import unittest
class Second(unittest.TestCase):
    def test_two(self): self.fail("synthetic expected failure")
''')
        result = self.run_cli("--jobs", "2")
        self.assertEqual(result.returncode, 1)
        self.assertIn("synthetic expected failure", result.stderr)
        self.assertIn("Ran 3 tests", result.stderr)
        self.write("test_two.py", "raise ImportError('synthetic import error')")
        result = self.run_cli("--jobs", "2")
        self.assertEqual(result.returncode, 1)
        self.assertIn("synthetic import error", result.stderr)
        self.write("test_two.py", "import unittest; raise unittest.SkipTest('synthetic module skip')")
        result = self.run_cli("--jobs", "2", "--timings", "module-skip.json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        metadata = json.loads((self.root / "module-skip.json").read_text("utf-8"))
        self.assertEqual((metadata["tests"], metadata["skipped"]), (3, 2))

    def test_unexpected_success_is_a_failure_and_invalid_job_count_is_rejected(self):
        self.write("test_two.py", '''import unittest
class Second(unittest.TestCase):
    @unittest.expectedFailure
    def test_two(self): pass
''')
        result = self.run_cli("--jobs", "2")
        self.assertEqual(result.returncode, 1)
        self.assertIn("unexpected_successes=1", result.stderr)
        self.assertEqual(self.run_cli("--jobs", "0").returncode, 2)
        self.write("test_two.py", '''import unittest
class Second(unittest.TestCase):
    changed = False
    def id(self): return super().id() + (".changed" if self.changed else "")
    def test_two(self): self.changed = True
''')
        for jobs in (1, 2):
            result = self.run_cli("--jobs", str(jobs))
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("selected and executed test IDs differ", result.stderr)
