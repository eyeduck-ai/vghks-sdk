"""PRQ outpatient lists, visits, SOAP and visit numeric reports."""

from __future__ import annotations

import re
from datetime import date

from bs4 import BeautifulSoup

from ..core.errors import AuthorizationError, ConfigurationError, ParseError
from ..core.operations import operation_spec
from ..identifiers import normalize_mrn, normalize_national_id
from ..models import CaseDetail, NumericReport, OutpatientPatient, SoapRecord, VisitCase
from ..parsing.prq import (
    parse_numeric_report,
    parse_opd_patients,
    parse_patient_identity,
    parse_soap,
    parse_visit_cases,
)
from ..parsing.prq_access_review import parse_patient_access_review_form
from .prq_context import _ACCESS_REVIEW, _CASE_DETAIL, PrqPatientContext

_OPD_LANDING = operation_spec("prq.opd_landing")


_OPD_PATIENTS = operation_spec("prq.opd_patients")


_PATIENT_IDENTITY = operation_spec("prq.patient_identity")


_VISIT_CASES = operation_spec("prq.visit_cases")


_SOAP = operation_spec("prq.soap")


_NUMERIC_PAGE = operation_spec("prq.numeric_page")


_NUMERIC_SELECT = operation_spec("prq.numeric_select")


_NUMERIC = operation_spec("prq.numeric")


class PrqVisitOperations(PrqPatientContext):
    _opd_initialized: set[tuple[int, str]]

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

    def get_visit_cases(
        self,
        mrn: str | None = None,
        *,
        national_id: str | None = None,
        access_review_reason: str | None = None,
    ) -> list[VisitCase]:
        if (mrn is None) == (national_id is None):
            raise ConfigurationError(
                "provide exactly one MRN or patient national ID", code="PATIENT_IDENTIFIER_REQUIRED"
            )
        mrn = normalize_mrn(mrn) if mrn is not None else ""
        national_id = normalize_national_id(national_id) if national_id is not None else ""
        if access_review_reason is not None and (
            national_id or not isinstance(access_review_reason, str)
            or not re.fullmatch(r"[A-Za-z0-9]{1,4}", access_review_reason)
        ):
            raise ConfigurationError(
                "an access-review reason requires an MRN and an offered reason code",
                code="PRQ_ACCESS_REVIEW_INPUT_INVALID",
            )

        def operation() -> list[VisitCase]:
            base = self.runtime.settings.prq_base_url.rstrip("/")
            self._establish_patient_context_raw(mrn, national_id, access_review_reason)
            resolved_mrn = mrn
            if national_id:
                patient_html = self.runtime.request_text(
                    _PATIENT_IDENTITY,
                    f"{base}/Page/JSP/KS_Patient.jsp",
                )
                resolved_mrn = parse_patient_identity(
                    patient_html,
                    expected_national_id=national_id,
                )
            html_text = self.runtime.request_text(
                _VISIT_CASES,
                f"{base}/QueryCaseList.do",
            )
            cases = parse_visit_cases(
                html_text,
                resolved_mrn,
                expected_national_id=national_id,
                allow_related_mrns=True,
            )
            if not cases and BeautifulSoup(html_text, "html.parser").find(id="typeO") is None:
                raise ParseError(
                    "case-list response did not contain the expected structure",
                    code="PRQ_CASE_LIST_STRUCTURE_MISSING",
                )
            return cases

        return self.runtime.execute(
            _ACCESS_REVIEW if access_review_reason is not None else _VISIT_CASES,
            operation,
            operation_name="get_visit_cases",
        )

    def get_case_detail(self, case: VisitCase) -> CaseDetail:
        return self.runtime.execute(
            _CASE_DETAIL,
            lambda: self._get_case_detail_raw(case),
            operation_name="get_case_detail",
        )

    def get_soap(
        self, case: VisitCase, *, access_review_reason: str | None = None
    ) -> SoapRecord:
        self._validate_outpatient_case(case)
        if access_review_reason is not None and (
            not isinstance(access_review_reason, str)
            or not re.fullmatch(r"[A-Za-z0-9]{1,4}", access_review_reason)
        ):
            raise ConfigurationError(
                "access-review reason must be an offered reason code",
                code="PRQ_ACCESS_REVIEW_INPUT_INVALID",
            )

        def operation() -> SoapRecord:
            if access_review_reason is not None:
                self._establish_patient_context_raw(
                    normalize_mrn(case.patient_mrn), "", access_review_reason
                )
            self._get_case_detail_raw(case)
            self._prime_key_raw()
            hid = self.runtime.auth.hid_for("prq")
            base = self.runtime.settings.prq_base_url.rstrip("/")
            params = {
                "reqCode": "qrySOAP",
                "pdfFlg": "N",
                "hhisnum": case.mrn,
                "hid": hid,
                "caseNo": case.case_no,
                "casesec": case.section_code,
                "casedt": case.visit_date.isoformat() if case.visit_date else "",
                "casenoO": case.case_no,
                "sectC": case.section_name,
            }
            soap_url = f"{base}/QueryBillingSOAP.do"
            html_text = self.runtime.request_text(_SOAP, soap_url, params=params)
            if BeautifulSoup(html_text, "html.parser").find(id="data") is None:
                review = parse_patient_access_review_form(
                    html_text,
                    expected_mrn=normalize_mrn(case.patient_mrn),
                    expected_hid=hid,
                )
                if review is not None:
                    self._submit_access_review_raw(review, access_review_reason)
                    html_text = self.runtime.request_text(_SOAP, soap_url, params=params)
                    if parse_patient_access_review_form(
                        html_text,
                        expected_mrn=normalize_mrn(case.patient_mrn),
                        expected_hid=hid,
                    ) is not None:
                        raise AuthorizationError(
                            "PRQ still requires patient-access review after submission",
                            code="PRQ_ACCESS_REVIEW_NOT_ACCEPTED",
                        )
            if BeautifulSoup(html_text, "html.parser").find(id="data") is None:
                raise ParseError(
                    "SOAP response did not contain its data container",
                    code="PRQ_SOAP_CONTAINER_MISSING",
                )
            return parse_soap(html_text, case)

        return self.runtime.execute(
            _ACCESS_REVIEW if access_review_reason is not None else _SOAP,
            operation,
            operation_name="get_soap",
        )

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
