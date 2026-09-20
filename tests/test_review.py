from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from html import escape
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

import requests
from test_webmaas import adapter_with, response

from vghks_sdk import PortalCredentials, RequestPolicy, ReviewCaseFilter, ReviewCaseRef, SDKSettings
from vghks_sdk.adapters.auth import AuthenticationAdapter
from vghks_sdk.adapters.review import ReviewAdapter
from vghks_sdk.adapters.review_auth import ReviewOAuth
from vghks_sdk.core.errors import AuthenticationError, ConfigurationError, ParseError
from vghks_sdk.core.operations import operation_spec
from vghks_sdk.core.readiness import make_auth_report, resolve_auth_targets
from vghks_sdk.core.transport import SafeSessionTransport
from vghks_sdk.live.atomic import _classify, _query_inputs, build_test_plan, run_atomic_test
from vghks_sdk.live.config import LiveTestConfig, resolve_live_test_config
from vghks_sdk.live.presets import combined_round
from vghks_sdk.models import AuthCheckTarget, to_jsonable
from vghks_sdk.offline.replay import identify_operation, replay_hars, replay_response
from vghks_sdk.parsing.review import (
    parse_login_info,
    parse_review_case,
    parse_review_cases,
    parse_review_options,
    parse_review_part,
)
from vghks_sdk.queries import QUERY_SPECS, query_spec

ROOT = Path(__file__).resolve().parents[1]
REF = ReviewCaseRef("000000000000001")
REVIEW_KEYS = tuple(spec.key for spec in QUERY_SPECS if spec.app == "review")
LOGIN_INFO = {"UserNMC": "Synthetic user", "LoginDateTime": "2026-09-20", "LastLoginDateTime": ""}


def case_row(seq=REF.apply_seq, decision="1"):
    return {
        "ApplySeq": seq,
        "ApplyDate": "20260920",
        "PatNo": "SYNTHETIC",
        "PatName": "Synthetic patient",
        "VSDrID": "SYNTHETIC",
        "InsuSectNo": "OPH",
        "VerifyCode": decision,
        "ApplyStatus": "Y",
        "ApplyFinishFlag": "F",
        "FutureField": {"preserve": True},
        "ErrorMessage": None,
    }


def grid(*rows):
    return {"Data": list(rows), "Total": len(rows), "AggregateResults": None, "Errors": None}


def options():
    return {
        "InsuSectNo": [{"Text": "Synthetic department", "Value": "OPH", "Disabled": False}],
        "VSDrID": [{"Text": "Synthetic doctor", "Value": "SYNTHETIC"}],
        "VerifyCode": [{"Text": "同意備查", "Value": "1"}],
    }


