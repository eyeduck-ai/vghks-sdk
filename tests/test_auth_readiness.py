from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from vghks_sdk.adapters.auth import AppSession
from vghks_sdk.cli import main
from vghks_sdk.core.errors import (
    AuthenticationError,
    AuthExpiredError,
    ConfigurationError,
    ErrorInfo,
)
from vghks_sdk.core.readiness import (
    make_auth_report,
    resolve_auth_targets,
)
from vghks_sdk.local_io import write_auth_check_report
from vghks_sdk.models import AuthCheckTarget
from vghks_sdk.runtime import SDKRuntime


class FakeReadinessAuth:
    def __init__(
        self,
        *,
        failures: dict[str, list[BaseException]] | None = None,
        portal_failures: list[BaseException] | None = None,
    ) -> None:
        self.failures = failures or {}
        self.portal_failures = list(portal_failures or [])
        self.login_forces: list[bool] = []
        self.ensure_calls: list[str] = []
        self.portal_landing_url = "https://portal.example/myPortal.do"

    def login(self, *, force: bool = False) -> None:
        self.login_forces.append(force)

    def check_portal_session(self) -> str:
        if self.portal_failures:
            raise self.portal_failures.pop(0)
        return "https://portal.example/sessionCheck.do?secret=not-in-report"

    def ensure(self, app: str) -> AppSession:
        self.ensure_calls.append(app)
        failures = self.failures.get(app, [])
        if failures:
            raise failures.pop(0)
        return AppSession(
            app,
            "SENSITIVE-HID",
            f"https://{app}.example/landing.do?token=SENSITIVE-TOKEN",
        )


def make_context(auth: FakeReadinessAuth) -> SDKRuntime:
    context = object.__new__(SDKRuntime)
    context.auth = auth  # type: ignore[assignment]
    context.transport = SimpleNamespace(
        session=SimpleNamespace(cookies={"JSESSIONID": "SENSITIVE-COOKIE", "SSO": "SENSITIVE-SSO"})
    )  # type: ignore[assignment]
    context.operation_lock = threading.RLock()
    context.diagnostics = None
    context.raw_capture = None
    return context


class AuthRegistryTests(unittest.TestCase):
    def test_default_registry_order_and_capabilities(self) -> None:
        self.assertEqual(
            [spec.key for spec in resolve_auth_targets()],
            ["portal", "prq", "sectord", "webmaas", "oppl", "audit", "oppl_records", "review", "personnel"],
        )
        self.assertIn("SOAP", resolve_auth_targets()[1].capability)

    def test_only_deduplicates_preserves_order_and_expands_dependencies(self) -> None:
        specs = resolve_auth_targets(("audit", "webmaas", "audit", "oppl"))
        self.assertEqual(
            [spec.key for spec in specs],
            ["portal", "audit", "sectord", "webmaas", "oppl"],
        )

    def test_unknown_target_fails_before_network(self) -> None:
        auth = FakeReadinessAuth()
        context = make_context(auth)
        with self.assertRaises(ConfigurationError):
            context.auth_check(("unknown",))
        self.assertEqual(auth.login_forces, [])
        self.assertEqual(auth.ensure_calls, [])


class AuthSweepTests(unittest.TestCase):
    def test_zero_argument_check_runs_the_full_registry(self) -> None:
        auth = FakeReadinessAuth()
        report = make_context(auth).auth_check()
        self.assertTrue(report.ok)
        self.assertEqual(
            auth.ensure_calls,
            ["prq", "sectord", "webmaas", "oppl", "audit", "oppl_records", "review", "personnel"],
        )
        self.assertEqual([row.status for row in report.targets], ["OK"] * 9)

    def test_portal_failure_blocks_all_children_without_probing_them(self) -> None:
        auth = FakeReadinessAuth(portal_failures=[AuthenticationError("fixture portal failure")])
        report = make_context(auth).auth_check()
        self.assertEqual(report.status, "ERROR")
        self.assertEqual(report.targets[0].status, "ERROR")
        self.assertTrue(all(row.status == "BLOCKED" for row in report.targets[1:]))
        self.assertEqual(auth.ensure_calls, [])

    def test_independent_targets_continue_after_non_auth_failure(self) -> None:
        auth = FakeReadinessAuth(failures={"prq": [AuthenticationError("fixture malformed SSO")]})
        report = make_context(auth).auth_check()
        by_target = {row.target: row for row in report.targets}
        self.assertEqual(report.status, "COMPLETED_WITH_ERRORS")
        self.assertEqual(by_target["prq"].status, "ERROR")
        self.assertEqual(by_target["audit"].status, "OK")
        self.assertEqual(by_target["webmaas"].status, "OK")

    def test_dependency_failure_blocks_bridge_consumers_but_not_audit(self) -> None:
        auth = FakeReadinessAuth(
            failures={"sectord": [AuthenticationError("fixture bridge failure")]}
        )
        report = make_context(auth).auth_check()
        by_target = {row.target: row for row in report.targets}
        self.assertEqual(by_target["sectord"].status, "ERROR")
        self.assertEqual(by_target["webmaas"].status, "BLOCKED")
        self.assertEqual(by_target["oppl"].status, "OK")
        self.assertEqual(by_target["audit"].status, "OK")
        self.assertNotIn("webmaas", auth.ensure_calls)
        self.assertIn("oppl", auth.ensure_calls)

    def test_auth_expiry_discards_sweep_and_restarts_from_portal_once(self) -> None:
        auth = FakeReadinessAuth(failures={"prq": [AuthExpiredError("fixture expired")]})
        report = make_context(auth).auth_check(("prq",))
        self.assertTrue(report.ok)
        self.assertTrue(report.reauthenticated)
        self.assertEqual(auth.login_forces, [False, True])
        self.assertEqual(auth.ensure_calls, ["prq", "prq"])

    def test_second_auth_expiry_stops_remaining_targets(self) -> None:
        auth = FakeReadinessAuth(
            failures={
                "prq": [
                    AuthExpiredError("fixture first expiry"),
                    AuthExpiredError("fixture second expiry"),
                ]
            }
        )
        report = make_context(auth).auth_check()
        by_target = {row.target: row for row in report.targets}
        self.assertEqual(report.status, "COMPLETED_WITH_ERRORS")
        self.assertEqual(by_target["prq"].status, "ERROR")
        self.assertTrue(all(row.status == "BLOCKED" for row in report.targets[2:]))
        self.assertEqual(auth.login_forces, [False, True])
        self.assertEqual(auth.ensure_calls, ["prq", "prq"])


