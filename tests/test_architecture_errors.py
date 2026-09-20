from __future__ import annotations

import ast
import importlib.util
import io
import json
import socket
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import requests

import vghks_sdk.live.bundle as live_bundle
from vghks_sdk import RequestPolicy, __version__
from vghks_sdk.contracts.har import BASELINE_CONTRACTS, validate_contract_coverage
from vghks_sdk.core.errors import (
    ConfigurationError,
    ParseError,
    RequestError,
    error_info,
)
from vghks_sdk.core.operations import OPERATIONS, validate_operation_registry
from vghks_sdk.core.transport import SafeSessionTransport
from vghks_sdk.live.bundle import (
    COMPLETE_MARKER,
    INCOMPLETE_MARKER,
    LiveTestBundleManager,
)
from vghks_sdk.live.capture import RawCaptureRecorder
from vghks_sdk.live.console import configure_console_output
from vghks_sdk.live.environment import environment_report
from vghks_sdk.live_test_app import build_parser, run_live_test_namespace
from vghks_sdk.local_io import read_mrns, write_json_atomic
from vghks_sdk.models import to_jsonable

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "vghks_sdk"


class RaisingSession:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.headers: dict[str, str] = {}
        self.cookies = requests.cookies.RequestsCookieJar()

    def request(self, *_: object, **__: object) -> requests.Response:
        raise self.error

    def close(self) -> None:
        return None


class ErrorModelTests(unittest.TestCase):
    def test_error_info_serialization_is_stable_and_value_free(self) -> None:
        error = RequestError(
            "SECRET response body and patient value",
            status_code=503,
            code="HTTP_503",
            endpoint_path="https://internal.example/PRQWeb/query.do?mrn=SECRET",
            attempt=3,
            cause_type="ReadTimeout",
        )
        payload = to_jsonable(error.info)
        self.assertEqual(
            payload,
            {
                "code": "HTTP_503",
                "category": "HTTP",
                "operation": "",
                "app": "",
                "endpoint_path": "/PRQWeb/query.do",
                "http_status": 503,
                "attempt": 3,
                "cause_type": "ReadTimeout",
            },
        )
        self.assertNotIn("SECRET", json.dumps(payload))
        unknown = error_info(RuntimeError("PASSWORD=SECRET"))
        self.assertEqual(unknown.code, "UNEXPECTED_ERROR")
        self.assertEqual(unknown.cause_type, "RuntimeError")

    def test_transport_classifies_tls_timeouts_dns_and_connection(self) -> None:
        cases = (
            (requests.exceptions.SSLError("fixture"), "NETWORK_TLS_FAILED"),
            (requests.exceptions.ConnectTimeout("fixture"), "NETWORK_CONNECT_TIMEOUT"),
            (requests.exceptions.ReadTimeout("fixture"), "NETWORK_READ_TIMEOUT"),
            (
                requests.exceptions.ConnectionError(socket.gaierror("fixture")),
                "NETWORK_DNS_FAILED",
            ),
            (requests.exceptions.ConnectionError("fixture"), "NETWORK_CONNECTION_FAILED"),
        )
        for raised, expected in cases:
            with self.subTest(expected=expected):
                transport = SafeSessionTransport(
                    policy=RequestPolicy(
                        min_delay_seconds=0,
                        max_delay_seconds=0,
                        max_attempts=1,
                    ),
                    session=RaisingSession(raised),  # type: ignore[arg-type]
                    sleeper=lambda _: None,
                )
                with self.assertRaises(RequestError) as caught:
                    transport.request("GET", "https://internal.test/PRQWeb/query.do")
                self.assertEqual(caught.exception.info.code, expected)
                self.assertEqual(caught.exception.info.endpoint_path, "/PRQWeb/query.do")

    def test_input_and_output_io_failures_have_stable_codes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaises(ConfigurationError) as missing:
                read_mrns(root / "missing.txt")
            self.assertEqual(missing.exception.info.code, "INPUT_FILE_NOT_FOUND")

            invalid = root / "invalid.txt"
            invalid.write_bytes(b"\xff")
            with self.assertRaises(ConfigurationError) as encoding:
                read_mrns(invalid)
            self.assertEqual(encoding.exception.info.code, "INPUT_ENCODING_ERROR")

            with (
                patch(
                    "vghks_sdk.local_io.tempfile.NamedTemporaryFile",
                    side_effect=OSError("fixture"),
                ),
                self.assertRaises(ConfigurationError) as output,
            ):
                write_json_atomic(root / "result.json", {"ok": True})
            self.assertEqual(output.exception.info.code, "OUTPUT_WRITE_FAILED")


