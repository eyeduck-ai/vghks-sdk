"""PRQ outpatient list, visit, SOAP, and numeric report adapter."""

from __future__ import annotations

import time
from datetime import date
from urllib.parse import parse_qsl, quote, urlencode

from bs4 import BeautifulSoup

from ..core.browser_headers import IMAGE_ACCEPT
from ..core.errors import ConfigurationError, ParseError
from ..core.operations import operation_spec
from ..models import (
    BinaryAsset,
    CaseDetail,
    ClinicalOrder,
    ConsultRecord,
    MedicationHistoryFilter,
    MedicationOrder,
    NumericHistoryFilter,
    NumericHistoryReport,
    NumericReport,
    OrderDetail,
    OrderDetailRef,
    OrderHistoryFilter,
    OrderReport,
    OrderReportRef,
    OutpatientPatient,
    PacsImageRef,
    PacsStudy,
    PacsStudyRef,
    PatientSurgeryRecord,
    PdfAttachmentRef,
    SoapRecord,
    SurgeryHistoryFilter,
    TreatmentRecord,
    VisitCase,
)
from ..parsing.assets import parse_binary_asset
from ..parsing.clinical import (
    parse_clinical_orders,
    parse_consults,
    parse_medication_orders,
    parse_numeric_history,
    parse_order_detail,
    parse_order_report,
    parse_pacs_study,
    parse_surgery_history,
    parse_treatments,
)
from ..parsing.prq import (
    parse_case_detail,
    parse_numeric_report,
    parse_opd_patients,
    parse_soap,
    parse_visit_cases,
)
from ..runtime import SDKRuntime
from .prq_extensions import PrqExtendedOperations

_OPD_LANDING = operation_spec("prq.opd_landing")
_OPD_PATIENTS = operation_spec("prq.opd_patients")
_PATIENT_CONTEXT = operation_spec("prq.patient_context")
_VISIT_CASES = operation_spec("prq.visit_cases")
_CASE_DETAIL = operation_spec("prq.case_detail")
_SOAP = operation_spec("prq.soap")
_KEY_PREFLIGHT = operation_spec("prq.key_preflight")
_NUMERIC_PAGE = operation_spec("prq.numeric_page")
_NUMERIC_SELECT = operation_spec("prq.numeric_select")
_NUMERIC = operation_spec("prq.numeric")
_PATIENT_HISTORY_CONTEXT = operation_spec("prq.patient_history_context")
_CASE_ORDERS_PAGE = operation_spec("prq.case_orders_page")
_CASE_ORDERS_SELECT = operation_spec("prq.case_orders_select")
_CASE_ORDERS = operation_spec("prq.case_orders")
_ORDER_HISTORY_PAGE = operation_spec("prq.order_history_page")
_ORDER_HISTORY_SELECT = operation_spec("prq.order_history_select")
_ORDER_HISTORY = operation_spec("prq.order_history")
_CASE_MEDICATIONS_PAGE = operation_spec("prq.case_medications_page")
_CASE_MEDICATIONS_SELECT = operation_spec("prq.case_medications_select")
_CASE_MEDICATIONS = operation_spec("prq.case_medications")
_MEDICATION_HISTORY_PAGE = operation_spec("prq.medication_history_page")
_MEDICATION_HISTORY_SELECT = operation_spec("prq.medication_history_select")
_MEDICATION_HISTORY = operation_spec("prq.medication_history")
_NUMERIC_HISTORY_PAGE = operation_spec("prq.numeric_history_page")
_NUMERIC_HISTORY_SELECT = operation_spec("prq.numeric_history_select")
_NUMERIC_HISTORY = operation_spec("prq.numeric_history")
_SURGERY_HISTORY_PAGE = operation_spec("prq.surgery_history_page")
_SURGERY_HISTORY_SELECT = operation_spec("prq.surgery_history_select")
_SURGERY_HISTORY = operation_spec("prq.surgery_history")
_CONSULTS = operation_spec("prq.consults")
_TREATMENTS = operation_spec("prq.treatments")
_ORDER_DETAIL = operation_spec("prq.order_detail")
_ORDER_REPORT = operation_spec("prq.order_report")
_PACS_STUDY = operation_spec("prq.pacs_study")
_PACS_IMAGE = operation_spec("prq.pacs_image")
_PDF_ATTACHMENT = operation_spec("prq.pdf_attachment")

