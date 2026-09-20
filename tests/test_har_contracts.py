from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from vghks_sdk.cli import main
from vghks_sdk.contracts.check import (
    discover_har_files,
    run_har_check,
    write_har_check_report,
)
from vghks_sdk.contracts.har import (
    BASELINE_CONTRACTS,
    HarEntry,
    HarSignature,
    evaluate_har_contracts,
    load_har,
)
from vghks_sdk.core.errors import ConfigurationError

ROOT = Path(__file__).resolve().parents[1]
TEST_MRN = "00000000"


def _entry(
    *,
    method: str = "POST",
    url: str = "https://internal.test/sessionCheck.do",
    query: list[dict[str, str]] | None = None,
    form: list[dict[str, str]] | None = None,
    body: str | None = "OK",
    encoding: str | None = None,
    status: int = 200,
    mime_type: str = "text/html; charset=utf-8",
) -> dict:
    content: dict[str, object] = {"mimeType": mime_type}
    if body is not None:
        content["text"] = body
    if encoding is not None:
        content["encoding"] = encoding
    return {
        "request": {
            "method": method,
            "url": url,
            "queryString": query or [],
            "postData": {
                "mimeType": "application/x-www-form-urlencoded",
                "params": form if form is not None else [{"name": "userName", "value": TEST_MRN}],
            },
        },
        "response": {"status": status, "content": content},
    }


def _write_har(path: Path, entries: list[dict]) -> None:
    path.write_text(
        json.dumps({"log": {"version": "1.2", "entries": entries}}),
        encoding="utf-8-sig",
    )


