"""Manufactured PACS launch pages, bindings and native-button discovery."""

from __future__ import annotations

import html
import json
import unittest
from dataclasses import FrozenInstanceError, replace
from urllib.parse import parse_qs, urlencode, urlsplit

from vghks_sdk.core.errors import ConfigurationError, NotAuthenticatedError, ParseError
from vghks_sdk.models import to_jsonable
from vghks_sdk.models.assets import PacsStudyRef
from vghks_sdk.models.orders import (
    ClinicalOrder,
    OrderDetail,
    OrderDetailRef,
    OrderReport,
    OrderReportRef,
)
from vghks_sdk.models.pacs_launch import (
    SMARTWONDER_BASE_URL,
    SMARTWONDER_WADO_URL,
    PacsLaunchContext,
    PacsLaunchRef,
)
from vghks_sdk.parsing.clinical import parse_clinical_orders, parse_order_detail, parse_order_report
from vghks_sdk.parsing.pacs_launch import parse_pacs_launch_bootstrap, parse_pacs_launch_context

MRN, REQUEST_NO = "0000000", "SYNTHETIC-ORDER"
LOGIN_ID, CLIENT_IP, STUDY_UID = "SYNTHETIC", "192.0.2.10", "1.2.3.4"


def bootstrap_html(*, mrn=MRN, request_no=REQUEST_NO, login_id=LOGIN_ID,
                   client_ip=CLIENT_IP, study_uid=STUDY_UID, **assignments):
    values = {
        "m_szUserID": login_id, "m_szUserIP": client_ip, "m_szProgID": "qryExternalAP",
        "m_szHospID": "", "m_szPatID": mrn, "m_szAccNo": request_no,
        "m_szDicomServer": "SK", "m_szLevel": "STUDY", "m_szUID": study_uid,
        "m_szGoCFind": "true", "m_szViewerModule": "../Viewer/vwrIris.jsp",
        "m_szDownloadApp": "false", "m_szGEZFP": "false",
    }
    values.update(assignments)
    source = []
    for key, value in values.items():
        expression = json.dumps(value)
        if key == "m_szUID":
            expression = f'Trim({expression}, ",")'
        source.append(f"var {key}={expression};")
    return "<html><script>" + "\n".join(source) + "</script></html>"


def wado_url(*, login_id=LOGIN_ID, client_ip=CLIENT_IP):
    return SMARTWONDER_WADO_URL + "?" + urlencode({
        "requestType": "WADO", "contentType": "application/dicom;text/plain",
        "DicomServer": "SK", "PID": "vwrIris",
        "Dcm00110010": login_id, "Dcm00110011": client_ip,
    })


def viewer_html(*, login_id=LOGIN_ID, client_ip=CLIENT_IP, wado_url=None):
    image_url = wado_url or globals()["wado_url"](login_id=login_id, client_ip=client_ip)
    user_query = "&" + urlencode({"PID": "vwrIris", "UserID": login_id,
                                  "ClientIP": client_ip, "NotifyResult": "false"})
    return f"""<html><script>
var szCodeBase=location.protocol+"//"+location.host+location.pathname.substring(
    0,location.pathname.indexOf("Viewer/vwrIris.jsp"));
function showImage(p_szDicomServer,p_szLevel,p_szUID,p_szRecycle,p_szHospID,p_szPatID,p_szAccNo){{
    var aWadoURL=new Object();
    aWadoURL['SK']={json.dumps(image_url)};
    var aUIDList=p_szUID.split(/[,;]+/);
    if (aUIDList!=null){{var szUserUrl={json.dumps(user_query)};}}
}}
function initOcx(){{var szLoginID={json.dumps(login_id)};}}
</script></html>"""


def native_button(mrn=MRN, request_no=REQUEST_NO, *, button_id="pacsBtn"):
    return (f'<button id="{button_id}" hhisnum="{html.escape(mrn)}" '
            f'reqno="{html.escape(request_no)}">PACS</button>')


