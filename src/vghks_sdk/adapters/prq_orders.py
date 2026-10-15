"""PRQ case and historical clinical lists with their original request sequences."""

from __future__ import annotations

from bs4 import BeautifulSoup

from ..core.errors import ParseError
from ..core.operations import operation_spec
from ..models import (
    ClinicalOrder,
    ConsultRecord,
    MedicationHistoryFilter,
    MedicationOrder,
    NumericHistoryFilter,
    NumericHistoryReport,
    OrderHistoryFilter,
    PatientSurgeryRecord,
    SurgeryHistoryFilter,
    TreatmentRecord,
    VisitCase,
)
from ..parsing.clinical import (
    parse_clinical_orders,
    parse_consults,
    parse_medication_orders,
    parse_numeric_history,
    parse_surgery_history,
    parse_treatments,
)
from .prq_context import PrqPatientContext

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


class PrqOrderOperations(PrqPatientContext):
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
