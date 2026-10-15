"""Dependency-free full/subset test runner with optional process isolation."""

from __future__ import annotations

import argparse
import fnmatch
import io
import json
import multiprocessing
import signal
import sys
import time
import unittest
from collections import Counter, defaultdict
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "src"))


class _TimedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.timings = []
        self._started = 0.0

    def startTest(self, test):
        self._started = time.perf_counter()
        super().startTest(test)

    def stopTest(self, test):
        self.timings.append({"test": test.id(), "seconds": time.perf_counter() - self._started})
        super().stopTest(test)


class _CapturedOutput(io.StringIO):
    # The SDK self-check inspects these attributes; plain StringIO has no encoding.
    encoding = "utf-8"


def _cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _cases(item)
        else:
            yield item


def _summary(result):
    return {"tests": result.testsRun, "skipped": len(result.skipped),
            "failures": len(result.failures), "errors": len(result.errors),
            "expected_failures": len(result.expectedFailures),
            "unexpected_successes": len(result.unexpectedSuccesses),
            "success": result.wasSuccessful(), "timings": result.timings}


def _worker_setup():
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(ROOT), str(ROOT / "src"), str(ROOT / "tests")]


def _run_module(task):
    module, names = task
    output, log = _CapturedOutput(), io.StringIO()
    suite = unittest.TestLoader().loadTestsFromNames(names)
    started = time.perf_counter()
    with redirect_stdout(output), redirect_stderr(output):
        result = unittest.TextTestRunner(stream=log, verbosity=1, resultclass=_TimedResult).run(suite)
    return {**_summary(result), "module": module, "seconds": time.perf_counter() - started,
            "log": log.getvalue(), "output": output.getvalue()}


def _parallel(groups, jobs):
    results = []
    pool = multiprocessing.get_context("spawn").Pool(jobs, initializer=_worker_setup)
    try:
        for result in pool.imap_unordered(_run_module, groups.items()):
            results.append(result)
            print(f"[{len(results)}/{len(groups)}] {result['module']}: {result['tests']} tests, "
                  f"{result['seconds']:.2f}s, {'OK' if result['success'] else 'FAILED'}", flush=True)
            if not result["success"]:
                sys.stderr.write(result["output"] + result["log"])
        pool.close()
        pool.join()
    except BaseException:
        pool.terminate()
        pool.join()
        raise
    return {"success": all(item["success"] for item in results),
            "timings": [row for item in results for row in item["timings"]],
            **{key: sum(item[key] for item in results) for key in (
                "tests", "skipped", "failures", "errors", "expected_failures", "unexpected_successes"
            )}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pattern", action="append", help="test filename pattern; repeat to combine")
    parser.add_argument("--jobs", type=int, default=1, help="isolated worker processes, 1-8 (default: 1)")
    parser.add_argument("--durations", type=int, default=0, help="show N slowest tests")
    parser.add_argument("--timings", type=Path, help="write test-name/timing metadata as JSON")
    parser.add_argument("--list", action="store_true", help="list selected test IDs without running")
    args = parser.parse_args(argv)
    if not 1 <= args.jobs <= 8 or args.durations < 0:
        parser.error("jobs must be 1-8 and durations must be nonnegative")
    loader = unittest.TestLoader()
    if args.pattern:
        names = sorted(path.name for path in (ROOT / "tests").glob("test_*.py")
                       if any(fnmatch.fnmatchcase(path.name, pattern) for pattern in args.pattern))
        suite = unittest.TestSuite(loader.discover(str(ROOT / "tests"), pattern=name) for name in names)
    else:
        suite = loader.discover(str(ROOT / "tests"))
    cases = list(_cases(suite))
    if not cases:
        parser.error("no tests matched; no successful empty run")
    if args.list:
        print("\n".join(test.id() for test in cases))
        return 0
    groups = defaultdict(list)
    for test in cases:
        groups[test.__class__.__module__].append(test.id())
    selected_ids = Counter(name for names in groups.values() for name in names)
    parallel = args.jobs > 1 and not loader.errors and all(
        test.__class__.__module__ != "unittest.loader" for test in cases
    )
    started = time.perf_counter()
    try:
        if not parallel:
            result = unittest.TextTestRunner(verbosity=2, resultclass=_TimedResult).run(suite)
            summary = _summary(result)
        else:
            summary = _parallel(groups, min(args.jobs, len(groups)))
    except KeyboardInterrupt:
        print("Tests interrupted; run incomplete.", file=sys.stderr)
        return 130
    if Counter(row["test"] for row in summary["timings"]) != selected_ids:
        summary["success"] = False
        summary["errors"] += 1
        print("FAILED: selected and executed test IDs differ; run incomplete.", file=sys.stderr)
    summary["seconds"] = time.perf_counter() - started
    if parallel:
        print(f"\nRan {summary['tests']} tests in {summary['seconds']:.3f}s", file=sys.stderr)
        counts = ", ".join(f"{key}={summary[key]}" for key in (
            "failures", "errors", "skipped", "expected_failures", "unexpected_successes"
        ) if summary[key])
        print(("OK" if summary["success"] else "FAILED") + (f" ({counts})" if counts else ""),
              file=sys.stderr)
    for row in sorted(summary["timings"], key=lambda value: value["seconds"], reverse=True)[:args.durations]:
        print(f"{row['seconds']:.3f}s {row['test']}")
    if args.timings:
        args.timings.parent.mkdir(parents=True, exist_ok=True)
        args.timings.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return 0 if summary["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