class PacsLaunchParsingTests(unittest.TestCase):
    def setUp(self):
        self.reference = PacsLaunchRef(MRN, REQUEST_NO)

    def parse_bootstrap(self, source=None):
        return parse_pacs_launch_bootstrap(bootstrap_html() if source is None else source,
                                           reference=self.reference,
                                           expected_login_id=LOGIN_ID)

    def test_two_stage_parse_binds_patient_study_and_builds_context_url(self):
        bootstrap = self.parse_bootstrap()
        context = parse_pacs_launch_context(bootstrap, viewer_html())
        self.assertIsInstance(context, PacsLaunchContext)
        self.assertEqual(context.reference, self.reference)
        self.assertEqual((context.study_uid, context.wonder_url), (STUDY_UID, SMARTWONDER_BASE_URL))
        self.assertTrue(context.wado_url.endswith("&"))
        query = parse_qs(urlsplit(context.cmove_url).query, keep_blank_values=True)
        self.assertEqual(query, {"HospID": [""], "PatID": [MRN], "AccNo": [REQUEST_NO],
                                 "PID": ["vwrIris"], "UserID": [LOGIN_ID],
                                 "ClientIP": [CLIENT_IP], "NotifyResult": ["false"]})
        with self.assertRaises(FrozenInstanceError):
            context.study_uid = "1.2.5"

    def test_repr_and_sdk_json_hide_context_identifiers_and_urls_recursively(self):
        bootstrap = self.parse_bootstrap()
        context = parse_pacs_launch_context(bootstrap, viewer_html())
        rendered = repr(bootstrap) + repr(context) + json.dumps(to_jsonable({"context": context}))
        for private in (MRN, REQUEST_NO, LOGIN_ID, CLIENT_IP, STUDY_UID,
                        SMARTWONDER_BASE_URL, context.wado_url, context.cmove_url):
            self.assertNotIn(private, rendered)
        self.assertEqual(to_jsonable(context)["study_count"], 1)

    def test_binding_unknown_modes_and_uid_states_fail_explicitly(self):
        for fields, code in [
            ({"mrn": "1111111"}, "PACS_LAUNCH_REFERENCE_MISMATCH"),
            ({"request_no": "OTHER-ORDER"}, "PACS_LAUNCH_REFERENCE_MISMATCH"),
            ({"login_id": "OTHER-USER"}, "PACS_LAUNCH_LOGIN_MISMATCH"),
            ({"study_uid": ""}, "PACS_LAUNCH_UID_MISSING"),
            ({"study_uid": "1.2,1.3"}, "PACS_LAUNCH_MULTIPLE_UIDS_UNSUPPORTED"),
            ({"study_uid": "1.2;1.3"}, "PACS_LAUNCH_MULTIPLE_UIDS_UNSUPPORTED"),
            ({"study_uid": "../invalid"}, "PACS_LAUNCH_UID_INVALID"),
            ({"m_szDicomServer": "OTHER"}, "PACS_LAUNCH_MODE_UNSUPPORTED"),
            ({"m_szLevel": "SERIES"}, "PACS_LAUNCH_MODE_UNSUPPORTED"),
            ({"m_szGoCFind": "NoData"}, "PACS_LAUNCH_MODE_UNSUPPORTED"),
            ({"client_ip": "not-an-address"}, "PACS_LAUNCH_CLIENT_INVALID"),
            ({"m_szViewerModule": "https://example.test/launch"}, "PACS_LAUNCH_MODE_UNSUPPORTED"),
        ]:
            with self.subTest(fields=fields), self.assertRaises(ParseError) as caught:
                self.parse_bootstrap(bootstrap_html(**fields))
            self.assertEqual(caught.exception.info.code, code)

    def test_comments_strings_callbacks_and_unbraced_branches_are_not_bootstrap_data(self):
        statements = bootstrap_html().removeprefix("<html><script>").removesuffix("</script></html>")
        for source in ["", "<p>查無資料</p>", "<script>/*" + statements + "*/</script>",
                       "<script>function later(){" + statements + "}</script>",
                       "<script>var fake=" + json.dumps(statements) + ";</script>",
                       bootstrap_html().replace("var m_szUID=", "if(false) var m_szUID="),
                       bootstrap_html().replace("var m_szUID=", "other(), m_szUID=")]:
            with self.subTest(source=source), self.assertRaises(ParseError):
                self.parse_bootstrap(source)
        with self.assertRaises(NotAuthenticatedError):
            self.parse_bootstrap('<form><input type="password">登入</form>')

    def test_duplicate_dynamic_or_unsupported_uid_assignment_is_rejected(self):
        for extra in ['var m_szUID=Trim("1.2.5", ",");', 'm_szUID=fetchUID();']:
            with self.assertRaises(ParseError):
                self.parse_bootstrap(bootstrap_html().replace("</script>", extra + "</script>"))
        with self.assertRaises(ParseError):
            self.parse_bootstrap(bootstrap_html().replace('Trim("1.2.3.4", ",")', 'getUID()'))

    def test_legacy_script_wrappers_do_not_rewrite_literal_values(self):
        wrapped = bootstrap_html().replace("<script>", "<script>\n<!--\n").replace(
            "</script>", "\n-->\n</script>")
        self.assertEqual(self.parse_bootstrap(wrapped).study_uid, STUDY_UID)
        with self.assertRaises(ParseError) as caught:
            self.parse_bootstrap(bootstrap_html(study_uid="<!--1.2.3.4-->"))
        self.assertEqual(caught.exception.info.code, "PACS_LAUNCH_UID_INVALID")

    def test_viewer_binding_source_query_duplicates_and_different_user_are_rejected(self):
        bootstrap = self.parse_bootstrap()
        for source in [viewer_html(login_id="OTHER"), viewer_html(client_ip="192.0.2.11"),
                       viewer_html(wado_url=wado_url().replace("192.168.195.101", "127.0.0.1")),
                       viewer_html(wado_url=wado_url()+"&PID=vwrIris"),
                       viewer_html(wado_url=wado_url().replace("vwrIris", "other")),
                       viewer_html(wado_url=wado_url().replace("SK", "OTHER")),
                       viewer_html(wado_url=wado_url()+"&extra=1")]:
            with self.subTest(source=source), self.assertRaises(ParseError):
                parse_pacs_launch_context(bootstrap, source)

    def test_fake_function_or_wado_and_later_dynamic_overwrite_fail_closed(self):
        bootstrap = self.parse_bootstrap()
        source = viewer_html()
        variants = [
            '<script>var fake=' + json.dumps(source) + ';</script>',
            source.replace("aWadoURL['SK']=", "if(false) aWadoURL['SK']="),
            source.replace("var aUIDList=", "aWadoURL['SK']=getURL(); var aUIDList="),
            source.replace("var aUIDList=", "aWadoURL[key]=getURL(); var aUIDList="),
            source.replace("var aUIDList=", "aWadoURL['S'+'K']=getURL(); var aUIDList="),
            source.replace("var aUIDList=", "aWadoURL['SK']+=getURL(); var aUIDList="),
            source.replace("var aUIDList=", "aWadoURL=getURLs(); var aUIDList="),
        ]
        for value in variants:
            with self.subTest(source=value), self.assertRaises(ParseError):
                parse_pacs_launch_context(bootstrap, value)

    def test_context_constructor_validates_direct_callers(self):
        context = parse_pacs_launch_context(self.parse_bootstrap(), viewer_html())
        for changed in [{"wonder_url": "https://example.test"}, {"client_ip": 1},
                        {"wado_url": context.wado_url + "&"}, {"login_id": "OTHER"}]:
            with self.subTest(changed=changed), self.assertRaises(ConfigurationError):
                replace(context, **changed)
        object.__setattr__(context.reference, "request_no", "../unsafe")
        with self.assertRaises(ConfigurationError):
            replace(context)