class ArchitectureTests(unittest.TestCase):
    def test_operation_registry_and_contract_coverage_are_complete(self) -> None:
        validate_operation_registry()
        validate_contract_coverage()
        self.assertEqual(len({spec.key for spec in OPERATIONS}), len(OPERATIONS))
        required = {spec.key for spec in OPERATIONS if spec.contract_required}
        covered = {contract.operation_key for contract in BASELINE_CONTRACTS}
        self.assertEqual(covered, required)
        self.assertTrue(all("://" not in spec.path for spec in OPERATIONS))

    def test_layers_do_not_bypass_their_declared_dependencies(self) -> None:
        for path in (PACKAGE / "workflows").glob("*.py"):
            modules = _imported_modules(path)
            self.assertFalse(
                modules & {"requests", "bs4", "vghks_sdk.core.transport"},
                path.name,
            )
            self.assertFalse(any("parsing" in module for module in modules), path.name)

        for path in (PACKAGE / "services").glob("*.py"):
            modules = _imported_modules(path)
            self.assertFalse(any("transport" in module for module in modules), path.name)
            self.assertFalse(any("parsing" in module for module in modules), path.name)
            self.assertNotIn("requests", modules, path.name)

        runtime_modules = _imported_modules(PACKAGE / "runtime.py")
        self.assertFalse(any("adapters" in module for module in runtime_modules))
        contract_source = (PACKAGE / "contracts" / "har.py").read_text(encoding="utf-8")
        for forbidden in ("PortalCredentials", "SDKSettings", "VghksSDK", "requests."):
            self.assertNotIn(forbidden, contract_source)

    def test_testing_namespace_is_gone_and_capture_is_only_assembled_by_live_runner(self) -> None:
        self.assertIsNone(importlib.util.find_spec("vghks_sdk.testing"))
        importers: list[str] = []
        for path in PACKAGE.rglob("*.py"):
            if path.name == "capture.py" and path.parent.name == "live":
                continue
            if "RawCaptureRecorder" in path.read_text(encoding="utf-8"):
                importers.append(path.relative_to(PACKAGE).as_posix())
        self.assertEqual(importers, ["live/runner.py"])

    def test_version_has_one_runtime_source(self) -> None:
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")
        occurrences: list[str] = []
        for path in PACKAGE.rglob("*.py"):
            if f'"{__version__}"' in path.read_text(encoding="utf-8"):
                occurrences.append(path.relative_to(PACKAGE).as_posix())
        self.assertEqual(occurrences, ["_version.py"])


