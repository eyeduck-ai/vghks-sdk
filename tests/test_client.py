from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace

from vghks_sdk.adapters.auth import AppSession
from vghks_sdk.core.errors import AuthenticationError, AuthExpiredError, RequestError
from vghks_sdk.core.operations import operation_spec
from vghks_sdk.runtime import SDKRuntime


class FakeAuth:
    def __init__(self) -> None:
        self.ensure_calls: list[str] = []
        self.force_login_count = 0

    def ensure(self, app_key: str) -> object:
        self.ensure_calls.append(app_key)
        return object()

    def login(self, *, force: bool = False) -> None:
        if force:
            self.force_login_count += 1


class ContextReauthenticationTests(unittest.TestCase):
    @staticmethod
    def _client(auth: FakeAuth) -> SDKRuntime:
        client = object.__new__(SDKRuntime)
        client.auth = auth  # type: ignore[assignment]
        client.operation_lock = threading.RLock()
        client.diagnostics = None
        client.raw_capture = None
        return client

    def test_expired_operation_relogs_once_then_succeeds(self) -> None:
        auth = FakeAuth()
        client = self._client(auth)
        calls = 0

        def operation() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise AuthExpiredError("synthetic expiry")
            return "ok"

        self.assertEqual(
            client._execute("prq", operation, operation_name="fixture"),
            "ok",
        )
        self.assertEqual(auth.ensure_calls, ["prq", "prq"])
        self.assertEqual(auth.force_login_count, 1)

    def test_sso_auth_status_relogs_once_then_succeeds(self) -> None:
        auth = FakeAuth()
        client = self._client(auth)
        calls = 0

        def operation() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RequestError("fixture auth status", status_code=403)
            return "ok"

        self.assertEqual(
            client._execute("prq", operation, operation_name="fixture"),
            "ok",
        )
        self.assertEqual(auth.ensure_calls, ["prq", "prq"])
        self.assertEqual(auth.force_login_count, 1)

    def test_second_expiry_stops_without_another_login(self) -> None:
        auth = FakeAuth()
        client = self._client(auth)

        def operation() -> str:
            raise AuthExpiredError("synthetic expiry")

        with self.assertRaises(AuthenticationError):
            client._execute("prq", operation, operation_name="fixture")
        self.assertEqual(auth.ensure_calls, ["prq", "prq"])
        self.assertEqual(auth.force_login_count, 1)

    def test_failed_relogin_has_stable_authentication_code(self) -> None:
        class FailedReloginAuth(FakeAuth):
            def login(self, *, force: bool = False) -> None:
                super().login(force=force)
                if force:
                    raise RuntimeError("fixture login failure")

        client = self._client(FailedReloginAuth())

        def operation() -> str:
            raise AuthExpiredError("synthetic expiry")

        with self.assertRaises(AuthenticationError) as caught:
            client._execute("prq", operation, operation_name="fixture")
        self.assertEqual(caught.exception.info.code, "AUTH_RELOGIN_FAILED")
        self.assertEqual(caught.exception.info.operation, "fixture")
        self.assertEqual(caught.exception.info.app, "prq")

    def test_json_http_auth_status_is_promoted_to_session_expiry(self) -> None:
        class ExpiredTransport:
            def request(self, *_: object, **__: object) -> object:
                raise RequestError("fixture status", status_code=401)

        context = object.__new__(SDKRuntime)
        context.transport = ExpiredTransport()  # type: ignore[assignment]
        with self.assertRaises(AuthExpiredError):
            context.request_json(
                operation_spec("prq.visit_cases"),
                "https://example.test/PRQWeb/QueryCaseList.do",
            )

    def test_auth_check_retries_one_expired_sso(self) -> None:
        class ExpiringAuth:
            def __init__(self) -> None:
                self.login_forces: list[bool] = []
                self.ensure_calls = 0
                self.portal_landing_url = "https://portal.example/myPortal.do"

            def login(self, *, force: bool = False) -> None:
                self.login_forces.append(force)

            def check_portal_session(self) -> str:
                return "https://portal.example/sessionCheck.do"

            def ensure(self, app: str) -> AppSession:
                self.ensure_calls += 1
                if self.ensure_calls == 1:
                    raise AuthExpiredError("fixture expired SSO")
                return AppSession(app, "HID", f"https://{app}.example/landing.do")

        auth = ExpiringAuth()
        context = object.__new__(SDKRuntime)
        context.auth = auth  # type: ignore[assignment]
        context.transport = SimpleNamespace(session=SimpleNamespace(cookies={"session": "fixture"}))  # type: ignore[assignment]
        context.operation_lock = threading.RLock()
        context.diagnostics = None
        context.raw_capture = None
        report = context.auth_check(("prq",))
        self.assertEqual(auth.login_forces, [False, True])
        self.assertEqual(auth.ensure_calls, 2)
        self.assertTrue(report.ok)
        self.assertTrue(report.reauthenticated)


if __name__ == "__main__":
    unittest.main()