class SyntheticHarContractTests(unittest.TestCase):
    def test_semantic_selection_allows_extra_keys_and_base64(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "scenario.har"
            encoded = base64.b64encode(b"OK").decode("ascii")
            _write_har(
                path,
                [
                    _entry(
                        query=[{"name": "newPortalField", "value": "ignored"}],
                        form=[
                            {"name": "userName", "value": TEST_MRN},
                            {"name": "newField", "value": "ignored"},
                        ],
                        body=encoded,
                        encoding="base64",
                    )
                ],
            )
            report, _ = run_har_check(input_path=path, output_path=Path(temp_dir) / "report.json")
            self.assertTrue(report.ok)
            self.assertEqual(report.contracts[0].contract, "portal_session_check")
            self.assertEqual(report.contracts[0].matched_count, 1)

    def test_base64_big5_response_is_decoded_for_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "scenario.har"
            encoded = base64.b64encode("連線正常".encode("cp950")).decode("ascii")
            _write_har(
                path,
                [
                    _entry(
                        body=encoded,
                        encoding="base64",
                        mime_type="text/html; charset=Big5",
                    )
                ],
            )
            archive = load_har(path)
            self.assertEqual(archive.entries[0].text(), "連線正常")
            report = evaluate_har_contracts((archive,), require_baseline=False)
            self.assertTrue(report.ok)

    def test_duplicate_endpoint_validates_every_nonempty_success_response(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "scenario.har"
            _write_har(
                path,
                [
                    _entry(body="OK"),
                    _entry(body='<form><input name="mpassword"></form>'),
                ],
            )
            report, _ = run_har_check(input_path=path, output_path=Path(temp_dir) / "report.json")
            result = report.contracts[0]
            self.assertFalse(report.ok)
            self.assertEqual(result.matched_count, 2)
            self.assertEqual(result.passed_count, 1)
            self.assertEqual(
                result.error_code,
                "HAR_SESSION_RESPONSE_WAS_A_LOGIN_PAGE",
            )

    def test_get_and_post_same_path_are_distinguished(self) -> None:
        entry = HarEntry(
            method="GET",
            path="/PRQWeb/QueryResNumCenter.do",
            query_keys=frozenset({"Use", "caseNo"}),
            form_keys=frozenset(),
            operation_values={"Use": "Case"},
            response_status=200,
            response_body=b"body",
            response_mime_type="text/html",
        )
        get_signature = HarSignature(
            "GET",
            "/PRQWeb/QueryResNumCenter.do",
            frozenset({"Use", "caseNo"}),
            operation_values=(("Use", "Case"),),
        )
        post_signature = HarSignature("POST", "/PRQWeb/QueryResNumCenter.do")
        self.assertTrue(get_signature.matches(entry))
        self.assertFalse(post_signature.matches(entry))

    def test_missing_body_unknown_endpoint_and_malformed_har_are_safe_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            missing = root / "missing.har"
            _write_har(missing, [_entry(body=None)])
            report, _ = run_har_check(input_path=missing, output_path=root / "missing-report.json")
            self.assertEqual(report.contracts[0].error_code, "NO_VALIDATABLE_RESPONSE")

            unknown = root / "unknown.har"
            _write_har(
                unknown,
                [_entry(method="GET", url="https://internal.test/unknown.do")],
            )
            report, _ = run_har_check(input_path=unknown, output_path=root / "unknown-report.json")
            self.assertEqual(report.contracts[0].error_code, "NO_RECOGNIZED_CONTRACT")

            malformed = root / "malformed.har"
            malformed.write_text("{not-json", encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                run_har_check(
                    input_path=malformed,
                    output_path=root / "malformed-report.json",
                )

            with redirect_stdout(io.StringIO()):
                contract_exit = main(
                    [
                        "har-check",
                        "--input",
                        str(missing),
                        "--output",
                        str(root / "cli-contract-report.json"),
                    ]
                )
            with redirect_stderr(io.StringIO()):
                format_exit = main(
                    [
                        "har-check",
                        "--input",
                        str(malformed),
                        "--output",
                        str(root / "cli-format-report.json"),
                    ]
                )
            self.assertEqual(contract_exit, 1)
            self.assertEqual(format_exit, 2)

    def test_safe_report_has_only_whitelisted_fields_and_no_archive_values(self) -> None:
        secret_values = (
            TEST_MRN,
            "SECRET-PASSWORD",
            "SECRET-COOKIE",
            "SECRET-TOKEN",
            "SECRET-RESPONSE",
            "internal.test",
            "scenario.har",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            har_path = root / "scenario.har"
            _write_har(
                har_path,
                [
                    _entry(
                        form=[
                            {"name": "userName", "value": TEST_MRN},
                            {"name": "password", "value": "SECRET-PASSWORD"},
                            {"name": "cookie", "value": "SECRET-COOKIE"},
                            {"name": "token", "value": "SECRET-TOKEN"},
                        ],
                        body="OK SECRET-RESPONSE",
                    )
                ],
            )
            report = evaluate_har_contracts((load_har(har_path),), require_baseline=False)
            destination = root / "report.json"
            destination.write_text("sentinel", encoding="utf-8")
            report_path = write_har_check_report(destination, report)
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(
                set(payload),
                {"schema_version", "status", "archive_count", "entry_count", "contracts"},
            )
            self.assertEqual(
                set(payload["contracts"][0]),
                {
                    "contract",
                    "matched_count",
                    "passed_count",
                    "status",
                    "error_codes",
                },
            )
            serialized = report_path.read_text(encoding="utf-8")
            self.assertNotIn("sentinel", serialized)
            for secret in secret_values:
                self.assertNotIn(secret, serialized)

    def test_directory_scan_is_non_recursive_and_cli_never_builds_sdk(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_har(root / "top.har", [_entry()])
            nested = root / "nested"
            nested.mkdir()
            _write_har(nested / "nested.har", [_entry()])
            with patch("vghks_sdk.contracts.check.Path.cwd", return_value=root):
                files, baseline = discover_har_files(None)
            self.assertTrue(baseline)
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].name, "top.har")

            with (
                patch("vghks_sdk.cli.VghksSDK") as sdk_factory,
                patch("vghks_sdk.cli.SDKSettings.from_env") as settings_factory,
                patch("vghks_sdk.cli._credentials_from_env") as credentials_factory,
                redirect_stdout(io.StringIO()),
            ):
                exit_code = main(
                    [
                        "har-check",
                        "--input",
                        str(root / "top.har"),
                        "--output",
                        str(root / "report.json"),
                    ]
                )
            self.assertEqual(exit_code, 0)
            sdk_factory.assert_not_called()
            settings_factory.assert_not_called()
            credentials_factory.assert_not_called()

    def test_project_uses_central_har_directory_without_scanning_returns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            har = root / "data" / "har"
            har.mkdir(parents=True)
            returns = root / "data" / "returns"
            returns.mkdir()
            _write_har(har / "recording.har", [_entry()])
            _write_har(returns / "unrelated.har", [_entry()])
            with patch("vghks_sdk.contracts.check.Path.cwd", return_value=root):
                files, baseline = discover_har_files(None)
            self.assertTrue(baseline)
            self.assertEqual(files, (har / "recording.har",))
            self.assertEqual(discover_har_files(root)[0], files)


@unittest.skipUnless(
    os.getenv("VGHKS_RUN_HAR_CONTRACT") == "1",
    "sensitive HAR contracts are opt-in",
)
class RealHarContractTests(unittest.TestCase):
    def test_all_current_archives_cover_the_complete_semantic_baseline(self) -> None:
        paths = tuple(
            sorted(
                (
                    path
                    for directory in (ROOT / "data" / "har",)
                    for path in directory.iterdir()
                    if path.suffix.casefold() == ".har"
                ),
                key=lambda path: path.name.casefold(),
            )
        )
        self.assertEqual(len(paths), 18)
        report = evaluate_har_contracts(
            tuple(load_har(path) for path in paths), require_baseline=True
        )
        self.assertTrue(report.ok)
        self.assertEqual(report.archive_count, 18)
        self.assertEqual(len(report.contracts), len(BASELINE_CONTRACTS))
        self.assertTrue(all(result.matched_count > 0 for result in report.contracts))


if __name__ == "__main__":
    unittest.main()