class PacsLaunchDiscoveryTests(unittest.TestCase):
    def test_native_button_is_distinct_from_jpg_or_arbitrary_reqno(self):
        reference = OrderReportRef(MRN, "CASE", "O", "1")
        for content, count in [(native_button(), 1), (native_button(button_id="jpgBtn"), 0),
                               ('<a reqno="SYNTHETIC-ORDER">PACS</a>', 0),
                               ('<!--'+native_button()+'-->', 0),
                               ('<script>var fake='+json.dumps(native_button())+';</script>', 0),
                               ('<template>'+native_button()+'</template>', 0)]:
            report = parse_order_report('<table><tr><td>檢查名稱:</td><td>合成</td></tr></table>'+content,
                                        reference=reference)
            self.assertEqual(len(report.pacs_launch_refs), count)
            self.assertEqual(report.jpeg_refs, report.pacs_refs)

    def test_native_button_binding_and_missing_fields_fail(self):
        reference = OrderReportRef(MRN, "CASE", "O", "1")
        for content in [native_button(mrn="1111111"), native_button(request_no=""),
                        '<button id="pacsBtn" reqno="SYNTHETIC-ORDER">PACS</button>']:
            with self.assertRaises(ParseError):
                parse_order_report(content, reference=reference)

    def test_order_uses_only_rendered_active_constructor_values_for_native_button(self):
        values = ["", "Synthetic order", "2026-01-01", "2026-01-01", "Synthetic", "OK", native_button()]
        source = "<script>new KSCase(" + ",".join(json.dumps(value) for value in values) + ");</script>"
        orders = parse_clinical_orders(source, mrn=MRN)
        self.assertEqual(orders[0].pacs_launch_ref, PacsLaunchRef(MRN, REQUEST_NO))
        values[-1] = ""
        source = ("<script>var unused="+json.dumps(native_button())+";new KSCase("+
                  ",".join(json.dumps(value) for value in values)+");</script>")
        self.assertIsNone(parse_clinical_orders(source, mrn=MRN)[0].pacs_launch_ref)

    def test_models_keep_positional_compatibility_and_readonly_jpeg_aliases(self):
        jpeg = PacsStudyRef(MRN, REQUEST_NO)
        order = ClinicalOrder(MRN, "CASE", "O", "Synthetic", pacs_ref=jpeg)
        self.assertIs(order.jpeg_ref, jpeg)
        detail_ref = OrderDetailRef(MRN, "CASE", "O", "1")
        detail = OrderDetail(detail_ref, {}, (), (jpeg,))
        self.assertEqual(detail.jpeg_refs, (jpeg,))
        report = OrderReport(OrderReportRef(MRN, "CASE", "O", "1"), {}, (), (jpeg,))
        self.assertEqual(report.jpeg_refs, (jpeg,))
        parsed = parse_order_detail('<table><tr><td>檢查名稱:</td><td>合成</td></tr></table>'+native_button(),
                                    reference=detail_ref)
        self.assertEqual(parsed.pacs_launch_refs, (PacsLaunchRef(MRN, REQUEST_NO),))


if __name__ == "__main__":
    unittest.main()