_MAX_ASSET_BYTES = 64 * 1024 * 1024


class PrqAdapter(PrqExtendedOperations):
    def __init__(self, runtime: SDKRuntime) -> None:
        self.runtime = runtime
        self._opd_initialized: set[tuple[int, str]] = set()

    def get_doctor_patients(
        self,
        doctor_card: str,
        visit_date: date,
    ) -> list[OutpatientPatient]:
        def operation() -> list[OutpatientPatient]:
            hid = self.runtime.auth.hid_for("prq")
            base = self.runtime.settings.prq_base_url.rstrip("/")
            url = f"{base}/QueryOPDPatList.do"
            init_key = (self.runtime.auth.generation, doctor_card)
            if init_key not in self._opd_initialized:
                self.runtime.request_text(
                    _OPD_LANDING,
                    url,
                    params={"docCode": doctor_card, "hid": hid},
                )
                self._opd_initialized = {
                    item for item in self._opd_initialized if item[0] == init_key[0]
                }
                self._opd_initialized.add(init_key)
            html_text = self.runtime.request_text(
                _OPD_PATIENTS,
                url,
                data={
                    "docCode": doctor_card,
                    "hid": hid,
                    "opdDate": visit_date.isoformat(),
                },
            )
            patients = parse_opd_patients(
                html_text,
                visit_date=visit_date,
                doctor_card=doctor_card,
            )
            if not patients:
                soup = BeautifulSoup(html_text, "html.parser")
                if (
                    soup.find(attrs={"name": "docCode"}) is None
                    or soup.find(attrs={"name": "opdDate"}) is None
                ):
                    raise ParseError(
                        "OPD response did not contain the expected query structure",
                        code="PRQ_OPD_STRUCTURE_MISSING",
                    )
            return patients

        return self.runtime.execute(
            _OPD_PATIENTS,
            operation,
            operation_name="get_doctor_opd_patients",
        )

    def get_visit_cases(self, mrn: str) -> list[VisitCase]:
        def operation() -> list[VisitCase]:
            hid = self.runtime.auth.hid_for("prq")
            base = self.runtime.settings.prq_base_url.rstrip("/")
            self.runtime.request_text(
                _PATIENT_CONTEXT,
                f"{base}/QueryPatientRecord.do",
                params={"Use": "Case", "hid": hid},
                data={"id": mrn, "queryID": "", "queryPtID": mrn, "type": "1"},
            )
            html_text = self.runtime.request_text(
                _VISIT_CASES,
                f"{base}/QueryCaseList.do",
            )
            cases = parse_visit_cases(html_text, mrn)
            if not cases and BeautifulSoup(html_text, "html.parser").find(id="typeO") is None:
                raise ParseError(
                    "case-list response did not contain the expected structure",
                    code="PRQ_CASE_LIST_STRUCTURE_MISSING",
                )
            return cases

        return self.runtime.execute(
            _VISIT_CASES,
            operation,
            operation_name="get_visit_cases",
        )

    def get_case_detail(self, case: VisitCase) -> CaseDetail:
        return self.runtime.execute(
            _CASE_DETAIL,
            lambda: self._get_case_detail_raw(case),
            operation_name="get_case_detail",
        )

    def get_soap(self, case: VisitCase) -> SoapRecord:
        def operation() -> SoapRecord:
            self._get_case_detail_raw(case)
            self._prime_key_raw()
            hid = self.runtime.auth.hid_for("prq")
            base = self.runtime.settings.prq_base_url.rstrip("/")
            html_text = self.runtime.request_text(
                _SOAP,
                f"{base}/QueryBillingSOAP.do",
                params={
                    "reqCode": "qrySOAP",
                    "pdfFlg": "N",
                    "hhisnum": case.mrn,
                    "hid": hid,
                    "caseNo": case.case_no,
                    "casesec": case.section_code,
                    "casedt": case.visit_date.isoformat() if case.visit_date else "",
                    "casenoO": case.case_no,
                    "sectC": case.section_name,
                },
            )
            if BeautifulSoup(html_text, "html.parser").find(id="data") is None:
                raise ParseError(
                    "SOAP response did not contain its data container",
                    code="PRQ_SOAP_CONTAINER_MISSING",
                )
            return parse_soap(html_text, case)

        return self.runtime.execute(_SOAP, operation, operation_name="get_soap")

    def get_numeric_report(self, case: VisitCase) -> NumericReport:
        def operation() -> NumericReport:
            self._get_case_detail_raw(case)
            self._prime_key_raw()
            hid = self.runtime.auth.hid_for("prq")
            base = self.runtime.settings.prq_base_url.rstrip("/")
            common = {
                "caseNo": case.case_no,
                "caseType": case.case_type,
                "hhisnum": case.mrn,
                "hid": hid,
            }
            self.runtime.request_text(
                _NUMERIC_PAGE,
                f"{base}/Page/JSP/TestReport_A.jsp",
                params={"Use": "Case", **common},
            )
            self.runtime.request_text(
                _NUMERIC_SELECT,
                f"{base}/Page/JSP/TestReport_Select.jsp",
                params=common,
            )
            html_text = self.runtime.request_text(
                _NUMERIC,
                f"{base}/QueryResNumCenter.do",
                params={"Use": "Case", "queryType": "case", **common},
            )
            if BeautifulSoup(html_text, "html.parser").find(id="data") is None:
                raise ParseError(
                    "numeric-report response did not contain its data container",
                    code="PRQ_NUMERIC_CONTAINER_MISSING",
                )
            return parse_numeric_report(html_text, case)

        return self.runtime.execute(
            _NUMERIC,
            operation,
            operation_name="get_numeric_report",
        )

    def get_case_orders(self, case: VisitCase) -> list[ClinicalOrder]:
        self._validate_outpatient_case(case)

        def operation() -> list[ClinicalOrder]:
            self._get_case_detail_raw(case)
            self._prime_key_raw()
            hid = self.runtime.auth.hid_for("prq")
            base = self.runtime.settings.prq_base_url.rstrip("/")
            common = {
                "Use": "Case",
                "caseNo": case.case_no,
                "caseType": case.case_type,
                "hhisnum": case.mrn,
                "hid": hid,
            }
            self.runtime.request_text(
                _CASE_ORDERS_PAGE,
                f"{base}/Page/JSP/Order.jsp",
                params={
                    **common,
                    "casenoO": case.case_no,
                    "section": case.section_code,
                },
            )
            self.runtime.request_text(
                _CASE_ORDERS_SELECT,
                f"{base}/Page/JSP/Order_Select.jsp",
                params=common,
            )
            html_text = self.runtime.request_text(
                _CASE_ORDERS,
                f"{base}/QueryOrderResult.do",
                data={
                    **common,
                    "caseNoO": case.case_no,
                    "date": "0",
                    "ordersubtype": "*",
                    "ordertype": "*",
                    "orstepc": "*",
                    "section": case.section_code,
                },
            )
            self._assert_constructor_list(html_text, code="PRQ_CASE_ORDERS_STRUCTURE_MISSING")
            return parse_clinical_orders(html_text, mrn=case.mrn, case=case)

        return self.runtime.execute(
            _CASE_ORDERS,
            operation,
            operation_name="get_case_orders",
        )

    def get_order_history(
        self,
        mrn: str,
        history_filter: OrderHistoryFilter,
    ) -> list[ClinicalOrder]:
        def operation() -> list[ClinicalOrder]:
            self._history_context_raw(mrn)
            hid = self.runtime.auth.hid_for("prq")
            base = self.runtime.settings.prq_base_url.rstrip("/")
            common = {"Use": "Dur", "hhisnum": mrn, "hid": hid}
            self.runtime.request_text(
                _ORDER_HISTORY_PAGE,
                f"{base}/Page/JSP/OrderC.jsp",
                params=common,
            )
            self.runtime.request_text(
                _ORDER_HISTORY_SELECT,
                f"{base}/Page/JSP/OrderC_Select.jsp",
                params=common,
            )
            html_text = self.runtime.request_text(
                _ORDER_HISTORY,
                f"{base}/QueryOrderResult.do",
                data={
                    **common,
                    "date": str(history_filter.lookback_days),
                    "ordersubtype": history_filter.subtype,
                    "ordertype": history_filter.category,
                    "orstepc": history_filter.status,
                },
            )
            self._assert_constructor_list(html_text, code="PRQ_ORDER_HISTORY_STRUCTURE_MISSING")
            orders = parse_clinical_orders(html_text, mrn=mrn)
            if history_filter.order_date is not None:
                selected = history_filter.order_date.isoformat()
                orders = [item for item in orders if item.order_date.startswith(selected)]
            return orders

        return self.runtime.execute(
            _ORDER_HISTORY,
            operation,
            operation_name="get_order_history",
        )

    def get_case_medications(self, case: VisitCase) -> list[MedicationOrder]:
        self._validate_outpatient_case(case)

        def operation() -> list[MedicationOrder]:
            self._get_case_detail_raw(case)
            self._prime_key_raw()
            hid = self.runtime.auth.hid_for("prq")
            base = self.runtime.settings.prq_base_url.rstrip("/")
            common = {
                "Use": "Case",
                "caseNo": case.case_no,
                "caseType": case.case_type,
                "hhisnum": case.mrn,
                "hid": hid,
            }
            self.runtime.request_text(
                _CASE_MEDICATIONS_PAGE,
                f"{base}/Page/JSP/UD.jsp",
                params=common,
            )
            self.runtime.request_text(
                _CASE_MEDICATIONS_SELECT,
                f"{base}/Page/JSP/UD_Select.jsp",
                params=common,
            )
            html_text = self.runtime.request_text(
                _CASE_MEDICATIONS,
                f"{base}/QueryUdResult.do",
                params=common,
                data={"qryType": "case", "udDate": "0", "udStatus": "*"},
            )
            self._assert_constructor_list(
                html_text,
                code="PRQ_CASE_MEDICATIONS_STRUCTURE_MISSING",
            )
            return parse_medication_orders(html_text, mrn=case.mrn, case=case)

        return self.runtime.execute(
            _CASE_MEDICATIONS,
            operation,
            operation_name="get_case_medications",
        )

    def get_medication_history(
        self,
        mrn: str,
        history_filter: MedicationHistoryFilter,
    ) -> list[MedicationOrder]:
        def operation() -> list[MedicationOrder]:
            self._history_context_raw(mrn)
            hid = self.runtime.auth.hid_for("prq")
            base = self.runtime.settings.prq_base_url.rstrip("/")
            common = {"Use": "Dur", "hhisnum": mrn, "hid": hid}
            self.runtime.request_text(
                _MEDICATION_HISTORY_PAGE,
                f"{base}/Page/JSP/UDC.jsp",
                params=common,
            )
            self.runtime.request_text(
                _MEDICATION_HISTORY_SELECT,
                f"{base}/Page/JSP/UDC_Select.jsp",
                params=common,
            )
            html_text = self.runtime.request_text(
                _MEDICATION_HISTORY,
                f"{base}/QueryUdResult.do",
                params=common,
                data={
                    "qryType": "condition",
                    "udDate": str(history_filter.lookback_days),
                    "udStatus": history_filter.status,
                },
            )
            self._assert_constructor_list(
                html_text,
                code="PRQ_MEDICATION_HISTORY_STRUCTURE_MISSING",
            )
            medications = parse_medication_orders(html_text, mrn=mrn)
            if history_filter.order_date is not None:
                selected = history_filter.order_date.isoformat()
                medications = [item for item in medications if item.start_date.startswith(selected)]
            return medications

        return self.runtime.execute(
            _MEDICATION_HISTORY,
            operation,
            operation_name="get_medication_history",
        )

    def get_numeric_history(
        self,
        mrn: str,
        history_filter: NumericHistoryFilter,
    ) -> NumericHistoryReport:
        def operation() -> NumericHistoryReport:
            self._history_context_raw(mrn)
            hid = self.runtime.auth.hid_for("prq")
            base = self.runtime.settings.prq_base_url.rstrip("/")
            common = {"Use": "Dur", "hhisnum": mrn, "hid": hid}
            self.runtime.request_text(
                _NUMERIC_HISTORY_PAGE,
                f"{base}/Page/JSP/TestReport_C.jsp",
                params=common,
            )
            self.runtime.request_text(
                _NUMERIC_HISTORY_SELECT,
                f"{base}/Page/JSP/TestReportC_Select.jsp",
                params=common,
            )
            html_text = self.runtime.request_text(
                _NUMERIC_HISTORY,
                f"{base}/QueryResNumCenter.do",
                params={**common, "queryType": "condition"},
                data={
                    "date": str(history_filter.lookback_days),
                    "dept": history_filter.department,
                    "ordersubtype": history_filter.subtype,
                },
            )
            soup = BeautifulSoup(html_text, "html.parser")
            if soup.find(id="data") is None and "resnumTable" not in html_text:
                raise ParseError(
                    "numeric history response did not contain its data container",
                    code="PRQ_NUMERIC_HISTORY_STRUCTURE_MISSING",
                )
            return parse_numeric_history(html_text, mrn=mrn)

        return self.runtime.execute(
            _NUMERIC_HISTORY,
            operation,
            operation_name="get_numeric_history",
        )

    def get_surgery_history(
        self,
        mrn: str,
        history_filter: SurgeryHistoryFilter,
    ) -> list[PatientSurgeryRecord]:
        def operation() -> list[PatientSurgeryRecord]:
            self._history_context_raw(mrn)
            hid = self.runtime.auth.hid_for("prq")
            base = self.runtime.settings.prq_base_url.rstrip("/")
            common = {"Use": "Dur", "hhisnum": mrn, "hid": hid}
            self.runtime.request_text(
                _SURGERY_HISTORY_PAGE,
                f"{base}/Page/JSP/OpNote.jsp",
                params=common,
            )
            self.runtime.request_text(
                _SURGERY_HISTORY_SELECT,
                f"{base}/Page/JSP/OpNote_Select.jsp",
                params=common,
            )
            html_text = self.runtime.request_text(
                _SURGERY_HISTORY,
                f"{base}/QueryOpNote.do",
                params={**common, "queryType": "condition"},
                data={"date": str(history_filter.lookback_days)},
            )
            return parse_surgery_history(html_text, mrn=mrn)

        return self.runtime.execute(
            _SURGERY_HISTORY,
            operation,
            operation_name="get_surgery_history",
        )

    def get_consults(self, case: VisitCase) -> list[ConsultRecord]:
        self._validate_outpatient_case(case)

        def operation() -> list[ConsultRecord]:
            self._get_case_detail_raw(case)
            hid = self.runtime.auth.hid_for("prq")
            base = self.runtime.settings.prq_base_url.rstrip("/")
            html_text = self.runtime.request_text(
                _CONSULTS,
                f"{base}/QueryClOrder.do",
                params={
                    "Use": "Case",
                    "caseNo": case.case_no,
                    "caseType": case.case_type,
                    "casedt": case.visit_date.isoformat() if case.visit_date else "",
                    "hhisnum": case.mrn,
                    "hid": hid,
                    "reqCode": "qryPCUClOrderList",
                },
            )
            return parse_consults(html_text, case=case)

        return self.runtime.execute(_CONSULTS, operation, operation_name="get_consults")

    def get_treatments(self, case: VisitCase) -> list[TreatmentRecord]:
        self._validate_outpatient_case(case)

        def operation() -> list[TreatmentRecord]:
            self._get_case_detail_raw(case)
            hid = self.runtime.auth.hid_for("prq")
            base = self.runtime.settings.prq_base_url.rstrip("/")
            html_text = self.runtime.request_text(
                _TREATMENTS,
                f"{base}/QueryTr.do",
                params={
                    "Use": "Case",
                    "caseNo": case.case_no,
                    "caseType": case.case_type,
                    "hhisnum": case.mrn,
                    "hid": hid,
                    "queryType": "case",
                },
            )
            return parse_treatments(html_text, case=case)

        return self.runtime.execute(_TREATMENTS, operation, operation_name="get_treatments")

    def get_order_detail(self, reference: OrderDetailRef) -> OrderDetail:
        if not isinstance(reference, OrderDetailRef):
            raise ConfigurationError("order detail requires an OrderDetailRef")

        def operation() -> OrderDetail:
            hid = self.runtime.auth.hid_for("prq")
            base = self.runtime.settings.prq_base_url.rstrip("/")
            html_text = self.runtime.request_text(
                _ORDER_DETAIL,
                f"{base}/QueryOrderDetail.do",
                params={
                    "caseNo": reference.case_no,
                    "caseType": reference.case_type,
                    "hhisnum": reference.mrn,
                    "hid": hid,
                    "orRsType": reference.result_type,
                    "orStep": reference.order_step,
                    "seqNo": reference.sequence_no,
                    "source": reference.source,
                },
            )
            return parse_order_detail(html_text, reference=reference)

        return self.runtime.execute(
            _ORDER_DETAIL,
            operation,
            operation_name="get_order_detail",
        )

    def get_order_report(self, reference: OrderReportRef) -> OrderReport:
        if not isinstance(reference, OrderReportRef):
            raise ConfigurationError("order report requires an OrderReportRef")

        def operation() -> OrderReport:
            self._prime_key_raw()
            hid = self.runtime.auth.hid_for("prq")
            base = self.runtime.settings.prq_base_url.rstrip("/")
            html_text = self.runtime.request_text(
                _ORDER_REPORT,
                f"{base}/QueryReportByOrder.do",
                params={
                    "caseNo": reference.case_no,
                    "caseType": reference.case_type,
                    "hhisnum": reference.mrn,
                    "hid": hid,
                    "orDept": reference.department,
                    "orRsType": reference.result_type,
                    "orpfcode": reference.fee_code,
                    "seqNo": reference.sequence_no,
                    "source": reference.source,
                },
            )
            return parse_order_report(html_text, reference=reference)

        return self.runtime.execute(
            _ORDER_REPORT,
            operation,
            operation_name="get_order_report",
        )

    def get_pacs_study(self, reference: PacsStudyRef) -> PacsStudy:
        if not isinstance(reference, PacsStudyRef):
            raise ConfigurationError("PACS query requires a PacsStudyRef")

        def operation() -> PacsStudy:
            hid = self.runtime.auth.hid_for("prq")
            base = self.runtime.settings.prq_base_url.rstrip("/")
            html_text = self.runtime.request_text(
                _PACS_STUDY,
                f"{base}/Adm_QueryPACS.do",
                params={
                    "hhisnum": reference.mrn,
                    "hid": hid,
                    "reqCode": "queryPACS",
                    "reqno": reference.request_no,
                },
            )
            return parse_pacs_study(html_text, reference=reference)

        return self.runtime.execute(
            _PACS_STUDY,
            operation,
            operation_name="get_pacs_study",
        )

    def download_pacs_image(self, reference: PacsImageRef) -> BinaryAsset:
        if not isinstance(reference, PacsImageRef):
            raise ConfigurationError("PACS download requires a PacsImageRef")

        def operation() -> BinaryAsset:
            hid = self.runtime.auth.hid_for("prq")
            base = self.runtime.settings.prq_base_url.rstrip("/")
            content, _ = self.runtime.request_binary(
                _PACS_IMAGE,
                f"{base}/Page/JSP/showPACSPic.jsp",
                max_bytes=_MAX_ASSET_BYTES,
                headers={
                    "Accept": IMAGE_ACCEPT,
                    "Referer": f"{base}/Adm_QueryPACS.do?"
                    + urlencode(
                        {
                            "hhisnum": reference.mrn,
                            "hid": hid,
                            "reqCode": "queryPACS",
                            "reqno": reference.request_no,
                        }
                    ),
                },
                params={
                    "SERIES_UID": reference.series_uid,
                    "STUDY_UID": reference.study_uid,
                    "hhisnum": reference.mrn,
                    "hid": hid,
                    "reqno": reference.request_no,
                    "uid": reference.uid,
                },
            )
            return parse_binary_asset(content, media_type="image/jpeg")

        return self.runtime.execute(
            _PACS_IMAGE,
            operation,
            operation_name="download_pacs_image",
        )

    def download_pdf(self, reference: PdfAttachmentRef) -> BinaryAsset:
        if not isinstance(reference, PdfAttachmentRef):
            raise ConfigurationError("PDF download requires a PdfAttachmentRef")

        def operation() -> BinaryAsset:
            hid = self.runtime.auth.hid_for("prq")
            base = self.runtime.settings.prq_base_url.rstrip("/")
            # requests performs the second encoding while serializing params.
            encoded_once = quote(reference.file_path, safe="")
            content, _ = self.runtime.request_binary(
                _PDF_ATTACHMENT,
                f"{base}/Page/JSP/showPDF.jsp",
                max_bytes=_MAX_ASSET_BYTES,
                params={
                    "fileName": encoded_once,
                    "hhisnum": reference.mrn,
                    "hid": hid,
                },
            )
            return parse_binary_asset(content, media_type="application/pdf")

        return self.runtime.execute(
            _PDF_ATTACHMENT,
            operation,
            operation_name="download_pdf",
        )

    def _get_case_detail_raw(self, case: VisitCase) -> CaseDetail:
        hid = self.runtime.auth.hid_for("prq")
        params = dict(case.detail_params or {})
        params.update(
            {
                "hid": hid,
                "hhisnum": case.mrn,
                "caseType": case.case_type,
                "caseNo": case.case_no,
                "caseSec": case.section_code,
                "caseDT": case.visit_date.isoformat() if case.visit_date else "",
                "caseSectC": case.section_name,
                "index": str(case.index if case.index is not None else 0),
            }
        )
        html_text = self.runtime.request_text(
            _CASE_DETAIL,
            f"{self.runtime.settings.prq_base_url.rstrip('/')}/QueryCaseDetail.do",
            params=params,
        )
        soup = BeautifulSoup(html_text, "html.parser")
        if soup.find(id="tabs") is None and soup.find(id="tab_ul") is None:
            raise ParseError(
                "case-detail response did not contain the expected tabs",
                code="PRQ_CASE_DETAIL_TABS_MISSING",
            )
        return parse_case_detail(html_text, case)

    def _history_context_raw(self, mrn: str) -> None:
        hid = self.runtime.auth.hid_for("prq")
        base = self.runtime.settings.prq_base_url.rstrip("/")
        self.runtime.request_text(
            _PATIENT_HISTORY_CONTEXT,
            f"{base}/QueryPatientRecord.do",
            params={
                "Use": "Dur",
                "type": "1",
                "check": "Y",
                "hid": hid,
                "id": mrn,
            },
        )

    @staticmethod
    def _validate_outpatient_case(case: VisitCase) -> None:
        if case.case_type.strip().upper() != "O":
            raise ConfigurationError(
                "only outpatient case type O is supported",
                code="CASE_TYPE_UNSUPPORTED",
            )

    @staticmethod
    def _assert_constructor_list(html_text: str, *, code: str) -> None:
        if "aryCase" not in html_text and "new KSCase" not in html_text:
            raise ParseError("clinical list response structure was missing", code=code)

    def _prime_key_raw(self) -> None:
        base = self.runtime.settings.prq_base_url.rstrip("/")
        now = str(int(time.time() * 1000))
        text = self.runtime.request_text(
            _KEY_PREFLIGHT,
            f"{base}/GenerateKeyAction.do",
            params={"_": now, "ts": now, "reqCode": "getKeyStr"},
        )
        bundle = dict(parse_qsl(text.strip(), keep_blank_values=True))
        if not {"ssID", "keyOne", "keyTwo", "keyThree"}.issubset(bundle):
            raise ParseError(
                "key preflight response omitted required fields",
                code="PRQ_KEY_BUNDLE_MISSING",
            )
