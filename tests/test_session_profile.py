from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import requests

from vghks_sdk.core.config import SDKSettings
from vghks_sdk.core.errors import ConfigurationError, ErrorInfo
from vghks_sdk.core.readiness import make_auth_report
from vghks_sdk.live.atomic import build_test_plan
from vghks_sdk.live.config import LiveTestConfig, resolve_live_test_config
from vghks_sdk.live.session import clear_webmaas_cookies, run_session_test
from vghks_sdk.models import AuthCheckTarget


def report(code="", *, reauthenticated=False):
    fields = {"capability": (), "landing_path": "", "hid_present": False,
              "cookie_count": 0, "duration_ms": 0, "dependencies": ()}
    targets = [
        AuthCheckTarget(target="portal", status="OK", **fields),
        AuthCheckTarget(target="sectord", status="OK", **fields),
        AuthCheckTarget(target="webmaas", status="ERROR" if code else "OK",
                        issue=ErrorInfo(code, "PARSE", app="webmaas") if code else None, **fields),
    ]
    return make_auth_report(targets, reauthenticated=reauthenticated)


def fake_sdk(reports=()):
    jar = requests.cookies.RequestsCookieJar()
    jar.set("JSESSIONID", "PORTAL-SECRET", domain="synthetic.test", path="/")
    jar.set("JSESSIONID", "WEBMAAS-SECRET", domain="synthetic.test", path="/webmaas")
    settings = SDKSettings(
        portal_base_url="https://synthetic.test",
        sectord_base_url="https://synthetic.test/SectOrdWeb",
        webmaas_base_url="https://synthetic.test/webmaas",
    )
    return SimpleNamespace(
        _runtime=SimpleNamespace(settings=settings, transport=SimpleNamespace(session=SimpleNamespace(cookies=jar))),
        auth=SimpleNamespace(check=MagicMock(side_effect=list(reports))),
        patients=SimpleNamespace(get_demographics=MagicMock(return_value={"synthetic": "identity"}),
                                 get_basic_info=MagicMock(return_value={"synthetic": "details"})),
    )


class SessionProfileTests(unittest.TestCase):
    def test_defaults_and_plan_are_bounded_and_contain_no_wrong_passwords(self):
        config = resolve_live_test_config(cli_values={"profile": "session"}, environ={})
        plan = build_test_plan(config)
        self.assertEqual(config.login_negative_attempts, 0)
        self.assertFalse(config.download_assets)
        self.assertFalse(config.session_pause)
        self.assertEqual(plan["max_patients"], 1)
        self.assertEqual(plan["auth_targets"], ["portal", "sectord", "webmaas"])
        self.assertEqual([row["key"] for row in plan["operations"]],
                         ["webmaas.demographics", "webmaas.basic_info"])
        self.assertEqual(config.to_safe_dict()["session_pause"], False)
        for values in ({"login_negative_attempts": 1}, {"session_pause": "yes"},
                       {"include_surgery": True}, {"include_earnings": True}):
            with self.subTest(values=values), self.assertRaises(ConfigurationError):
                LiveTestConfig(profile="session", **values)
        with self.assertRaises(ConfigurationError):
            LiveTestConfig(profile="failures", session_pause=True)

    def test_cookie_loss_keeps_shared_portal_and_unrelated_cookies(self):
        sdk = fake_sdk()
        jar = sdk._runtime.transport.session.cookies
        jar.set("JSESSIONID", "UNRELATED", domain="other.test", path="/webmaas")
        jar.set("OTHER", "UNRELATED", domain="synthetic.test", path="/webmaas")
        result = clear_webmaas_cookies(sdk)
        self.assertEqual(result["removed_count"], 1)
        self.assertEqual(len(jar), 3)
        self.assertEqual(jar.get("JSESSIONID", domain="synthetic.test", path="/"), "PORTAL-SECRET")
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual(clear_webmaas_cookies(sdk)["removed_count"], 0)

    def test_missing_cookie_sample_does_not_clear_the_portal_or_claim_expiry(self):
        sdk = fake_sdk((report(),))
        sdk._runtime.transport.session.cookies.clear("synthetic.test", "/webmaas", "JSESSIONID")
        with tempfile.TemporaryDirectory() as directory:
            result = run_session_test(sdk, LiveTestConfig(profile="session"), output_dir=Path(directory))
            summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
        self.assertEqual(result.status, "COMPLETED_WITH_GAPS")
        self.assertEqual(sdk.auth.check.call_count, 1)
        self.assertFalse(summary["session_test"]["challenge"]["performed"])
        self.assertEqual(summary["session_test"]["natural_ttl"]["status"], "NOT_TESTED")

    def test_missing_form_preserves_failure_and_rechecks_sso_once(self):
        sdk = fake_sdk((report(), report("WEBMAAS_QUERY_FORM_MISSING"), report()))
        with tempfile.TemporaryDirectory() as directory:
            result = run_session_test(sdk, LiveTestConfig(profile="session"), output_dir=Path(directory))
            summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
        self.assertEqual(sdk.auth.check.call_count, 3)
        self.assertEqual(sdk.patients.get_demographics.call_count, 2)
        self.assertEqual(result.status, "COMPLETED_WITH_ERRORS")
        self.assertEqual(result.steps[4].issue.code, "WEBMAAS_QUERY_FORM_MISSING")
        observation = summary["session_test"]["observations"][-1]
        self.assertTrue(observation["sso_recheck_succeeded"])
        self.assertFalse(summary["session_test"]["natural_ttl"]["verified"])

    def test_persistent_failure_stops_before_patient_posts(self):
        sdk = fake_sdk((report(), report("WEBMAAS_QUERY_TOKEN_MISSING"), report("WEBMAAS_QUERY_TOKEN_MISSING")))
        with tempfile.TemporaryDirectory() as directory:
            result = run_session_test(sdk, LiveTestConfig(profile="session"), output_dir=Path(directory))
        self.assertEqual(sdk.auth.check.call_count, 3)
        self.assertEqual(sdk.patients.get_basic_info.call_count, 1)
        self.assertEqual([step.status for step in result.steps[-2:]], ["BLOCKED", "BLOCKED"])

    def test_failed_runtime_reauthentication_does_not_receive_an_outer_retry(self):
        sdk = fake_sdk((report(), report("AUTH_LOGIN_REJECTED", reauthenticated=True)))
        with tempfile.TemporaryDirectory() as directory:
            result = run_session_test(sdk, LiveTestConfig(profile="session"), output_dir=Path(directory))
        self.assertEqual(sdk.auth.check.call_count, 2)
        self.assertEqual(result.status, "COMPLETED_WITH_ERRORS")

    def test_manual_idle_uses_the_same_sdk_and_never_clears_cookies(self):
        sdk = fake_sdk((report(), report()))
        def resume():
            self.assertEqual(sdk.auth.check.call_count, 1)
            self.assertEqual(len(sdk._runtime.transport.session.cookies), 2)
        with tempfile.TemporaryDirectory() as directory:
            result = run_session_test(sdk, LiveTestConfig(profile="session", session_pause=True),
                                      output_dir=Path(directory), resume=resume)
            summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
        self.assertEqual(result.status, "OK")
        self.assertEqual(len(sdk._runtime.transport.session.cookies), 2)
        self.assertFalse(summary["session_test"]["challenge"]["cookies_cleared"])
        self.assertEqual(summary["session_test"]["natural_ttl"],
                         {"status": "IDLE_OBSERVATION", "verified": False})


if __name__ == "__main__":
    unittest.main()
