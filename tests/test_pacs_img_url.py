"""Synthetic PACS viewer variants through models, services and offline replay."""

from __future__ import annotations

import html
import unittest
from dataclasses import replace
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

from vghks_sdk import PortalCredentials, RequestPolicy, SDKSettings, VghksSDK
from vghks_sdk.adapters.auth import AppSession
from vghks_sdk.contracts.har import BASELINE_CONTRACTS, HarEntry, HarSignature
from vghks_sdk.core.browser_headers import IMAGE_ACCEPT
from vghks_sdk.core.errors import AuthExpiredError, ConfigurationError, ParseError
from vghks_sdk.core.operations import operation_spec
from vghks_sdk.core.transport import SafeSessionTransport
from vghks_sdk.models import PacsImageRef, PacsStudyRef, to_jsonable
from vghks_sdk.offline.replay import replay_response
from vghks_sdk.parsing.clinical import parse_pacs_study

MRN, REQUEST_NO, HID = "0000000", "SYNTHETIC-REQ", "CURRENT-HID"
PROXY_PATH = "/PRQWeb/Page/JSP/showPACSPic.jsp"
VIEWER_PATH = "/PRQWeb/Adm_QueryPACS.do"
# The service origin/path are the allowlisted contract. All identifiers, HTML,
# response bytes and query values below are manufactured, never copied evidence.
WADO_ENDPOINT = "http://192.168.20.137:800/xwado/PushWADO"
JPEG = b"\xff\xd8synthetic-image\xff\xd9"


def image_url(uid="1.2.3", **overrides):
    query = {"requestType": "WADO", "contentType": "image/jpeg", "rows": "512", "objectUID": uid}
    query.update(overrides)
    return WADO_ENDPOINT + "?" + urlencode(query)


def image_tag(**overrides):
    query = {"hhisnum": MRN, "hid": "OLD-HID", "reqno": REQUEST_NO, **overrides}
    source = PROXY_PATH + "?" + urlencode(query)
    return f'<img class="pacsJpg" src="{html.escape(source, quote=True)}">'


def viewer(*tags):
    return '<div id="pacsContent">' + "".join(tags) + "</div>"


