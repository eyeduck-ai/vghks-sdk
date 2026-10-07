"""Bounded old-password observations must never mutate or replay credentials."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from vghks_sdk import (
    AuthenticationError,
    ConfigurationError,
    PortalCredentials,
    RequestError,
    RequestPolicy,
    SDKSettings,
    VghksSDK,
)
from vghks_sdk.contracts.auth_evidence import expected_anonymous_challenge
from vghks_sdk.core.transport import SafeSessionTransport
from vghks_sdk.live.config import LiveTestConfig
from vghks_sdk.live.password import _read_change_page, build_password_plan, run_password_test
from vghks_sdk.offline.analyze import _no_sample_steps, _step_query
from vghks_sdk.offline.password import password_test_summary
from vghks_sdk.parsing.portal import find_password_change_target

PORTAL = "https://portal.synthetic.invalid"


class PasswordProfileTests(unittest.TestCase):
    def test_trusted_literal_or_redirect_only(self):
        for text, location in (
            ('<script>targetUrl="changePassword.do";</script>', ""),
            ('<script>window.location.href="/changePwd.do";</script>', ""),
            ("", "/modifyPassword.do"),
        ):
            with self.subTest(text=text, location=location):
                self.assertIsNotNone(find_password_change_target(text, PORTAL + "/login.do", PORTAL,
                                                                location=location))
        for text, location in (
            ('<form action="/changePassword.do"></form>', ""),
            ('<button onclick="location.href=\'/changePassword.do\'">Change</button>', ""),
            ('<script>function callback(){targetUrl="changePassword.do";}</script>', ""),
            ('<script>// targetUrl="changePassword.do";\n</script>', ""),
            ('<script>targetUrl="changePassword.do";location.href="myPortal.do";</script>', ""),
            ("", "https://foreign.invalid/changePassword.do"),
            ("", "/password-save.do"),
            ("", "http://portal.synthetic.invalid/changePassword.do"),
        ):
            with self.subTest(text=text, location=location):
                self.assertIsNone(find_password_change_target(text, PORTAL + "/login.do", PORTAL,
                                                             location=location))
        self.assertIsNone(find_password_change_target("", "https://foreign.invalid/login.do", PORTAL,
                                                     location=PORTAL + "/changePassword.do"))

    def test_profile_is_bounded_and_refuses_other_test_flows(self):
        config = LiveTestConfig(profile="password", test_mrn="00000000", max_cases=8, max_items=8)
        self.assertEqual((config.login_negative_attempts, config.max_cases, config.max_items), (0, 1, 1))
        self.assertFalse(config.download_assets)
        self.assertFalse(config.weekly_opd_soap)
        plan = build_password_plan(config)
        self.assertEqual((plan["password_post_limit"], plan["change_password_submissions"]), (1, 0))
        for kwargs in (
            {"login_negative_attempts": 1}, {"session_pause": True}, {"include_earnings": True},
            {"include_surgery": True}, {"include_unsigned": True},
            {"only_operations": ("prq.upload_types",)},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ConfigurationError):
                LiveTestConfig(profile="password", **kwargs)

    def test_page_get_does_not_retry_or_change_the_shared_policy(self):
        session = SimpleNamespace(headers={}, request=Mock(side_effect=requests.ConnectTimeout("synthetic")))
        transport = SafeSessionTransport(session=session, policy=RequestPolicy(
            min_delay_seconds=0, max_delay_seconds=0, max_attempts=3), sleeper=lambda _: None)
        sdk = SimpleNamespace(_runtime=SimpleNamespace(transport=transport, operation_lock=threading.RLock()))
        with self.assertRaises(RequestError):
            _read_change_page(sdk, PORTAL + "/changePassword.do")
        self.assertEqual(session.request.call_count, 1)
        self.assertEqual(transport.policy.max_attempts, 3)
        self.assertFalse(session.request.call_args.kwargs["allow_redirects"])
        self.assertEqual(session.request.call_args.args[0], "GET")

    def test_expected_anonymous_evidence_still_requires_zero_password_posts(self):
        step = {"name": "password.unauthenticated_catalog", "operation": "prq.upload_types", "status": "OK",
                "details": {"evidence": "LIVE_UNAUTHENTICATED", "prior_login": False,
                            "expected_challenge": True, "observed_code": "AUTH_NOT_AUTHENTICATED",
                            "password_posts": 0, "blocked_posts": 0}}
        self.assertTrue(expected_anonymous_challenge(step))
        self.assertEqual(_step_query(step), "")
        self.assertEqual(_step_query({"name": "password.existing_cookie_catalog", "operation": "prq.upload_types"}), "")
        self.assertEqual(_step_query({"name": "password.sdk_catalog", "operation": "prq.upload_types", "status": "EMPTY"}), "prq.upload_types")
        step["details"]["password_posts"] = 1
        self.assertFalse(expected_anonymous_challenge(step))

    def test_extra_page_observation_does_not_replace_original_sdk_notice(self):
        with (tempfile.TemporaryDirectory() as directory,
              patch("socket.socket", side_effect=AssertionError("no network")),
              VghksSDK(settings=SDKSettings(), credentials=PortalCredentials("SYNTHETIC", "SYNTHETIC-ONLY")) as sdk):
            page = {"status": "CAPTURED", "captured": True,
                    "password_status": {"status": "CHANGE_REQUIRED", "remaining_days": None,
                                        "evidence": "VISIBLE_TEXT"}}
            with (patch.object(sdk.auth, "login", side_effect=AuthenticationError("synthetic", code="PORTAL_LOGIN_TARGET_MISSING")),
                  patch("vghks_sdk.live.password._watch_change_destination", return_value=nullcontext({"target": PORTAL + "/changePassword.do"})),
                  patch("vghks_sdk.live.password._read_change_page", return_value=page),
                  patch("vghks_sdk.live.password._catalog_without_login", return_value=[])):
                result = run_password_test(sdk, LiveTestConfig(profile="password"), output_dir=Path(directory))
            summary = json.loads(result.summary_path.read_text(encoding="utf-8"))["password_change_test"]
            self.assertEqual(summary["password_status"]["status"], "NO_NOTICE")
            self.assertEqual(summary["change_page"]["password_status"]["status"], "CHANGE_REQUIRED")
            self.assertEqual(summary["login_error_code"], "PORTAL_LOGIN_TARGET_MISSING")
            self.assertEqual(summary["credential_validity"], "UNKNOWN")
            self.assertEqual(summary["patient_read_status"], "BLOCKED")

    def test_offline_summary_drops_raw_values_and_handles_malformed_fields(self):
        source = {
            "login_status": "ERROR", "login_error_code": "PORTAL_PASSWORD_CHANGE_REQUIRED",
            "credential_validity": "UNKNOWN", "password_status": {
                "status": "CHANGE_REQUIRED", "evidence": "VISIBLE_TEXT"},
            "change_page": {"status": "CAPTURED", "captured": True, "url": "SECRET",
                            "password_status": {"status": "CHANGE_REQUIRED", "evidence": "LOGIN_CHANGE_FORM"}},
            "anonymous_catalog": {"query_accepted": True, "cookie": "SECRET"},
            "existing_cookie_catalog": {"status": "OK", "query_accepted": True, "body": "SECRET"},
            "password_post_budget": {"password_posts": 1, "blocked_posts": 0, "password": "SECRET"},
            "password": "SECRET", "scope": "SECRET",
        }
        reader = SimpleNamespace(names={"parsed/password/summary.json"}, json=lambda _: source)
        result = password_test_summary(reader)
        self.assertEqual(result["credential_validity"], "UNKNOWN")
        self.assertTrue(result["existing_cookie_catalog"]["query_accepted"])
        self.assertNotIn("SECRET", json.dumps(result))
        for field in source:
            malformed = dict(source, **{field: ["SECRET"]})
            reader.json = lambda _, value=malformed: value
            self.assertNotIn("SECRET", json.dumps(password_test_summary(reader)), field)
        reader.json = lambda _: ["SECRET"]
        self.assertEqual(password_test_summary(reader)["login_status"], "UNKNOWN")
        gap = _no_sample_steps([{"name": "password.change_page", "status": "NO_SAMPLE",
                                "details": {"reason": ["SECRET"]}}])[0]
        self.assertEqual(gap["reason_code"], "")


if __name__ == "__main__":
    unittest.main()
