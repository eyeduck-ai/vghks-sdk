"""Synthetic preparation, compatibility, replay and no-retry boundaries."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode, urlsplit

import requests

from tests.test_pacs_launch_parsing import bootstrap_html, viewer_html
from vghks_sdk import (
    PacsImageRef,
    PacsLaunchRef,
    PacsStudyRef,
    PortalCredentials,
    RequestPolicy,
    SDKSettings,
)
from vghks_sdk.adapters.pacs_launch import _validated_base
from vghks_sdk.adapters.prq import PrqAdapter
from vghks_sdk.core.errors import ConfigurationError, SDKError
from vghks_sdk.core.transport import SafeSessionTransport
from vghks_sdk.models import to_jsonable
from vghks_sdk.offline.pacs_launch import PacsLaunchReplay
from vghks_sdk.offline.replay import replay_hars, replay_response
from vghks_sdk.queries import QUERY_BY_KEY
from vghks_sdk.runtime import SDKRuntime
from vghks_sdk.services.orders import OrdersService

POLICY = RequestPolicy(min_delay_seconds=0, max_delay_seconds=0, backoff_base_seconds=0)
PARAMS = {"LoginID": "SYNTHETIC", "PatID": "0000000",
          "AccNo": "SYNTHETIC-ORDER", "LoginLevel": "RAD"}
VIEWER_PARAMS = {"LoginID": "SYNTHETIC", "WonderClientIP": "192.0.2.10"}


def response(body: str, status: int = 200) -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result.url = "http://pacswonder.vghks.gov.tw:8080/SmartWonderRAD/Query/qryExternalAP.jsp"
    result.headers["Content-Type"] = "text/html; charset=utf-8"
    result._content = body.encode("utf-8")
    result._content_consumed = True
    return result


def adapter(*outcomes):
    session = requests.Session()
    session.request = MagicMock(side_effect=outcomes)
    auth = MagicMock()
    auth.assert_not_expired = MagicMock()
    auth.generation = 1
    auth.credentials = PortalCredentials("SYNTHETIC", "synthetic-secret")
    runtime = SDKRuntime(settings=SDKSettings(request_policy=POLICY), auth=auth,
                         transport=SafeSessionTransport(policy=POLICY, session=session))
    return PrqAdapter(runtime), session, auth


class PacsLaunchAdapterTests(unittest.TestCase):
    def setUp(self):
        self.ref = PacsLaunchRef("0000000", "SYNTHETIC-ORDER")
        blocker = patch("socket.socket", side_effect=AssertionError("network prohibited"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def test_prepare_fetches_two_bound_pages_and_never_calls_local_bridge(self):
        impl, session, auth = adapter(response(bootstrap_html()), response(viewer_html()))
        context = impl.prepare_pacs_launch(self.ref)
        self.assertEqual(context.reference, self.ref)
        self.assertEqual(context.study_uid, "1.2.3.4")
        auth.ensure.assert_called_once_with("prq")
        auth.login.assert_not_called()
        calls = session.request.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].kwargs["params"], PARAMS)
        self.assertEqual(calls[1].kwargs["params"], VIEWER_PARAMS)
        for call in calls:
            self.assertEqual(call.args[0], "GET")
            self.assertEqual(urlsplit(call.args[1]).hostname, "pacswonder.vghks.gov.tw")
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertNotIn("allow_retry", call.kwargs)
            self.assertNotIn("synthetic-secret", str(call))
        safe = json.dumps(to_jsonable(context)) + repr(context)
        for private in ("0000000", "SYNTHETIC-ORDER", "SYNTHETIC", "192.0.2.10", "1.2.3.4"):
            self.assertNotIn(private, safe)

    def test_no_password_replay_or_get_retry_on_denial_redirect_busy_or_timeout(self):
        for outcome in (response("denied", 401), response("denied", 403),
                        response("busy", 503), response("redirect", 302),
                        requests.Timeout("sensitive original request omitted")):
            with self.subTest(kind=type(outcome).__name__, status=getattr(outcome, "status_code", None)):
                impl, session, auth = adapter(outcome)
                with self.assertRaises(SDKError) as raised:
                    impl.prepare_pacs_launch(self.ref)
                self.assertIs(raised.exception.info.retry_safe, False)
                self.assertFalse(raised.exception.info.retry_recommended)
                self.assertEqual(session.request.call_count, 1)
                auth.ensure.assert_called_once_with("prq")
                auth.login.assert_not_called()

    def test_mismatched_patient_never_requests_viewer(self):
        impl, session, _ = adapter(response(bootstrap_html(mrn="0000001")))
        with self.assertRaises(SDKError):
            impl.prepare_pacs_launch(self.ref)
        self.assertEqual(session.request.call_count, 1)

    def test_jpeg_reference_cannot_be_used_as_native_launch_reference(self):
        impl, session, auth = adapter()
        with self.assertRaises(ConfigurationError):
            impl.prepare_pacs_launch(PacsStudyRef("0000000", "SYNTHETIC-ORDER"))
        session.request.assert_not_called()
        auth.ensure.assert_not_called()

    def test_unknown_endpoint_is_rejected_before_authentication(self):
        for value in ("http://example.test/SmartWonderRAD", "http://127.0.0.1:123/Other",
                      "http://user@localhost:123/SmartWonderRAD", None):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                _validated_base(value)
        self.assertEqual(_validated_base("http://127.0.0.1:123/SmartWonderRAD"),
                         "http://127.0.0.1:123/SmartWonderRAD")

    def test_old_and_new_jpeg_names_share_exact_adapter_results(self):
        study, image = object(), object()
        impl = SimpleNamespace(get_pacs_study=MagicMock(return_value=study),
                               download_pacs_image=MagicMock(return_value=image))
        service = OrdersService(impl)
        ref = PacsStudyRef("0000000", "SYNTHETIC-ORDER")
        image_ref = PacsImageRef("0000000", "SYNTHETIC-ORDER", uid="1.2.3")
        self.assertIs(service.get_pacs_jpeg_study(ref), study)
        self.assertIs(service.get_pacs_study(ref), study)
        self.assertIs(service.download_pacs_jpeg(image_ref), image)
        self.assertIs(service.download_pacs_image(image_ref), image)
        self.assertEqual(impl.get_pacs_study.call_count, 2)
        self.assertEqual(impl.download_pacs_image.call_count, 2)
        self.assertIn("prq.pacs_study", QUERY_BY_KEY)
        self.assertIn("prq.pacs_image", QUERY_BY_KEY)
        self.assertFalse(any("launch" in key or "smartiris" in key for key in QUERY_BY_KEY))


class PacsLaunchReplayTests(unittest.TestCase):
    @staticmethod
    def har_entry(endpoint, params, body, *, status=200, page="page-1"):
        content = {"mimeType": "text/html; charset=utf-8"}
        if body is not None:
            content["text"] = body
        return {
            "pageref": page,
            "request": {
                "method": "GET",
                "url": "http://pacswonder.vghks.gov.tw:8080/SmartWonderRAD/" + endpoint
                + "?" + urlencode(params),
            },
            "response": {"status": status, "content": content},
        }

    def test_complete_pair_replays_without_sockets_and_without_identifiers(self):
        state = PacsLaunchReplay()
        with patch("socket.socket", side_effect=AssertionError("network prohibited")):
            first = replay_response("smartwonder.launch_bootstrap", bootstrap_html().encode(),
                                    PARAMS, pacs_replay=state, pacs_scope="operation-1")
            second = replay_response("smartwonder.launch_viewer", viewer_html().encode(),
                                     VIEWER_PARAMS, pacs_replay=state, pacs_scope="operation-1")
        self.assertEqual(first["pacs_launch_stage"], "BOOTSTRAP_PARSED")
        self.assertEqual(second["pacs_launch_stage"], "CONTEXT_PREPARED")
        self.assertNotIn("SYNTHETIC", json.dumps([first, second]))

    def test_other_operation_or_missing_new_bootstrap_cannot_reuse_old_context(self):
        state = PacsLaunchReplay()
        replay_response("smartwonder.launch_bootstrap", bootstrap_html().encode(), PARAMS,
                        pacs_replay=state, pacs_scope="operation-1")
        missing = replay_response("smartwonder.launch_viewer", viewer_html().encode(), VIEWER_PARAMS,
                                  pacs_replay=state, pacs_scope="operation-2")
        self.assertEqual(missing["error_code"], "PACS_LAUNCH_CONTEXT_REQUIRED")
        replay_response("smartwonder.launch_bootstrap", b"", PARAMS,
                        pacs_replay=state, pacs_scope="operation-1")
        missing = replay_response("smartwonder.launch_viewer", viewer_html().encode(), VIEWER_PARAMS,
                                  pacs_replay=state, pacs_scope="operation-1")
        self.assertEqual(missing["error_code"], "PACS_LAUNCH_CONTEXT_REQUIRED")

    def test_viewer_login_and_ip_must_match_bootstrap(self):
        state = PacsLaunchReplay()
        replay_response("smartwonder.launch_bootstrap", bootstrap_html().encode(), PARAMS,
                        pacs_replay=state, pacs_scope="operation-1")
        result = replay_response("smartwonder.launch_viewer", viewer_html().encode(),
                                 {**VIEWER_PARAMS, "LoginID": "OTHER"},
                                 pacs_replay=state, pacs_scope="operation-1")
        self.assertEqual(result["error_code"], "PACS_VIEWER_CONTEXT_MISMATCH")

    def test_har_new_bootstrap_invalidates_previous_study_before_contract_or_body_checks(self):
        cases = [
            ("missing_query_key", {key: value for key, value in PARAMS.items() if key != "AccNo"},
             bootstrap_html(), 200),
            ("unknown_login_level", {**PARAMS, "LoginLevel": "UNKNOWN"}, bootstrap_html(), 200),
            ("missing_body", PARAMS, None, 200),
            ("invalid_body", PARAMS, "<html>unknown page</html>", 200),
            ("http_error", PARAMS, bootstrap_html(), 503),
            ("redirect", PARAMS, None, 302),
            ("network_failure", PARAMS, None, 0),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for label, params, body, status in cases:
                with self.subTest(case=label):
                    entries = [
                        self.har_entry("Query/qryExternalAP.jsp", PARAMS, bootstrap_html()),
                        self.har_entry("Viewer/vwrIris.jsp", VIEWER_PARAMS, viewer_html()),
                        self.har_entry("Query/qryExternalAP.jsp", params, body, status=status),
                        self.har_entry("Viewer/vwrIris.jsp", VIEWER_PARAMS, viewer_html()),
                    ]
                    path = root / "synthetic.har"
                    path.write_text(json.dumps({"log": {"version": "1.2", "entries": entries}}),
                                    encoding="utf-8")
                    with patch("socket.socket", side_effect=AssertionError("network prohibited")):
                        report = replay_hars(path, output_path=root / "replay.json")
                    viewers = [row for row in report["exchanges"]
                               if row["operation"] == "smartwonder.launch_viewer"]
                    self.assertEqual(viewers[0]["pacs_launch_stage"], "CONTEXT_PREPARED")
                    self.assertEqual(viewers[1]["status"], "UNAVAILABLE")
                    self.assertEqual(viewers[1]["error_code"], "PACS_LAUNCH_CONTEXT_REQUIRED")
                    self.assertNotIn("SYNTHETIC", json.dumps(report))
