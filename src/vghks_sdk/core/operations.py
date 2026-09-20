"""Declarative metadata for endpoints; clinical writes are explicitly marked.

The registry contains structure only. Dynamic identifiers, credentials, HID,
tokens, and request values are deliberately built by domain adapters.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OperationSpec:
    key: str
    app: str
    method: str
    path: str
    response_kind: str = "text"
    retry_safe: bool = False
    query_keys: frozenset[str] = frozenset()
    form_keys: frozenset[str] = frozenset()
    operation_values: tuple[tuple[str, str], ...] = ()
    contract_required: bool = True
    mutates: bool = False


def _spec(
    key: str,
    app: str,
    method: str,
    path: str,
    *,
    response_kind: str = "text",
    retry_safe: bool = False,
    query: tuple[str, ...] = (),
    form: tuple[str, ...] = (),
    values: tuple[tuple[str, str], ...] = (),
    contract_required: bool = True,
    mutates: bool = False,
) -> OperationSpec:
    return OperationSpec(
        key=key,
        app=app,
        method=method,
        path=path,
        response_kind=response_kind,
        retry_safe=retry_safe,
        query_keys=frozenset(query),
        form_keys=frozenset(form),
        operation_values=values,
        contract_required=contract_required,
        mutates=mutates,
    )


OPERATIONS: tuple[OperationSpec, ...] = (
    _spec("portal.entry", "portal", "GET", "/index.do", contract_required=False),
    _spec(
        "portal.login",
        "portal",
        "POST",
        "/login.do",
        query=("thetime",),
        form=("muid", "mpassword", "ssoId2"),
    ),
    _spec(
        "portal.session_check",
        "portal",
        "POST",
        "/sessionCheck.do",
        retry_safe=True,
        form=("userName",),
    ),
    _spec(
        "portal.sso_log_add",
        "portal",
        "POST",
        "/ssoLogAdd.do",
        query=("apDesc", "apOu"),
        contract_required=False,
    ),
    _spec(
        "portal.app_tree",
        "portal",
        "GET",
        "/aptreePath.do",
        retry_safe=True,
        query=("apDn", "thetime"),
        contract_required=False,
    ),
    _spec(
        "portal.sso_from_dn",
        "portal",
        "GET",
        "/ssoFromDn.do",
        retry_safe=True,
        query=("apDn",),
    ),
    _spec(
        "prq.sso_logon",
        "prq",
        "POST",
        "/PRQWeb/WPSAutoLogon",
        form=("HID", "keyOne", "keyThree", "keyTwo", "ssID", "targetURL"),
        contract_required=False,
    ),
    _spec(
        "sectord.sso_logon",
        "sectord",
        "POST",
        "/SectOrdWeb/WPSAutoLogon",
        form=("HID", "keyOne", "keyThree", "keyTwo", "ssID", "targetURL"),
        contract_required=False,
    ),
    _spec(
        "audit.sso_logon",
        "audit",
        "POST",
        "/PRQWeb/WPSAutoLogon",
        form=("HID", "keyOne", "keyThree", "keyTwo", "ssID", "targetURL"),
        contract_required=False,
    ),
    _spec(
        "sectord.key_bridge",
        "sectord",
        "GET",
        "/SectOrdWeb/so.do",
        retry_safe=True,
        query=("CardNO", "reqCode"),
        values=(("reqCode", "getRSAInfo"),),
    ),
    _spec(
        "webmaas.sso_logon",
        "webmaas",
        "GET",
        "/webmaas/WPSAutoLogon",
        query=(
            "externalRoles",
            "keyOne",
            "keyThree",
            "keyTwo",
            "singlePage",
            "ssID",
            "targetURL",
            "uid",
        ),
        contract_required=False,
    ),
    _spec(
        "webmaas.demographics",
        "webmaas",
        "POST",
        "/webmaas/ajax/AJAXAction.do",
        response_kind="json",
        retry_safe=True,
        query=("pageid", "querymethod", "simpleData"),
        form=("patno",),
        values=(("querymethod", "CHECK_PAT"),),
    ),
    _spec("webmaas.registration_landing", "webmaas", "GET", "/webmaas/RSV/RSV11W001.do"),
    _spec(
        "webmaas.basic_info_landing",
        "webmaas",
        "GET",
        "/webmaas/QUY/QUY15W001.do",
        contract_required=False,
    ),
    _spec(
        "webmaas.basic_info",
        "webmaas",
        "POST",
        "/webmaas/QUY/QUY15W001.do",
        retry_safe=True,
        form=("QRY", "org.apache.struts.taglib.html.TOKEN", "pageid", "patno", "type"),
        values=(("type", "A"),),
        contract_required=False,
    ),
    _spec(
        "webmaas.registration_query",
        "webmaas",
        "POST",
        "/webmaas/RSV/RSV11W001.do",
        retry_safe=True,
        form=("QRY", "org.apache.struts.taglib.html.TOKEN", "pageid", "pageSize", "patno"),
    ),
    _spec(
        "prq.patient_context",
        "prq",
        "POST",
        "/PRQWeb/QueryPatientRecord.do",
        retry_safe=True,
        query=("Use", "hid"),
        form=("id", "queryPtID", "type"),
        values=(("Use", "Case"),),
    ),
    _spec(
        "prq.patient_history_context",
        "prq",
        "GET",
        "/PRQWeb/QueryPatientRecord.do",
        retry_safe=True,
        query=("Use", "check", "hid", "id", "type"),
        values=(("Use", "Dur"),),
        contract_required=False,
    ),
    _spec("prq.visit_cases", "prq", "GET", "/PRQWeb/QueryCaseList.do"),
    _spec(
        "prq.patient_identity",
        "prq",
        "GET",
        "/PRQWeb/Page/JSP/KS_Patient.jsp",
        contract_required=False,
    ),
    _spec(
        "prq.case_detail",
        "prq",
        "GET",
        "/PRQWeb/QueryCaseDetail.do",
        query=("caseDT", "caseNo", "caseSec", "caseSectC", "caseType", "hhisnum"),
    ),
    _spec(
        "prq.soap",
        "prq",
        "GET",
        "/PRQWeb/QueryBillingSOAP.do",
        query=("caseNo", "casesec", "hhisnum", "reqCode"),
        values=(("reqCode", "qrySOAP"),),
    ),
    _spec(
        "prq.key_preflight",
        "prq",
        "GET",
        "/PRQWeb/GenerateKeyAction.do",
        query=("reqCode",),
        values=(("reqCode", "getKeyStr"),),
        contract_required=False,
    ),
    _spec(
        "prq.numeric_page",
        "prq",
        "GET",
        "/PRQWeb/Page/JSP/TestReport_A.jsp",
        contract_required=False,
    ),
    _spec(
        "prq.numeric_select",
        "prq",
        "GET",
        "/PRQWeb/Page/JSP/TestReport_Select.jsp",
        contract_required=False,
    ),
    _spec(
        "prq.numeric",
        "prq",
        "GET",
        "/PRQWeb/QueryResNumCenter.do",
        query=("Use", "caseNo", "caseType", "hhisnum", "queryType"),
        values=(("Use", "Case"),),
    ),
    _spec(
        "prq.numeric_duration",
        "prq",
        "POST",
        "/PRQWeb/QueryResNumCenter.do",
        retry_safe=True,
        query=("Use", "date", "dept", "hhisnum", "queryType"),
        form=("date", "dept"),
        values=(("Use", "Dur"),),
    ),
    _spec(
        "prq.case_orders_page",
        "prq",
        "GET",
        "/PRQWeb/Page/JSP/Order.jsp",
        query=("Use", "caseNo", "caseType", "casenoO", "hhisnum", "hid", "section"),
        values=(("Use", "Case"),),
        contract_required=False,
    ),
    _spec(
        "prq.case_orders_select",
        "prq",
        "GET",
        "/PRQWeb/Page/JSP/Order_Select.jsp",
        contract_required=False,
    ),
    _spec(
        "prq.case_orders",
        "prq",
        "POST",
        "/PRQWeb/QueryOrderResult.do",
        retry_safe=True,
        form=(
            "Use",
            "caseNo",
            "caseNoO",
            "caseType",
            "date",
            "hhisnum",
            "hid",
            "ordersubtype",
            "ordertype",
            "orstepc",
            "section",
        ),
        values=(("Use", "Case"),),
        contract_required=True,
    ),
    _spec(
        "prq.order_history_page",
        "prq",
        "GET",
        "/PRQWeb/Page/JSP/OrderC.jsp",
        query=("Use", "hhisnum", "hid"),
        values=(("Use", "Dur"),),
        contract_required=False,
    ),
    _spec(
        "prq.order_history_select",
        "prq",
        "GET",
        "/PRQWeb/Page/JSP/OrderC_Select.jsp",
        contract_required=False,
    ),
    _spec(
        "prq.order_history",
        "prq",
        "POST",
        "/PRQWeb/QueryOrderResult.do",
        retry_safe=True,
        form=("Use", "date", "hhisnum", "hid", "ordersubtype", "ordertype", "orstepc"),
        values=(("Use", "Dur"),),
        contract_required=True,
    ),
    _spec(
        "prq.case_medications_page",
        "prq",
        "GET",
        "/PRQWeb/Page/JSP/UD.jsp",
        query=("Use", "caseNo", "caseType", "hhisnum", "hid"),
        values=(("Use", "Case"),),
        contract_required=False,
    ),
    _spec(
        "prq.case_medications_select",
        "prq",
        "GET",
        "/PRQWeb/Page/JSP/UD_Select.jsp",
        contract_required=False,
    ),
    _spec(
        "prq.case_medications",
        "prq",
        "POST",
        "/PRQWeb/QueryUdResult.do",
        retry_safe=True,
        query=("Use", "caseNo", "caseType", "hhisnum", "hid"),
        form=("qryType", "udDate", "udStatus"),
        values=(("Use", "Case"), ("qryType", "case")),
        contract_required=True,
    ),
    _spec(
        "prq.medication_history_page",
        "prq",
        "GET",
        "/PRQWeb/Page/JSP/UDC.jsp",
        query=("Use", "hhisnum", "hid"),
        values=(("Use", "Dur"),),
        contract_required=False,
    ),
    _spec(
        "prq.medication_history_select",
        "prq",
        "GET",
        "/PRQWeb/Page/JSP/UDC_Select.jsp",
        contract_required=False,
    ),
    _spec(
        "prq.medication_history",
        "prq",
        "POST",
        "/PRQWeb/QueryUdResult.do",
        retry_safe=True,
        query=("Use", "hhisnum", "hid"),
        form=("qryType", "udDate", "udStatus"),
        values=(("Use", "Dur"), ("qryType", "condition")),
        contract_required=True,
    ),
    _spec(
        "prq.numeric_history_page",
        "prq",
        "GET",
        "/PRQWeb/Page/JSP/TestReport_C.jsp",
        query=("Use", "hhisnum", "hid"),
        values=(("Use", "Dur"),),
        contract_required=False,
    ),
    _spec(
        "prq.numeric_history_select",
        "prq",
        "GET",
        "/PRQWeb/Page/JSP/TestReportC_Select.jsp",
        contract_required=False,
    ),
    _spec(
        "prq.numeric_history",
        "prq",
        "POST",
        "/PRQWeb/QueryResNumCenter.do",
        retry_safe=True,
        query=("Use", "hhisnum", "hid", "queryType"),
        form=("date", "dept", "ordersubtype"),
        values=(("Use", "Dur"), ("queryType", "condition")),
        contract_required=True,
    ),
    _spec(
        "prq.surgery_history_page",
        "prq",
        "GET",
        "/PRQWeb/Page/JSP/OpNote.jsp",
        query=("Use", "hhisnum", "hid"),
        values=(("Use", "Dur"),),
        contract_required=False,
    ),
    _spec(
        "prq.surgery_history_select",
        "prq",
        "GET",
        "/PRQWeb/Page/JSP/OpNote_Select.jsp",
        contract_required=False,
    ),
    _spec(
        "prq.surgery_history",
        "prq",
        "POST",
        "/PRQWeb/QueryOpNote.do",
        retry_safe=True,
        query=("Use", "hhisnum", "hid", "queryType"),
        form=("date",),
        values=(("Use", "Dur"), ("queryType", "condition")),
        contract_required=True,
    ),
    _spec(
        "prq.consults",
        "prq",
        "GET",
        "/PRQWeb/QueryClOrder.do",
        query=("Use", "caseNo", "caseType", "casedt", "hhisnum", "hid", "reqCode"),
        values=(("Use", "Case"), ("reqCode", "qryPCUClOrderList")),
        contract_required=True,
    ),
    _spec(
        "prq.treatments",
        "prq",
        "GET",
        "/PRQWeb/QueryTr.do",
        query=("Use", "caseNo", "caseType", "hhisnum", "hid", "queryType"),
        values=(("Use", "Case"), ("queryType", "case")),
        contract_required=True,
    ),
    _spec(
        "prq.order_detail",
        "prq",
        "GET",
        "/PRQWeb/QueryOrderDetail.do",
        query=("caseNo", "caseType", "hhisnum", "hid", "orRsType", "orStep", "seqNo", "source"),
        contract_required=True,
    ),
    _spec(
        "prq.order_report",
        "prq",
        "GET",
        "/PRQWeb/QueryReportByOrder.do",
        query=(
            "caseNo",
            "caseType",
            "hhisnum",
            "hid",
            "orDept",
            "orRsType",
            "orpfcode",
            "seqNo",
            "source",
        ),
        contract_required=True,
    ),
    _spec(
        "prq.pacs_study",
        "prq",
        "GET",
        "/PRQWeb/Adm_QueryPACS.do",
        query=("hhisnum", "hid", "reqCode", "reqno"),
        values=(("reqCode", "queryPACS"),),
        contract_required=True,
    ),
    _spec(
        "prq.pacs_image",
        "prq",
        "GET",
        "/PRQWeb/Page/JSP/showPACSPic.jsp",
        response_kind="bytes",
        query=("SERIES_UID", "STUDY_UID", "hhisnum", "hid", "reqno", "uid"),
        contract_required=True,
    ),
    _spec(
        "prq.pdf_attachment",
        "prq",
        "GET",
        "/PRQWeb/Page/JSP/showPDF.jsp",
        response_kind="bytes",
        query=("fileName",),
        contract_required=True,
    ),
    _spec("prq.opd_landing", "prq", "GET", "/PRQWeb/QueryOPDPatList.do", query=("docCode", "hid")),
    _spec(
        "prq.opd_patients",
        "prq",
        "POST",
        "/PRQWeb/QueryOPDPatList.do",
        retry_safe=True,
        form=("docCode", "hid", "opdDate"),
    ),
    _spec(
        "oppl.doctor_resolution",
        "oppl",
        "POST",
        "/OPPLWeb/surgAction.do",
        retry_safe=True,
        form=("cardno", "hid", "method"),
        values=(("method", "searchDr"),),
    ),
    _spec(
        "oppl.surgery_schedule",
        "oppl",
        "POST",
        "/OPPLWeb/surgAction.do",
        response_kind="json",
        retry_safe=True,
        form=("bgn", "drno", "end", "hid", "method"),
        values=(("method", "doSearchByConition"),),
    ),
    _spec(
        "audit.unsigned_records",
        "audit",
        "GET",
        "/PRQWeb/QueryRecordList.do",
        query=("bgnDt", "endDt", "qryStr", "qryType", "status", "statusType"),
    ),
)


# Additional September recordings have their own positive/negative regression
# corpus, independent of the original baseline HAR coverage requirements.
RECORDED_OPERATIONS = (
    _spec("mis.sso_logon", "mis", "GET", "/VGHK/WPSAutoLogon.asp", contract_required=False),
    *(
        _spec(f"mis.{key}", "mis", method, path, contract_required=False)
        for key, method, path in (
            ("single", "POST", "/VGHK/MIS_Por/MADSINGLE.ASP"),
            ("performance_entry", "POST", "/VGHK/PA_PMO003M.asp"),
            ("payroll_entry", "POST", "/VGHK/PA_PMO004M.asp"),
            ("password", "POST", "/VGHK/PAswd2db.asp"),
            ("password_page", "GET", "/VGHK/Pswdchk.asp"),
            ("main", "POST", "/VGHK/MAD/MADMAIN.ASP"),
            ("frame", "GET", "/ibi_apps/WFServlet"),
        )
    ),
    *(
        _spec(
            f"prq.{key}",
            "prq",
            "GET",
            "/PRQWeb/QueryMrData.do",
            response_kind="json",
            query=("hid", "hhisnum", "reqCode"),
            values=(("reqCode", code),),
            contract_required=False,
        )
        for key, code in (
            ("allergy", "getUdhcdsps"),
            ("advance_directives", "getAD"),
            ("research_flags", "getIrb"),
            ("bed_transfers", "getNextBed"),
        )
    ),
    _spec(
        "prq.care_cases",
        "prq",
        "GET",
        "/PRQWeb/QueryNISAction.do",
        response_kind="json",
        query=("hid", "hhisnum", "reqCode", "src", "usrId"),
        values=(("reqCode", "getCareCase"),),
        contract_required=False,
    ),
    _spec(
        "prq.text_report_history",
        "prq",
        "POST",
        "/PRQWeb/QueryResTextCenter.do",
        retry_safe=True,
        query=("hid", "hhisnum", "Use", "queryType", "dept", "reportType"),
        form=("date",),
        values=(("Use", "Dur"), ("queryType", "condition")),
        contract_required=False,
    ),
    _spec(
        "prq.text_report",
        "prq",
        "GET",
        "/PRQWeb/QueryResText.do",
        query=("hid", "hhisnum", "caseNo", "caseType", "seqNo", "orDept", "source"),
        contract_required=False,
    ),
    _spec(
        "prq.upload_types",
        "prq",
        "POST",
        "/PRQWeb/QueryUploadMR.do",
        response_kind="json",
        retry_safe=True,
        form=("reqCode",),
        values=(("reqCode", "queryUploadType"),),
        contract_required=False,
    ),
    _spec(
        "prq.upload_history",
        "prq",
        "POST",
        "/PRQWeb/MRUploadFile.do",
        retry_safe=True,
        form=("reqCode", "days", "mainType"),
        values=(("reqCode", "getUploadFile"),),
        contract_required=False,
    ),
    _spec(
        "oppl.sso_logon",
        "oppl",
        "POST",
        "/OPPLWeb/WPSAutoLogon",
        form=("HID", "uid", "ssID", "keyOne", "keyTwo", "keyThree", "targetURL"),
        contract_required=False,
    ),
    *(
        _spec(
            f"oppl.{key}",
            "oppl",
            "POST",
            "/OPPLWeb/surgAction.do",
            response_kind="json",
            retry_safe=True,
            form=("method", "hid", *fields),
            values=(("method", method),),
            contract_required=False,
        )
        for key, method, fields in (
            ("patient_info", "getPatInfo", ("hhisnum",)),
            ("request_numbers", "getReqnos", ("hhisnum",)),
            ("anticoagulants", "listAnticoagulant", ("hhisnum",)),
            ("sglt2", "listSGLT2", ("hhisnum",)),
            ("procedure_catalog", "getPfiles", ()),
            ("holidays", "getHolidays", ()),
            ("supply_model", "getNCModel", ("key", "sect")),
        )
    ),
    _spec(
        "oppl.patient_consents",
        "oppl",
        "POST",
        "/OPPLWeb/surgAction.do",
        response_kind="json",
        retry_safe=True,
        form=("method", "hid", "hhisnum", "type"),
        values=(("method", "getPatInfo"), ("type", "C")),
        contract_required=False,
    ),
    *(
        _spec(
            f"oppl.{key}",
            "oppl",
            "POST",
            "/OPPLWeb/surgConsentController.do",
            response_kind="json",
            retry_safe=True,
            form=("method", "hid", *fields),
            values=(("method", method),),
            contract_required=False,
        )
        for key, method, fields in (
            ("consent_catalog", "getOprpformName", ()),
            ("consent_template", "loadform", ("formname", "opdept")),
            ("consent_doctor", "searchDr", ("cardno",)),
        )
    ),
    _spec(
        "oppl.schedule_form",
        "oppl",
        "POST",
        "/OPPLWeb/jsp/surg/open.jsp",
        retry_safe=True,
        form=("orhisnum", "orcaseno"),
        contract_required=False,
    ),
    _spec(
        "oppl.consent_form",
        "oppl",
        "POST",
        "/OPPLWeb/jsp/consent/open.jsp",
        retry_safe=True,
        form=("hhisnum", "hid"),
        contract_required=False,
    ),
    *(
        _spec(
            f"oppl.{key}",
            "oppl",
            "POST",
            path,
            response_kind=kind,
            form=("method", "hid"),
            values=(("method", method),),
            contract_required=False,
            mutates=True,
        )
        for key, method, path, kind in (
            ("create_schedule", "doSave", "/OPPLWeb/surgAction.do", "json"),
            ("edit_schedule", "doEdit", "/OPPLWeb/surgAction.do", "text"),
            ("cancel_schedule", "doCancel", "/OPPLWeb/surgAction.do", "text"),
            ("create_consent", "doSave", "/OPPLWeb/surgConsentController.do", "text"),
        )
    ),
)
OPERATIONS += RECORDED_OPERATIONS
OPERATIONS += (
    _spec(
        "oppl_records.sso_logon",
        "oppl_records",
        "POST",
        "/OPPLWeb/WPSAutoLogon",
        contract_required=False,
    ),
    _spec(
        "oppl_records.departments",
        "oppl_records",
        "POST",
        "/OPPLWeb/qdataAction.do",
        response_kind="json",
        retry_safe=True,
        form=("method", "hid"),
        values=(("method", "getDeptList"),),
        contract_required=False,
    ),
    _spec(
        "oppl_records.cases",
        "oppl_records",
        "POST",
        "/OPPLWeb/qlogAction.do",
        response_kind="json",
        retry_safe=True,
        form=(
            "method",
            "hid",
            "doctVId",
            "guiDoctId",
            "opDept",
            "opdate",
            "opCode",
            "assDoct1",
            "assDoct2",
            "assDoct3",
            "assDoct4",
        ),
        values=(("method", "getQlog"),),
        contract_required=False,
    ),
    _spec(
        "oppl_records.note",
        "oppl_records",
        "POST",
        "/OPPLWeb/qlogAction.do",
        response_kind="json",
        retry_safe=True,
        form=("method", "hid", "hhisnum", "reqno", "seqno"),
        values=(("method", "getOpnotePDF"),),
        contract_required=False,
    ),
    # Viewer GET is present in the recorded page's button handler; binary
    # response remains to be verified by the new intranet test.
    _spec(
        "oppl_records.pdf",
        "oppl_records",
        "GET",
        "/OPPLWeb/jsp/pc/page/show/showPDF.jsp",
        response_kind="bytes",
        retry_safe=True,
        query=("file",),
        contract_required=False,
    ),
)
OPERATIONS += (
    *(
        _spec(
            f"review.{key}",
            "review",
            "GET",
            path,
            response_kind="json",
            retry_safe=True,
            contract_required=False,
        )
        for key, path in (
            ("login_info", "/Pck/Menu/GetLoginInfo"),
            ("options", "/Pck/PCKQ010/Selections"),
        )
    ),
    _spec(
        "review.doctors",
        "review",
        "POST",
        "/Pck/PCKQ010/ReadVSDrList",
        response_kind="json",
        retry_safe=True,
        form=("InsuSectNo",),
        contract_required=False,
    ),
    _spec(
        "review.cases",
        "review",
        "POST",
        "/Pck/PCKQ010/PckQ010Grid_Read",
        response_kind="json",
        retry_safe=True,
        # Every filter is optional on this unique path. ReviewCaseFilter
        # requires at least one; signature matching must accept each variant.
        contract_required=False,
    ),
    *(
        _spec(
            f"review.{key}",
            "review",
            "POST",
            f"/Pck/PCKC010/{endpoint}",
            response_kind="json",
            retry_safe=True,
            form=("ApplySeq",),
            contract_required=False,
        )
        for key, endpoint in (
            ("case_detail", "ReadPckC010"),
            ("orders", "PCKAPPLOGrid_Read"),
            ("attachments", "PCKAPPLAGrid_Read"),
            ("pacs", "PCKAPPLAPacsGrid_Read"),
        )
    ),
)
OPERATION_BY_KEY = {item.key: item for item in OPERATIONS}


def validate_operation_registry() -> None:
    """Fail at import/build time when structural endpoint metadata drifts."""

    if len(OPERATION_BY_KEY) != len(OPERATIONS):
        raise RuntimeError("operation registry contains duplicate keys")
    signatures: set[tuple[object, ...]] = set()
    allowed_values = {"method", "reqCode", "querymethod", "queryType", "qryType", "Use", "type"}
    for spec in OPERATIONS:
        if spec.method not in {"GET", "POST"}:
            raise RuntimeError(f"operation {spec.key} has an unsupported method")
        if spec.mutates and (spec.method != "POST" or spec.retry_safe):
            raise RuntimeError(f"mutation {spec.key} must be a non-retryable POST")
        if not spec.path.startswith("/") or "://" in spec.path:
            raise RuntimeError(f"operation {spec.key} has an unsafe endpoint path")
        if spec.response_kind not in {"text", "json", "bytes"}:
            raise RuntimeError(f"operation {spec.key} has an invalid response type")
        if any(key not in allowed_values for key, _ in spec.operation_values):
            raise RuntimeError(f"operation {spec.key} has a non-whitelisted constant")
        signature = (
            spec.app,
            spec.method,
            spec.path,
            spec.query_keys,
            spec.form_keys,
            spec.operation_values,
        )
        if signature in signatures:
            raise RuntimeError(f"operation {spec.key} duplicates a semantic signature")
        signatures.add(signature)


validate_operation_registry()


def operation_spec(key: str) -> OperationSpec:
    return OPERATION_BY_KEY[key]
