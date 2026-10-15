"""Measure uncached/cached offline analysis using one unchanged local ZIP.

Defaults to synthetic responses. Reports contain counts, timings and digests;
input paths, patient identifiers, response values and credentials are omitted.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import io
import json
import statistics
import sys
import tempfile
import time
import tracemalloc
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vghks_sdk import __version__  # noqa: E402
from vghks_sdk.local_io import write_json_atomic  # noqa: E402
from vghks_sdk.offline import cache as analysis_cache  # noqa: E402
from vghks_sdk.offline import requests as captured_requests  # noqa: E402
from vghks_sdk.offline.analyze import _inspect_bundle, inspect_bundle  # noqa: E402
from vghks_sdk.offline.bundle import BundleReader  # noqa: E402
from vghks_sdk.offline.presentation import _markdown  # noqa: E402


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _run(source: Path, *, cached: bool, trace: bool = False):
    reads = Counter()
    decodes = 0
    request_parses = 0
    original_read, original_loads = BundleReader.read, json.loads
    original_parts = captured_requests.request_parts

    def read(reader, name):
        reads[name] += 1
        return original_read(reader, name)

    def loads(value, *args, **kwargs):
        nonlocal decodes
        decodes += 1
        return original_loads(value, *args, **kwargs)

    def parts(request):
        nonlocal request_parses
        request_parses += 1
        return original_parts(request)

    gc.collect()
    if trace:
        tracemalloc.start()
    try:
        started = time.perf_counter()
        with (patch.object(BundleReader, "read", read), patch.object(json, "loads", loads),
              patch.object(captured_requests, "request_parts", parts),
              patch.object(analysis_cache, "request_parts", parts), BundleReader(source) as reader):
            report, retest = (inspect_bundle if cached else _inspect_bundle)(reader)
            files = len(reader.names)
        seconds = time.perf_counter() - started
        peak = tracemalloc.get_traced_memory()[1] if trace else None
    finally:
        if trace:
            tracemalloc.stop()
    markdown = _markdown(report)
    report.pop("analyzer_build", None)
    return (report, retest, markdown), {
        "seconds": seconds, "peak_traced_bytes": peak, "file_count": files,
        "file_reads": sum(reads.values()), "manifest_reads": reads["capture_manifest.jsonl"],
        "json_decodes": decodes, "request_parses": request_parses,
    }


def benchmark(source: Path, iterations: int = 5) -> dict:
    if not 1 <= iterations <= 20:
        raise ValueError("iterations must be between 1 and 20")
    source = source.expanduser().resolve()
    original_hash = _file_hash(source) if source.is_file() else None
    samples = {False: [], True: []}
    reference = None
    with patch("socket.socket.connect", side_effect=AssertionError("offline benchmark cannot connect")):
        for index in range(iterations):
            for enabled in (False, True) if index % 2 == 0 else (True, False):
                result, metrics = _run(source, cached=enabled)
                if reference is None:
                    reference = result
                if result != reference:
                    raise AssertionError("analysis or retest changed")
                samples[enabled].append(metrics)
        output = {}
        for enabled in (False, True):
            result, traced = _run(source, cached=enabled, trace=True)
            if result != reference:
                raise AssertionError("traced analysis or retest changed")
            output["cached" if enabled else "uncached"] = {
                "median_seconds": statistics.median(row["seconds"] for row in samples[enabled]),
                "peak_traced_bytes": traced["peak_traced_bytes"],
                **{key: traced[key] for key in ("file_count", "file_reads", "manifest_reads", "json_decodes", "request_parses")},
            }
    unchanged = original_hash == _file_hash(source) if original_hash else None
    if unchanged is False:
        raise AssertionError("original ZIP changed")
    return {
        "schema_version": 1, "sdk_version": __version__, "iterations": iterations,
        "input_kind": "ZIP" if original_hash else "DIRECTORY", "original_zip_unchanged": unchanged,
        "full_report_equal": True, "retest_equal": True, "markdown_equal": True,
        "report_digest": _digest(reference[0]), "retest_digest": _digest(reference[1]),
        "cache_scope": "one ZIP analysis; directories and binary bodies bypass cache",
        **output,
    }


def _synthetic_zip(folder: Path, records: int) -> Path:
    import requests

    from vghks_sdk.live.bundle import LiveTestBundleManager
    from vghks_sdk.live.capture import RawCaptureRecorder

    manager = LiveTestBundleManager(folder / "run")
    manager.write_config({"schema_version": 4, "profile": "atomic", "test_mrn": "0000000",
                          "only_operations": ["prq.allergy"]})
    request = requests.Request("GET", "https://synthetic.invalid/PRQWeb/QueryMrData.do",
        params={"hhisnum": "0000000", "hid": "synthetic", "reqCode": "getUdhcdsps"}).prepare()
    with RawCaptureRecorder(manager.run_directory, prepare_root=False) as capture:
        for index in range(records):
            response = requests.Response()
            response.request, response.url, response.status_code = request, request.url, 200
            response.headers["Content-Type"] = "application/json; charset=utf-8"
            response._content = b'{"hhisnum":"0000000","allergy":false,"allergyMsg":""}'
            capture.set_live_step(f"prq.allergy.{index:04d}")
            capture.record_response(response=response, attempt=1, max_attempts=1,
                throttle_delay_seconds=0, elapsed_seconds=0, will_retry=False)
    steps = [{"name": f"prq.allergy.{index:04d}", "operation": "prq.allergy", "status": "OK"}
             for index in range(records)]
    with redirect_stdout(io.StringIO()):
        return manager.finalize(status="OK", summary={"schema_version": 6,
            "profile": "atomic", "status": "OK", "steps": steps}).archive_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="optional local ZIP/directory; read only")
    parser.add_argument("--synthetic-records", type=int, default=600)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--output", type=Path, default=Path("output/offline-benchmark.json"))
    args = parser.parse_args()
    if not 1 <= args.iterations <= 20 or not 1 <= args.synthetic_records <= 10000:
        parser.error("iterations must be 1-20 and synthetic-records must be 1-10000")
    if args.input:
        source_path = args.input.expanduser().resolve()
        output_path = args.output.expanduser().resolve()
        if output_path == source_path or (source_path.is_dir() and output_path.is_relative_to(source_path)):
            parser.error("benchmark output must be outside the original bundle")
    with tempfile.TemporaryDirectory(prefix="offline-benchmark-") as temporary:
        source = args.input or _synthetic_zip(Path(temporary), args.synthetic_records)
        result = benchmark(source, args.iterations)
    write_json_atomic(args.output, result)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