class OAuthFixture:
    """A requests session double: any unrecognized endpoint fails immediately."""

    def __init__(
        self,
        fallback=False,
        *,
        callback_state=None,
        foreign_redirect=False,
        missing_cookie=False,
        missing_identity=False,
        bad_form=False,
        post_redirect=302,
        json_entry=False,
    ):
        self.settings = SDKSettings()
        self.portal = self.settings.portal_base_url
        self.base = self.settings.review_base_url
        self.authorization = {
            "response_type": "code",
            "client_id": "pckoauth",
            "redirect_uri": self.base.replace("https://", "http://") + "/HISLogin/SSOLoginCallBack",
            "state": "fresh-state",
        }
        self.login_fields = {
            **self.authorization,
            "code": "fresh-form-code",
            "oauthServer": self.portal,
        }
        self.callback_fields = {
            "code": "fresh-exchange-code",
            "oauthServer": self.portal,
            "redirect_uri": self.authorization["redirect_uri"],
        }
        if callback_state is not None:
            self.callback_fields["state"] = callback_state
        self.callback_url = (
            self.authorization["redirect_uri"] + "?" + urlencode(self.callback_fields)
        )
        self.fallback, self.foreign_redirect = fallback, foreign_redirect
        self.missing_cookie, self.missing_identity = missing_cookie, missing_identity
        self.bad_form, self.post_redirect, self.json_entry = bad_form, post_redirect, json_entry
        self.session = requests.Session()
        self.session.request = MagicMock(side_effect=self.dispatch)
        self.transport = SafeSessionTransport(
            policy=RequestPolicy(min_delay_seconds=0, max_delay_seconds=0, max_attempts=1),
            session=self.session,
        )
        self.auth = AuthenticationAdapter(
            settings=self.settings,
            credentials=PortalCredentials("SYNTHETIC", "SYNTHETIC-PASSWORD"),
            transport=self.transport,
        )
        self.auth._portal_authenticated = True

    def dispatch(self, method, url, **kwargs):
        assert url.startswith("https://"), "No cleartext callback request is permitted"
        assert kwargs["allow_redirects"] is False
        path = urlsplit(url).path
        result = response("")
        result.url = url
        authorization_url = self.portal + "/oauth2Server.do?" + urlencode(self.authorization)
        if path in {"/Pck/HISLogin", "/Pck/HISLogin/SSOLogin"}:
            if self.json_entry:
                result._content = json.dumps(authorization_url).encode()
            else:
                result.status_code = 302
                result.headers["Location"] = authorization_url
        elif path == "/oauth2Server.do":
            if self.foreign_redirect:
                result.status_code = 302
                result.headers["Location"] = "https://foreign.invalid/oauth2ServerLogin.do"
            elif self.fallback:
                hidden = "".join(
                    f'<input type="hidden" name="{k}" value="{escape(v, quote=True)}">'
                    for k, v in self.login_fields.items()
                )
                html = (
                    '<form method="post" action="/oauth2ServerLogin.do">'
                    + hidden
                    + '<input name="muid"><input type="password" name="mpassword">'
                    + '<input name="submit" type="submit" value="登入"></form>'
                )
                result._content = ("<html>unknown page</html>" if self.bad_form else html).encode()
            else:
                result.status_code = 302
                result.headers["Location"] = self.callback_url
        elif path == "/oauth2ServerLogin.do":
            assert method == "POST"
            assert kwargs["data"] == {
                **self.login_fields,
                "muid": "SYNTHETIC",
                "mpassword": "SYNTHETIC-PASSWORD",
                "submit": "登入",
            }
            result.status_code = self.post_redirect
            result.headers["Location"] = self.callback_url
        elif path == "/Pck/HISLogin/SSOLoginCallBack":
            assert parse_qs(urlsplit(url).query)["code"] == ["fresh-exchange-code"]
            if not self.missing_cookie:
                self.session.cookies.set(
                    "HIS_IPD", "fresh-review-session", domain="pck01p.vghks.gov.tw", path="/Pck"
                )
            result.status_code = 302
            result.headers["Location"] = "/Pck/angular/bulletin"
        elif path == "/Pck/angular/bulletin":
            result._content = b"<html>synthetic bulletin</html>"
        elif path == "/Pck/Menu/GetLoginInfo":
            result._content = json.dumps({} if self.missing_identity else LOGIN_INFO).encode()
        else:
            raise AssertionError(f"Unexpected route: {path}")
        return result