class PacsImageModelTests(unittest.TestCase):
    def test_legacy_positional_reference_and_both_json_round_trips(self):
        old = PacsImageRef(MRN, REQUEST_NO, "SERIES", "STUDY", "1.2.3")
        new = PacsImageRef(MRN, REQUEST_NO, img_url=image_url())
        self.assertEqual(
            (old.series_uid, old.study_uid, old.uid, old.img_url), ("SERIES", "STUDY", "1.2.3", "")
        )
        self.assertEqual((new.series_uid, new.study_uid, new.uid), ("", "", ""))
        for ref in (old, new):
            self.assertEqual(PacsImageRef(**to_jsonable(ref)), ref)
            self.assertEqual(hash(PacsImageRef(**to_jsonable(ref))), hash(ref))
        self.assertNotIn(image_url(), repr(new))

    def test_missing_and_mixed_reference_modes_are_rejected(self):
        for fields, code in (
            ({}, "NAV_REF_INCOMPLETE"),
            ({"img_url": None}, "PACS_IMG_URL_INVALID"),
            ({"uid": "1.2.3", "img_url": image_url()}, "PACS_REF_AMBIGUOUS"),
            ({"series_uid": "SERIES", "img_url": image_url()}, "PACS_REF_AMBIGUOUS"),
            ({"study_uid": "STUDY", "img_url": image_url()}, "PACS_REF_AMBIGUOUS"),
        ):
            with self.subTest(code=code), self.assertRaises(ConfigurationError) as caught:
                PacsImageRef(MRN, REQUEST_NO, **fields)
            self.assertEqual(caught.exception.info.code, code)

    def test_unknown_sources_paths_and_wado_queries_are_rejected(self):
        good = image_url()
        for label, value, code in (
            (
                "external",
                good.replace("192.168.20.137:800", "example.test"),
                "PACS_IMG_URL_EXTERNAL",
            ),
            ("loopback", good.replace("192.168.20.137", "127.0.0.1"), "PACS_IMG_URL_EXTERNAL"),
            ("credentials", good.replace("http://", "http://user:pass@"), "PACS_IMG_URL_EXTERNAL"),
            ("other port", good.replace(":800/", ":801/"), "PACS_IMG_URL_EXTERNAL"),
            ("relative", good.removeprefix("http:"), "PACS_IMG_URL_EXTERNAL"),
            ("other path", good.replace("PushWADO", "Other"), "PACS_IMG_URL_PATH_UNKNOWN"),
            ("traversal", good.replace("/xwado/", "/xwado/../"), "PACS_IMG_URL_PATH_UNKNOWN"),
            ("fragment", good + "#other", "PACS_IMG_URL_INVALID"),
            ("control", good + "\n", "PACS_IMG_URL_INVALID"),
            ("malformed host", "http://[broken/", "PACS_IMG_URL_INVALID"),
            ("missing key", good.replace("rows=512&", ""), "PACS_IMG_URL_INVALID"),
            ("duplicate key", good + "&objectUID=1.2.4", "PACS_IMG_URL_INVALID"),
            ("extra key", good + "&target=http://example.test", "PACS_IMG_URL_INVALID"),
            ("wrong request", image_url(requestType="STORE"), "PACS_IMG_URL_INVALID"),
            ("wrong MIME", image_url(contentType="text/html"), "PACS_IMG_URL_INVALID"),
            ("blank UID", image_url(uid=""), "PACS_IMG_URL_INVALID"),
            ("unsafe UID", image_url(uid="../other"), "PACS_IMG_URL_INVALID"),
            ("large UID", image_url(uid="1" * 257), "PACS_IMG_URL_INVALID"),
            ("invalid rows", image_url(rows="-1"), "PACS_IMG_URL_INVALID"),
        ):
            with self.subTest(case=label), self.assertRaises(ConfigurationError) as caught:
                PacsImageRef(MRN, REQUEST_NO, img_url=value)
            self.assertEqual(caught.exception.info.code, code)
            self.assertNotIn(value, str(caught.exception))