class AuthReportAndCliTests(unittest.TestCase):
    @staticmethod
    def _report(status: str = "OK"):
        target = AuthCheckTarget(
            target="portal",
            capability=("Login", "Session"),
            landing_path="/sessionCheck.do",
            hid_present=False,
            cookie_count=2,
            duration_ms=12.5,
            dependencies=(),
            status=status,
            issue=(None if status == "OK" else ErrorInfo("AUTHENTICATION_ERROR", "AUTHENTICATION")),
        )
        return make_auth_report((target,))

    def test_report_is_atomic_overwritten_and_contains_no_secret_material(self) -> None:
        context = make_context(FakeReadinessAuth())
        report = context.auth_check(("prq",))
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "auth.json"
            path.write_text("old-content", encoding="utf-8")
            write_auth_check_report(path, report)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "OK")
            self.assertEqual(
                set(payload),
                {
                    "schema_version",
                    "sdk_version",
                    "generated_at",
                    "status",
                    "reauthenticated",
                    "targets",
                },
            )
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(
                set(payload["targets"][0]),
                {
                    "target",
                    "capability",
                    "landing_path",
                    "hid_present",
                    "cookie_count",
                    "duration_ms",
                    "dependencies",
                    "status",
                    "issue",
                },
            )
            combined = json.dumps(payload, ensure_ascii=False)
            for secret in (
                "SENSITIVE-HID",
                "SENSITIVE-COOKIE",
                "SENSITIVE-SSO",
                "SENSITIVE-TOKEN",
                "portal.example",
                "prq.example",
                "JSESSIONID",
            ):
                self.assertNotIn(secret, combined)
            self.assertFalse(list(path.parent.glob("*.tmp")))

    def test_cli_exit_codes_matrix_and_only_forwarding(self) -> None:
        environment = {
            "VGHKS_USERNAME": "TEST-USER",
            "VGHKS_PASSWORD": "TEST-PASSWORD",
            "VGHKS_CA_BUNDLE": "",
        }
        for target_status, expected_code in (("OK", 0), ("ERROR", 1)):
            with (
                self.subTest(target_status=target_status),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                fake_sdk = MagicMock()
                fake_sdk.__enter__.return_value = fake_sdk
                fake_sdk.__exit__.return_value = None
                fake_sdk.auth.check.return_value = self._report(target_status)
                output = Path(temp_dir) / "auth.json"
                stdout = io.StringIO()
                with (
                    patch.dict("os.environ", environment, clear=False),
                    patch("vghks_sdk.cli.VghksSDK", return_value=fake_sdk),
                    redirect_stdout(stdout),
                ):
                    code = main(
                        [
                            "auth-check",
                            "--only",
                            "prq,prq",
                            "--output",
                            str(output),
                        ]
                    )
                self.assertEqual(code, expected_code)
                fake_sdk.auth.check.assert_called_once_with(only=("prq", "prq"))
                self.assertIn("Target", stdout.getvalue())
                self.assertTrue(output.is_file())

    def test_missing_credentials_writes_blocked_safe_report_and_returns_two(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "auth.json"
            stderr = io.StringIO()
            with (
                patch.dict(
                    "os.environ",
                    {"VGHKS_USERNAME": "", "VGHKS_USER": "", "VGHKS_PASSWORD": ""},
                    clear=False,
                ),
                redirect_stderr(stderr),
            ):
                code = main(["auth-check", "--output", str(output)])
            self.assertEqual(code, 2)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["targets"][0]["status"], "ERROR")
            self.assertTrue(all(row["status"] == "BLOCKED" for row in payload["targets"][1:]))
            self.assertNotIn("TEST-PASSWORD", output.read_text(encoding="utf-8"))

    def test_cli_unknown_only_target_fails_before_sdk_or_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "auth.json"
            with (
                patch("vghks_sdk.cli.VghksSDK") as sdk_type,
                redirect_stderr(io.StringIO()),
            ):
                code = main(
                    [
                        "auth-check",
                        "--only",
                        "not-a-target",
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(code, 2)
            sdk_type.assert_not_called()
            self.assertTrue(output.is_file())

    def test_legacy_apps_option_is_rejected_by_argparse(self) -> None:
        with self.assertRaises(SystemExit) as raised, redirect_stderr(io.StringIO()):
            main(["auth-check", "--apps", "prq"])
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
