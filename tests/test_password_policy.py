"""Password notices and auth challenges must retain evidence without replaying credentials."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vghks_sdk import ConfigurationError, PasswordChangeRequiredError, PasswordStatus
from vghks_sdk.adapters.auth import AuthenticationAdapter
from vghks_sdk.core.config import PortalCredentials, SDKSettings
from vghks_sdk.live.auth_edge_simulation import AUTH_EDGE_SCENARIOS, run_auth_edge_scenario
from vghks_sdk.live.auth_edges import expected_anonymous_challenge
from vghks_sdk.live.config import LiveTestConfig, resolve_live_test_config
from vghks_sdk.live.login import expected_rejection
from vghks_sdk.models import to_jsonable
from vghks_sdk.offline.replay import replay_response
from vghks_sdk.parsing.portal import parse_login_target, parse_password_status


class PasswordPolicyTests(unittest.TestCase):
    def test_countdown_can_warn_without_blocking_a_successful_login(self):
        for text in (
            "密碼將於 3 天後到期，請更改密碼。",
            "密碼剩餘3天到期",
            "密碼尚餘3日有效",
            "Your password will expire in 3 days",
            "3天內必須變更密碼",
            "密碼將於【3】日後到期，請盡快修改。",
            "密碼將於[3]日後到期",
        ):
            with self.subTest(text=text):
                status = parse_password_status("<p>" + text + "</p>")
                self.assertEqual((status.status, status.remaining_days), ("EXPIRING", 3))
        html = '<p>密碼剩餘3天到期</p><script>targetUrl="myPortal.do";</script>'
        self.assertEqual(parse_login_target(html), "myPortal.do")
        self.assertEqual(parse_password_status("密碼剩餘0天到期").status, "EXPIRING")
        self.assertEqual(parse_password_status("密碼已過期，尚餘3天處理").status, "CHANGE_REQUIRED")

    def test_bracketed_alert_survives_portal_navigation_and_offline_replay(self):
        page = '<script>// alert("密碼已過期");\nalert("密碼將於【4】日後到期，請盡快修改。");var targetUrl="myPortal.do";if(window.name=="example"){location.href=targetUrl;}</script>'
        status = parse_password_status(page, login_stage=True)
        self.assertEqual((status.status, status.remaining_days, status.evidence), ("EXPIRING", 4, "SCRIPT_LITERAL"))
        self.assertEqual(parse_login_target(page), "myPortal.do")
        row = replay_response("portal.login", page.encode(), {}, mime="text/html; charset=utf-8")
        self.assertEqual(row["status"], "CONTRACT_OK")
        self.assertEqual(row["password_status"]["remaining_days"], 4)

    def test_forced_change_requires_explicit_prose_or_login_change_form(self):
        for text in (
            "密碼已到期，必須先變更密碼才能登入",
            "密碼使用期限已到期",
            "需先修改密碼才能登入",
            "Your password has expired",
            "You must change your password",
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_password_status(text).status, "CHANGE_REQUIRED")
                with self.assertRaises(PasswordChangeRequiredError):
                    parse_login_target(text)
        form = '<form><input name="oldPassword" type="password"><input name="newPassword" type="password"></form>'
        self.assertEqual(parse_password_status(form).status, "NO_NOTICE")
        self.assertEqual(parse_password_status(form, login_stage=True).status, "CHANGE_REQUIRED")
        optional_form = form + '<script>targetUrl="myPortal.do";</script>'
        self.assertEqual(parse_password_status(optional_form, login_stage=True).status, "NO_NOTICE")
        warning_with_form = "<p>密碼剩餘3天到期</p>" + optional_form
        self.assertEqual(parse_password_status(warning_with_form, login_stage=True).status, "EXPIRING")
        for wrapper in (
            "<div hidden>{}</div>",
            '<div style="display:none">{}</div>',
            '<div aria-hidden="true">{}</div>',
        ):
            self.assertEqual(
                parse_password_status(wrapper.format(form), login_stage=True).status, "NO_NOTICE"
            )

    def test_static_alerts_only_and_unknown_content_is_not_guessed(self):
        active = '<script>alert("密碼剩餘3天到期"); targetUrl="myPortal.do";</script>'
        status = parse_password_status(active)
        self.assertEqual((status.remaining_days, status.evidence), (3, "SCRIPT_LITERAL"))
        for text in (
            '<script>// alert("密碼已到期");</script>',
            '<script>function later(){alert("密碼已到期");}</script>',
            '<script>if(false){alert("密碼已到期");}</script>',
            '<script>if(false){alert("密碼將於【4】日後到期");}</script>',
            '<script>var example="密碼已到期";</script>',
            '<script type="application/json">alert("密碼已到期")</script>',
            "<button onclick=\"alert('密碼已到期')\">更改密碼</button>",
            "<div hidden>密碼已到期</div>",
            '<style>.example { content: "密碼已到期"; }</style>',
            "密碼問題請洽資訊室",
            "修改密碼",
            "Your password is invalid",
            "如果密碼已到期，請先修改密碼",
            "不需要先更改密碼",
            "If your password has expired, you must change your password",
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_password_status(text).status, "NO_NOTICE")

    def test_password_change_redirect_never_follows_or_submits_a_form(self):
        origin = "https://auth.test.invalid"
        transport = Mock()
        auth = AuthenticationAdapter(
            settings=SDKSettings(portal_base_url=origin),
            credentials=PortalCredentials("TST1", "synthetic"),
            transport=transport,
        )
        response = SimpleNamespace(
            status_code=302, url=origin + "/login.do", headers={"Location": "/changePassword.do"}
        )
        with self.assertRaises(PasswordChangeRequiredError):
            auth._follow_login_redirects(response, password_post=True)
        transport.request.assert_not_called()
        self.assertEqual(auth.password_status.status, "CHANGE_REQUIRED")

    def test_no_socket_scenarios_keep_unauthenticated_expired_and_rejected_separate(self):
        with patch("socket.socket", side_effect=AssertionError("simulation opened a socket")):
            rows = [run_auth_edge_scenario(name) for name in AUTH_EDGE_SCENARIOS]
        self.assertTrue(all(row["passed"] for row in rows), rows)
        by_name = {row["scenario"]: row for row in rows}
        self.assertEqual(by_name["unauthenticated_form"]["password_posts"], 0)
        self.assertEqual(by_name["expired_form"]["error_type"], "AuthExpiredError")
        self.assertTrue(by_name["expired_form"]["recovered"])
        self.assertEqual(by_name["password_required_visible"]["password_posts"], 1)
        self.assertFalse(by_name["password_required_visible"]["error"]["retry_safe"])

    def test_replay_uses_password_policy_parser_and_retains_unknown_errors(self):
        for operation, body in (
            ("portal.login", "密碼已到期，必須先更改密碼"),
            ("portal.login", '<script>targetUrl="changePassword.do";</script>'),
            ("prq.upload_types", "密碼已到期，必須先更改密碼"),
        ):
            row = replay_response(operation, body.encode(), {}, mime="text/html; charset=utf-8")
            self.assertEqual(row["error_code"], "PORTAL_PASSWORD_CHANGE_REQUIRED")
        row = replay_response("portal.login", b"<p>unknown policy</p>", {})
        self.assertEqual(row["error_code"], "PORTAL_LOGIN_TARGET_MISSING")

    def test_negative_limits_and_evidence_are_strict(self):
        self.assertEqual(
            resolve_live_test_config(
                cli_values={"profile": "failures"}, environ={}
            ).login_negative_attempts,
            1,
        )
        self.assertEqual(
            LiveTestConfig(profile="failures", login_negative_attempts=0).login_negative_attempts, 0
        )
        with self.assertRaises(ConfigurationError):
            LiveTestConfig(profile="failures", login_negative_attempts=2)
        step = {
            "name": "failures.live.unauthenticated",
            "operation": "prq.upload_types",
            "status": "OK",
            "details": {
                "evidence": "LIVE_UNAUTHENTICATED",
                "prior_login": False,
                "expected_challenge": True,
                "observed_code": "AUTH_NOT_AUTHENTICATED",
                "password_posts": 0,
                "blocked_posts": 0,
            },
        }
        self.assertTrue(expected_anonymous_challenge(step))
        for key, value in (
            ("prior_login", True),
            ("password_posts", 1),
            ("observed_code", "HTTP_503"),
            ("expected_challenge", False),
        ):
            changed = {**step, "details": {**step["details"], key: value}}
            self.assertFalse(expected_anonymous_challenge(changed))
        negative = {
            "name": "failures.live.negative_before_login",
            "operation": "portal.login",
            "status": "OK",
            "details": {
                "evidence": "LIVE",
                "expected_failure": True,
                "observed_code": "PORTAL_LOGIN_REJECTED",
                "password_posts": 1,
                "blocked_posts": 0,
                "position": "BEFORE_CORRECT_LOGIN",
            },
        }
        self.assertTrue(expected_rejection(negative))
        negative["details"]["position"] = "AFTER_LOGIN"
        self.assertFalse(expected_rejection(negative))

    def test_public_notice_carries_no_raw_messages_or_credentials(self):
        self.assertEqual(
            to_jsonable(PasswordStatus("EXPIRING", 3, "VISIBLE_TEXT")),
            {"status": "EXPIRING", "remaining_days": 3, "evidence": "VISIBLE_TEXT"},
        )
        for kwargs in (
            {"status": "ACCOUNT_LOCKED"},
            {"remaining_days": -1},
            {"evidence": "raw password/account message"},
        ):
            with self.assertRaises(ValueError):
                PasswordStatus(**kwargs)


if __name__ == "__main__":
    unittest.main()