class ReviewParserTests(unittest.TestCase):
    def test_filter_dates_normalization_and_reference_keep_leading_zeros(self):
        selector = ReviewCaseFilter(
            doctor_card=" doc ", start_date=date(2026, 9, 1), end_date=date(2026, 9, 20)
        )
        self.assertEqual(
            selector.to_form(),
            {"VSDrID": "DOC", "ApplyDateS": "20260901", "ApplyDateE": "20260920"},
        )
        self.assertEqual(REF.apply_seq, "000000000000001")
        for fields in (
            {},
            {"doctor_card": "x&y"},
            {"mrn": None},
            {"start_date": "2026-09-20"},
            {"start_date": date(2026, 9, 20), "end_date": date(2026, 9, 19)},
        ):
            with self.subTest(fields=fields), self.assertRaises(ConfigurationError):
                ReviewCaseFilter(**fields)
        for seq in (1, "", "１２３", "x", "1" * 33):
            with self.subTest(seq=seq), self.assertRaises(ConfigurationError):
                ReviewCaseRef(seq)

    def test_upload_and_application_completion_never_mean_approved(self):
        for code, approved, label in (
            ("1", True, "同意備查"),
            ("2", False, "不予同意"),
            ("3", None, "部分同意"),
            ("0", None, "審查中"),
            ("new", None, "未辨識"),
        ):
            case = parse_review_case(case_row(decision=code))
            self.assertIs(case.approved, approved)
            self.assertEqual(case.review_label, label)
            self.assertEqual((case.application_status, case.processing_status), ("Y", "F"))
            self.assertTrue(case.fields["FutureField"]["preserve"])
            self.assertEqual(to_jsonable(case)["review_label"], label)
            self.assertIn("approved", to_jsonable(case))

    def test_grid_incomplete_backend_error_duplicates_and_identity_are_not_empty(self):
        bad = (
            {},
            {"Data": [], "Total": 3},
            {"Data": [], "Total": True},
            {**grid(), "Errors": {"failure": "synthetic"}},
            grid(case_row(), case_row()),
            grid({**case_row(), "ErrorMessage": "synthetic"}),
            grid({"ApplySeq": 123}),
        )
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(ParseError):
                parse_review_cases(payload)
        for parse in (
            lambda: parse_review_case(case_row("2"), REF),
            lambda: parse_review_part(grid(case_row("2")), REF, "orders"),
        ):
            with self.assertRaises(ParseError) as caught:
                parse()
            self.assertEqual(caught.exception.info.code, "REVIEW_IDENTITY_MISMATCH")
        self.assertEqual(parse_review_cases(grid()), [])
        empty = parse_review_part(grid(), REF, "pacs")
        self.assertEqual((empty.rows, empty.total, _classify(empty)), ((), 0, "EMPTY"))

    def test_login_shells_and_malformed_options_fail_instead_of_succeeding(self):
        self.assertEqual(parse_login_info(LOGIN_INFO), LOGIN_INFO)
        self.assertEqual(parse_review_options(options()), options())
        for parser, value in (
            (parse_login_info, {}),
            (parse_login_info, ""),
            (parse_review_options, {}),
            (parse_review_options, {**options(), "VSDrID": [{"Text": "missing value"}]}),
        ):
            with self.subTest(parser=parser.__name__), self.assertRaises(ParseError):
                parser(value)

    def test_partial_filter_requests_are_identified_and_offline_parser_preserves_counts(self):
        for fields in ("VSDrID=SYNTHETIC", "PatNo=SYNTHETIC", "InsuSectNo=OPH&VSDrID=SYNTHETIC"):
            key = identify_operation(
                {
                    "method": "POST",
                    "url": "https://example.invalid/Pck/PCKQ010/PckQ010Grid_Read",
                    "body": {"text": fields},
                }
            )
            self.assertEqual(key, "review.cases")
            result = replay_response(key, json.dumps(grid(case_row())).encode(), {})
            self.assertEqual((result["status"], result["record_count"]), ("PARSED", 1))