class PortableBundleTests(unittest.TestCase):
    def test_precredential_config_failure_is_packaged_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bad_config = root / "bad.json"
            bad_config.write_text("{broken", encoding="utf-8")
            output = root / "run"
            args = build_parser().parse_args(
                [
                    "--non-interactive",
                    "--config",
                    str(bad_config),
                    "--output",
                    str(output),
                ]
            )
            with (
                patch("vghks_sdk.live_test_app.Path.cwd", return_value=root),
                patch("requests.sessions.Session.request") as network,
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                exit_code = run_live_test_namespace(args)
            network.assert_not_called()
            self.assertEqual(exit_code, 2)
            summary = json.loads((output / "run_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["schema_version"], 6)
            self.assertEqual(summary["status"], "BOOTSTRAP_FAILED")
            self.assertEqual(summary["issue"]["code"], "LIVE_CONFIG_INVALID_JSON")
            self.assertTrue((output / "errors.jsonl").is_file())
            self.assertTrue((output / "environment.json").is_file())

    def test_packaging_failure_preserves_incomplete_and_consistent_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = LiveTestBundleManager(Path(temp_dir) / "run", run_id="pack-fail")
            with (
                patch.object(
                    manager,
                    "_create_archive",
                    side_effect=OSError("fixture"),
                ),
                self.assertRaises(ConfigurationError) as raised,
            ):
                manager.finalize(
                    status="OK",
                    summary={"schema_version": 4, "status": "OK"},
                )
            self.assertEqual(raised.exception.info.code, "ARCHIVE_CREATE_FAILED")
            self.assertTrue((manager.run_directory / INCOMPLETE_MARKER).is_file())
            self.assertFalse((manager.run_directory / COMPLETE_MARKER).exists())
            summary = json.loads(
                (manager.run_directory / "run_summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["status"], "PACKAGING_FAILED")
            self.assertEqual(summary["issue"]["code"], "ARCHIVE_CREATE_FAILED")
            error_row = json.loads(
                (manager.run_directory / "errors.jsonl").read_text(encoding="utf-8")
            )
            self.assertEqual(error_row["sdk_operation"], "live.package")
            self.assertFalse(error_row["locals_included"])

    def test_bundle_write_failure_is_recoverable_before_archive_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = LiveTestBundleManager(Path(temp_dir) / "run", run_id="write-fail")
            with (
                patch.object(
                    manager,
                    "_write_return_readme",
                    side_effect=OSError("fixture"),
                ),
                self.assertRaises(ConfigurationError) as raised,
            ):
                manager.finalize(status="OK")
            self.assertEqual(raised.exception.info.code, "OUTPUT_WRITE_FAILED")
            self.assertTrue((manager.run_directory / INCOMPLETE_MARKER).is_file())
            self.assertFalse((manager.run_directory / COMPLETE_MARKER).exists())
            summary = json.loads(
                (manager.run_directory / "run_summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["status"], "PACKAGING_FAILED")
            self.assertEqual(summary["issue"]["code"], "OUTPUT_WRITE_FAILED")

    def test_archive_publish_failure_removes_partial_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = LiveTestBundleManager(Path(temp_dir) / "run", run_id="publish-fail")
            real_replace = live_bundle.os.replace

            def fail_archive_publish(source: Path, destination: Path) -> None:
                if Path(destination).suffix == ".zip":
                    raise OSError("fixture")
                return real_replace(source, destination)

            with (
                patch.object(live_bundle.os, "replace", side_effect=fail_archive_publish),
                self.assertRaises(ConfigurationError) as raised,
            ):
                manager.finalize(status="OK")
            self.assertEqual(raised.exception.info.code, "ARCHIVE_CREATE_FAILED")
            self.assertTrue((manager.run_directory / INCOMPLETE_MARKER).is_file())
            self.assertFalse(tuple(manager.archive_directory.glob("*.zip")))
            self.assertFalse(tuple(manager.archive_directory.glob("*.tmp")))

    def test_raw_error_links_to_finished_operation_and_exchange(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = RawCaptureRecorder(Path(temp_dir) / "run", run_id="links")
            recorder.set_live_step("soap")
            operation_id = recorder.start_operation(name="get_soap", app_key="prq")
            request = requests.Request("GET", "https://internal.test/soap").prepare()
            response = requests.Response()
            response.status_code = 200
            response.url = request.url
            response.request = request
            response._content = b"fixture"
            response.headers["Content-Type"] = "text/plain"
            recorder.record_response(
                response=response,
                attempt=1,
                max_attempts=1,
                throttle_delay_seconds=0,
                elapsed_seconds=0.01,
                will_retry=False,
            )
            recorder.finish_operation(operation_id)
            error_id = recorder.record_error(
                error=ParseError("shape drift", code="PRQ_SOAP_CONTAINER_MISSING"),
                step="soap",
            )
            recorder.close()
            error_row = json.loads(
                (Path(temp_dir) / "run" / "errors.jsonl").read_text(encoding="utf-8")
            )
            self.assertEqual(error_row["error_id"], error_id)
            self.assertEqual(error_row["sdk_operation_id"], operation_id)
            self.assertEqual(error_row["sdk_operation"], "get_soap")
            self.assertEqual(error_row["app_key"], "prq")
            self.assertEqual(error_row["linked_capture_id"], "000001")

    def test_environment_and_cp950_console_are_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report = environment_report(Path(temp_dir))
            serialized = json.dumps(report).casefold()
            self.assertNotIn("hostname", serialized)
            self.assertNotIn("username", serialized)
            self.assertTrue(report["output"]["writable"])

        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="cp950", errors="strict")
        with patch.object(sys, "stdout", stream):
            configure_console_output()
            print("VGHKS — 眼科", file=stream)
            stream.flush()
        self.assertTrue(buffer.getvalue())


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
    return modules


if __name__ == "__main__":
    unittest.main()