class PacsViewerTests(unittest.TestCase):
    def setUp(self):
        self.reference = PacsStudyRef(MRN, REQUEST_NO)
        for target in ("socket.socket", "socket.create_connection"):
            patcher = patch(target, side_effect=AssertionError("network disabled"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_explicit_empty_viewer_accepts_repeated_punctuation_and_replays_empty(self):
        for message in ("查無資料", "查無資料!", "查無資料!!", "查無資料！！", " 查無資料 ! ! ",
                        "查無資料。", "查無資料..."):
            with self.subTest(message=message):
                page = viewer("<!-- synthetic branch -->", "<script>unused();</script>", message)
                study = parse_pacs_study(page, reference=self.reference)
                self.assertEqual((study.data_status, study.empty_reason, study.images),
                                 ("EMPTY", "NO_IMAGES", ()))
                replayed = replay_response("prq.pacs_study", page.encode(),
                                           {"hhisnum": MRN, "reqno": REQUEST_NO})
                self.assertEqual(replayed["status"], "EMPTY")
                self.assertEqual(replayed["data_availability"], "EMPTY")
                self.assertEqual(replayed["record_count"], 0)

    def test_empty_literal_in_script_or_changed_or_login_page_is_not_an_empty_viewer(self):
        for page in (viewer(""), viewer('<script>var result="查無資料!!";</script>'),
                     viewer("查無資料!! 請重新登入"), viewer("查無資料!! 系統錯誤"),
                     '<form><input type="password">請登入</form>',
                     '<div id="pacsContent">unknown layout</div>'):
            with self.subTest(page=page), self.assertRaises(ParseError) as caught:
                parse_pacs_study(page, reference=self.reference)
            self.assertEqual(caught.exception.info.code, "PACS_STRUCTURE_UNRECOGNIZED")

    def test_all_59_img_url_images_survive_with_duplicates_removed(self):
        tags = [image_tag(imgUrl=image_url(f"1.2.3.{n}")) for n in range(59)]
        source = viewer(*tags, tags[0])
        study = parse_pacs_study(source, reference=self.reference)
        self.assertEqual(study.data_status, "IMAGES_AVAILABLE")
        self.assertEqual(len(study.images), 59)
        self.assertEqual(
            [ref.img_url for ref in study.images], [image_url(f"1.2.3.{n}") for n in range(59)]
        )
        self.assertTrue(
            all(ref.mrn == MRN and ref.request_no == REQUEST_NO for ref in study.images)
        )
        self.assertTrue(all(not ref.uid for ref in study.images))
        replayed = replay_response(
            "prq.pacs_study",
            source.encode(),
            {"hhisnum": MRN, "reqno": REQUEST_NO},
            mime="text/html; charset=utf-8",
        )
        self.assertEqual((replayed["status"], replayed["record_count"]), ("PARSED", 59))

    def test_uid_and_img_url_modes_keep_separate_references(self):
        old_tag = image_tag(uid="1.2.3", SERIES_UID="SERIES", STUDY_UID="STUDY")
        new_tag = image_tag(imgUrl=image_url())
        study = parse_pacs_study(
            viewer(old_tag, new_tag, old_tag, new_tag), reference=self.reference
        )
        self.assertEqual(
            study.images,
            (
                PacsImageRef(MRN, REQUEST_NO, "SERIES", "STUDY", "1.2.3"),
                PacsImageRef(MRN, REQUEST_NO, img_url=image_url()),
            ),
        )
        replayed = replay_response(
            "prq.pacs_study", viewer(old_tag).encode(), {"hhisnum": MRN, "reqno": REQUEST_NO}
        )
        self.assertEqual((replayed["status"], replayed["record_count"]), ("PARSED", 1))

    def test_patient_and_request_binding_are_required_for_img_url(self):
        for fields, code in (
            ({"hhisnum": "9999999"}, "PACS_REF_MRN_MISMATCH"),
            ({"reqno": "OTHER-REQ"}, "PACS_REF_REQUEST_MISMATCH"),
            ({"hhisnum": ""}, "PACS_REF_MRN_MISMATCH"),
            ({"reqno": ""}, "PACS_REF_REQUEST_MISMATCH"),
        ):
            source = viewer(image_tag(imgUrl=image_url(), **fields))
            with self.subTest(code=code), self.assertRaises(ParseError) as caught:
                parse_pacs_study(source, reference=self.reference)
            self.assertEqual(caught.exception.info.code, code)
        for key in ("hhisnum", "reqno"):
            query = {"hhisnum": MRN, "reqno": REQUEST_NO, "imgUrl": image_url()}
            del query[key]
            source = PROXY_PATH + "?" + urlencode(query)
            with self.subTest(missing=key), self.assertRaises(ParseError) as caught:
                parse_pacs_study(
                    viewer(f'<img src="{html.escape(source)}">'), reference=self.reference
                )
            self.assertEqual(caught.exception.info.code, "PACS_REF_INVALID")

    def test_unsafe_and_ambiguous_references_remain_parse_errors(self):
        for fields, code in (
            (
                {"imgUrl": image_url().replace("192.168.20.137:800", "example.test")},
                "PACS_IMG_URL_EXTERNAL",
            ),
            ({"imgUrl": image_url(uid="")}, "PACS_IMG_URL_INVALID"),
            ({"imgUrl": image_url(), "uid": "1.2.3"}, "PACS_REF_AMBIGUOUS"),
            ({"imgUrl": ""}, "PACS_REF_INVALID"),
        ):
            source = viewer(image_tag(**fields))
            with self.subTest(code=code), self.assertRaises(ParseError) as caught:
                parse_pacs_study(source, reference=self.reference)
            self.assertEqual(caught.exception.info.code, code)
            replayed = replay_response(
                "prq.pacs_study", source.encode(), {"hhisnum": MRN, "reqno": REQUEST_NO}
            )
            self.assertEqual(replayed["error_code"], code)

    def test_img_url_does_not_relax_outer_proxy_restrictions(self):
        source = image_tag(imgUrl=image_url())
        for changed, code in (
            (source.replace(PROXY_PATH, "https://example.test" + PROXY_PATH), "PACS_REF_EXTERNAL"),
            (source.replace(PROXY_PATH, "/PRQWeb/Page/JSP/unknown.jsp"), "PACS_REF_PATH_UNKNOWN"),
        ):
            with self.subTest(code=code), self.assertRaises(ParseError) as caught:
                parse_pacs_study(viewer(changed), reference=self.reference)
            self.assertEqual(caught.exception.info.code, code)


class PacsSession:
    """In-memory HTTP responses; every unexpected request fails the test."""

    def __init__(self, page, content=JPEG, mime="image/jpeg"):
        self.headers = {}
        self.cookies = requests.cookies.RequestsCookieJar()
        self.page, self.content, self.mime = page, content, mime
        self.calls = []

    def request(self, method, url, **kwargs):
        prepared = requests.Request(method, url, params=kwargs.get("params")).prepare()
        self.calls.append((method, url, kwargs, prepared))
        path = urlsplit(url).path
        if path == VIEWER_PATH:
            body, mime = self.page.encode(), "text/html; charset=utf-8"
        elif path == PROXY_PATH:
            body, mime = self.content, self.mime
        else:
            raise AssertionError("unexpected HTTP endpoint")
        response = requests.Response()
        response.status_code = 200
        response._content, response._content_consumed = body, True
        response.url, response.request = prepared.url, prepared
        response.headers.update({"Content-Type": mime, "Content-Length": str(len(body))})
        return response

    def close(self):
        pass


class PacsDownloadTests(unittest.TestCase):
    def sdk(self, session):
        policy = RequestPolicy(min_delay_seconds=0, max_delay_seconds=0, max_attempts=1)
        settings = SDKSettings(request_policy=policy)
        sdk = VghksSDK(
            settings=settings,
            credentials=PortalCredentials("SYNTHETIC", "SYNTHETIC"),
            transport=SafeSessionTransport(policy=policy, session=session),
        )
        self.addCleanup(sdk.close)
        sdk._runtime.auth._portal_authenticated = True
        sdk._runtime.auth._generation = 1
        sdk._runtime.auth._apps["prq"] = AppSession("prq", HID, settings.prq_base_url + "/landing")
        return sdk

    def test_both_services_download_via_prq_with_the_exact_variant_params(self):
        for label, fields in (
            ("uid", {"uid": "1.2.3", "SERIES_UID": "SERIES", "STUDY_UID": "STUDY"}),
            ("imgUrl", {"imgUrl": image_url()}),
        ):
            with (
                self.subTest(mode=label),
                patch("socket.socket", side_effect=AssertionError("network disabled")),
            ):
                session = PacsSession(viewer(image_tag(**fields)))
                sdk = self.sdk(session)
                study = sdk.orders.get_pacs_study(PacsStudyRef(MRN, REQUEST_NO))
                asset = sdk.orders.download_pacs_image(study.images[0])
            self.assertEqual((asset.content, asset.media_type), (JPEG, "image/jpeg"))
            self.assertEqual(
                [urlsplit(call[1]).path for call in session.calls], [VIEWER_PATH, PROXY_PATH]
            )
            self.assertTrue(
                all(
                    call[1].startswith(sdk._runtime.settings.prq_base_url) for call in session.calls
                )
            )
            method, _, kwargs, prepared = session.calls[-1]
            self.assertEqual(method, "GET")
            self.assertEqual(
                kwargs["params"], {"hhisnum": MRN, "hid": HID, "reqno": REQUEST_NO, **fields}
            )
            self.assertEqual(kwargs["headers"]["Accept"], IMAGE_ACCEPT)
            referer = urlsplit(kwargs["headers"]["Referer"])
            self.assertEqual(referer.path, VIEWER_PATH)
            self.assertEqual(
                parse_qs(referer.query),
                {"hhisnum": [MRN], "hid": [HID], "reqno": [REQUEST_NO], "reqCode": ["queryPACS"]},
            )
            if label == "imgUrl":
                self.assertEqual(parse_qs(urlsplit(prepared.url).query)["imgUrl"], [image_url()])

    def test_both_variants_preserve_binary_and_corrupt_jpeg_handling(self):
        for ref in (
            PacsImageRef(MRN, REQUEST_NO, uid="1.2.3"),
            PacsImageRef(MRN, REQUEST_NO, img_url=image_url()),
        ):
            for mime in ("image/jpeg", "", "text/html; charset=utf-8"):
                with self.subTest(mode=bool(ref.img_url), mime=mime):
                    session = PacsSession("", mime=mime)
                    asset = self.sdk(session).orders.download_pacs_image(ref)
                    self.assertEqual(asset.content, JPEG)
                    self.assertEqual(len(session.calls), 1)
            for body in (JPEG[:-2], b"<html>unknown image response</html>"):
                with self.subTest(mode=bool(ref.img_url), body=body):
                    session = PacsSession("", content=body)
                    with self.assertRaises(ParseError) as caught:
                        self.sdk(session).orders.download_pacs_image(ref)
                    self.assertEqual(caught.exception.info.code, "PACS_JPEG_INVALID")
                    self.assertEqual(len(session.calls), 1)

    def test_login_html_still_challenges_authentication_for_img_url(self):
        body = b'<form><input name="muid"><input name="mpassword" type="password"></form>'
        session = PacsSession("", content=body)
        sdk = self.sdk(session)
        with (
            patch.object(sdk._runtime.auth, "login", side_effect=AssertionError("recovery called")),
            self.assertRaises(AuthExpiredError),
        ):
            sdk._runtime.request_binary(
                operation_spec("prq.pacs_image"),
                sdk._runtime.settings.prq_base_url + "/Page/JSP/showPACSPic.jsp",
                max_bytes=1024,
                params={"hhisnum": MRN, "hid": HID, "reqno": REQUEST_NO, "imgUrl": image_url()},
            )


class PacsContractTests(unittest.TestCase):
    def test_request_contract_recognizes_both_complete_variants(self):
        signature = HarSignature.from_operation(operation_spec("prq.pacs_image"))
        entry = HarEntry(
            "GET",
            PROXY_PATH,
            frozenset({"hhisnum", "hid", "reqno"}),
            frozenset(),
            {},
            200,
            JPEG,
            "image/jpeg",
        )
        for keys in ({"uid", "SERIES_UID", "STUDY_UID"}, {"imgUrl"}):
            matched = replace(entry, query_keys=entry.query_keys | keys)
            self.assertTrue(signature.matches(matched))
        for keys in (set(), {"uid"}, {"SERIES_UID", "STUDY_UID"}):
            self.assertFalse(signature.matches(replace(entry, query_keys=entry.query_keys | keys)))
        self.assertFalse(signature.matches(replace(entry, query_keys=frozenset({"imgUrl"}))))

    def test_img_url_viewer_contract_and_jpeg_replay_share_production_parsers(self):
        source = viewer(image_tag(imgUrl=image_url()), image_tag(imgUrl=image_url("1.2.4")))
        entry = HarEntry(
            "GET",
            VIEWER_PATH,
            frozenset({"hhisnum", "hid", "reqCode", "reqno"}),
            frozenset(),
            {"reqCode": "queryPACS"},
            200,
            source.encode(),
            "text/html",
            context_mrn=MRN,
        )
        contract = next(c for c in BASELINE_CONTRACTS if c.operation_key == "prq.pacs_study")
        self.assertTrue(contract.signature.matches(entry))
        contract.validator(entry)
        for params in ({"imgUrl": image_url()}, {"uid": "1.2.3"}):
            result = replay_response("prq.pacs_image", JPEG, params, mime="image/jpeg")
            self.assertEqual(
                (result["status"], result["data_availability"]), ("PARSED", "BINARY_AVAILABLE")
            )


if __name__ == "__main__":
    unittest.main()