class ReviewOAuthTests(unittest.TestCase):
    def test_existing_portal_session_and_fresh_credentials_share_the_same_session(self):
        for fallback in (False, True):
            with self.subTest(fallback=fallback):
                fixture = OAuthFixture(fallback)
                result = fixture.auth.ensure("review")
                self.assertEqual(
                    result.authentication_mode,
                    "portal_credentials" if fallback else "portal_session",
                )
                count = fixture.session.request.call_count
                self.assertIs(fixture.auth.ensure("review"), result)
                self.assertEqual(fixture.session.request.call_count, count)
                posts = [c for c in fixture.session.request.call_args_list if c.args[0] == "POST"]
                self.assertEqual(len(posts), int(fallback))
                self.assertTrue(
                    all(
                        c.args[1].startswith("https://")
                        for c in fixture.session.request.call_args_list
                    )
                )
                if posts:
                    self.assertEqual(
                        posts[0].kwargs["data"]["redirect_uri"],
                        fixture.authorization["redirect_uri"],
                    )

    def test_json_authorization_url_and_present_state_are_supported(self):
        fixture = OAuthFixture(json_entry=True, callback_state="fresh-state")
        self.assertEqual(fixture.auth.ensure("review").authentication_mode, "portal_session")

    def test_foreign_redirect_wrong_state_unknown_form_and_missing_cookie_fail_closed(self):
        for parameters, code in (
            ({"foreign_redirect": True}, "REVIEW_OAUTH_TARGET_INVALID"),
            ({"callback_state": "wrong"}, "REVIEW_OAUTH_STATE_MISMATCH"),
            ({"fallback": True, "bad_form": True}, "REVIEW_OAUTH_FORM_UNRECORDED"),
            ({"missing_cookie": True}, "REVIEW_LOGIN_COOKIE_MISSING"),
            ({"fallback": True, "post_redirect": 307}, "REVIEW_OAUTH_POST_REDIRECT_UNSUPPORTED"),
        ):
            fixture = OAuthFixture(**parameters)
            with self.subTest(code=code), self.assertRaises(AuthenticationError) as caught:
                fixture.auth.ensure("review")
            self.assertEqual(caught.exception.info.code, code)
            self.assertNotIn("review", fixture.auth._apps)
            self.assertFalse(
                any("foreign.invalid" in c.args[1] for c in fixture.session.request.call_args_list)
            )
        with self.assertRaises(ParseError):
            OAuthFixture(missing_identity=True).auth.ensure("review")

    def test_changed_hidden_context_or_missing_nonce_never_submits_password(self):
        for key, value in (
            ("state", "wrong"),
            ("client_id", "other"),
            ("code", ""),
            ("oauthServer", "https://foreign.invalid"),
        ):
            fixture = OAuthFixture(fallback=True)
            fixture.login_fields[key] = value
            with self.subTest(key=key), self.assertRaises(AuthenticationError):
                fixture.auth.ensure("review")
            self.assertFalse(
                any(c.args[0] == "POST" for c in fixture.session.request.call_args_list)
            )

    def test_initial_and_callback_url_validation(self):
        oauth = ReviewOAuth(OAuthFixture().auth)
        for url in (
            "http://portal.vghks.gov.tw/oauth2Server.do",
            "https://user@portal.vghks.gov.tw/oauth2Server.do",
            "https://pck01p.vghks.gov.tw/Pck/Write",
            "https://foreign.invalid/Pck/HISLogin/SSOLoginCallBack",
        ):
            with self.subTest(url=url), self.assertRaises(AuthenticationError):
                oauth._target(url)


class ReviewAdapterAndLiveTests(unittest.TestCase):
    def test_redirect_expiry_reauthenticates_then_returns_fresh_json(self):
        previous, session, auth = adapter_with(
            response("", 302), response(json.dumps(grid(case_row())))
        )
        adapter = ReviewAdapter(previous.runtime)
        self.assertEqual(len(adapter.get_cases(ReviewCaseFilter(doctor_card="SYNTHETIC"))), 1)
        auth.login.assert_called_once_with(force=True)
        self.assertEqual(session.request.call_args.kwargs["data"], {"VSDrID": "SYNTHETIC"})
        self.assertFalse(session.request.call_args.kwargs["allow_redirects"])
        self.assertEqual(
            session.request.call_args.kwargs["headers"]["X-Requested-With"], "XMLHttpRequest"
        )
        self.assertTrue(all(not operation_spec(key).mutates for key in REVIEW_KEYS))

    def test_default_combined_round_and_review_config_roundtrip(self):
        config = resolve_live_test_config(
            json_values=combined_round(), environ={}
        ).with_default_doctor("SYNTHETIC")
        plan = build_test_plan(config)
        self.assertEqual(len(plan["operations"]), 55)
        self.assertEqual(len(plan["auth_targets"]), 8)
        self.assertTrue(config.include_earnings)
        self.assertFalse(config.weekly_opd_soap)
        config = LiveTestConfig(
            profile="atomic",
            only_operations=REVIEW_KEYS,
            review_query={"mrn": "SYNTHETIC", "start_date": "2026-09-01"},
        )
        config.validate_for_execution()
        self.assertEqual(
            resolve_live_test_config(json_values=config.to_safe_dict(), environ={}), config
        )
        self.assertEqual(
            [s.key for s in resolve_auth_targets(build_test_plan(config)["auth_targets"])],
            ["portal", "review"],
        )
        for bad in ({"unknown": "x"}, {"doctor_card": []}, {"start_date": "yesterday"}):
            with self.subTest(bad=bad), self.assertRaises(ConfigurationError):
                LiveTestConfig(review_query=bad)

    def test_sampling_keeps_unfavorable_outcome_and_defaults_to_login_doctor(self):
        config = LiveTestConfig(profile="comprehensive", max_items=2).with_default_doctor(
            "SYNTHETIC"
        )
        values = {"review.options": [options()]}
        selector = _query_inputs(query_spec("review.cases"), config, values)[0]["filter"]
        self.assertEqual(selector.to_form(), {"VSDrID": "SYNTHETIC", "InsuSectNo": "OPH"})
        values["review.cases"] = [
            parse_review_cases(grid(*(case_row(str(i), "1" if i < 9 else "2") for i in range(10))))
        ]
        samples = [
            _query_inputs(query_spec(key), config, values)
            for key in ("review.case_detail", "review.orders", "review.attachments", "review.pacs")
        ]
        self.assertTrue(all(sample == samples[0] for sample in samples))
        self.assertIn({"ref": ReviewCaseRef("9")}, samples[0])

    def test_failed_case_detail_does_not_block_other_cases_or_independent_grids(self):
        cases = parse_review_cases(grid(case_row("1"), case_row("2", "2")))
        sdk = SimpleNamespace(
            auth=SimpleNamespace(
                check=Mock(
                    side_effect=lambda *, only: make_auth_report(
                        AuthCheckTarget(s.key, (), "", False, 1, 0, s.dependencies, "OK")
                        for s in resolve_auth_targets(only)
                    )
                )
            ),
            reviews=SimpleNamespace(),
        )
        for key in REVIEW_KEYS:
            setattr(sdk.reviews, query_spec(key).method, Mock(return_value=[]))
        sdk.reviews.get_options.return_value = options()
        sdk.reviews.get_cases.return_value = cases
        sdk.reviews.get_case.side_effect = [ParseError("synthetic one-detail error"), cases[1]]
        for name, kind in (
            ("get_orders", "orders"),
            ("get_attachments", "attachments"),
            ("get_pacs", "pacs"),
        ):
            getattr(sdk.reviews, name).side_effect = lambda ref, kind=kind: parse_review_part(
                grid(), ref, kind
            )
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(io.StringIO()):
            result = run_atomic_test(
                sdk,
                LiveTestConfig(
                    profile="comprehensive", only_operations=REVIEW_KEYS
                ).with_default_doctor("SYNTHETIC"),
                output_dir=Path(temporary),
            )
            self.assertEqual(result.status, "COMPLETED_WITH_ERRORS")
            for name in ("get_case", "get_orders", "get_attachments", "get_pacs"):
                self.assertEqual(getattr(sdk.reviews, name).call_count, 2)
            self.assertTrue((Path(temporary) / "parsed/atomic/review.pacs/0002.json").is_file())


@unittest.skipUnless(os.environ.get("VGHKS_RUN_HAR_CONTRACT") == "1", "private HAR opt-in")
class ReviewHarTests(unittest.TestCase):
    def test_recorded_reads_all_parse_offline_and_login_redirect_is_expected(self):
        filename = "審查系統登入+查詢送出的審查事件資訊與審查通過與否.har"
        candidates = [ROOT / "newHAR" / filename, ROOT / "data/recordings/2026-09-20" / filename]
        source = next(path for path in candidates if path.is_file())
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("requests.sessions.Session.request") as network,
        ):
            report = replay_hars(source, output_path=Path(temporary) / "report.json")
        network.assert_not_called()
        self.assertEqual(report["status_counts"], {"EXPECTED_NEGATIVE": 1, "PARSED": 13})
        self.assertEqual(report["status"], "OK")
        rows = report["exchanges"]
        self.assertEqual(
            [row["record_count"] for row in rows if row["operation"] == "review.cases"], [31]
        )
        self.assertEqual(
            [row["record_count"] for row in rows if row["operation"] == "review.pacs"], [10, 1]
        )


if __name__ == "__main__":
    unittest.main()
