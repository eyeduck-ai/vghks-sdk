"""Shared patient/case context; raw helpers run inside Runtime.execute's operation lock."""

from __future__ import annotations

import time
from urllib.parse import parse_qsl

from bs4 import BeautifulSoup

from ..core.errors import (
    AccessReviewRequiredError,
    AuthorizationError,
    ConfigurationError,
    ParseError,
)
from ..core.operations import operation_spec
from ..identifiers import normalize_mrn
from ..models import CaseDetail, VisitCase
from ..parsing.prq import parse_case_detail, require_patient_context
from ..parsing.prq_access_review import PatientAccessReviewForm, parse_patient_access_review_form
from ..runtime import SDKRuntime

_PATIENT_CONTEXT = operation_spec("prq.patient_context")


_ACCESS_REVIEW = operation_spec("prq.access_review")


_CASE_DETAIL = operation_spec("prq.case_detail")


_KEY_PREFLIGHT = operation_spec("prq.key_preflight")


_PATIENT_HISTORY_CONTEXT = operation_spec("prq.patient_history_context")


_RECORDED_CARE_REASON = "1A"


class PrqPatientContext:
    """Raw helpers must be called within the caller's complete operation."""

    runtime: SDKRuntime

    def _establish_patient_context_raw(
        self,
        mrn: str,
        national_id: str,
        access_review_reason: str | None,
    ) -> None:
        hid = self.runtime.auth.hid_for("prq")
        base = self.runtime.settings.prq_base_url.rstrip("/")
        context_html = self.runtime.request_text(
            _PATIENT_CONTEXT,
            f"{base}/QueryPatientRecord.do",
            params={"Use": "Case", "hid": hid},
            data={
                "id": national_id or mrn,
                "queryID": national_id,
                "queryPtID": mrn,
                "type": "2" if national_id else "1",
            },
        )
        if national_id and BeautifulSoup(context_html, "html.parser").find("form", id="addForm"):
            raise AuthorizationError(
                "access review needs a verified patient MRN",
                code="PRQ_ACCESS_REVIEW_MRN_REQUIRED",
            )
        review = parse_patient_access_review_form(
            context_html,
            expected_mrn=mrn,
            expected_hid=hid,
        )
        if review is None:
            require_patient_context(context_html)
            return
        self._submit_access_review_raw(review, access_review_reason)

    def _submit_access_review_raw(
        self,
        review: PatientAccessReviewForm,
        reason: str | None = None,
    ) -> None:
        if self.runtime.operation_write_attempted:
            raise AuthorizationError(
                "PRQ still requires patient-access review after submission",
                code="PRQ_ACCESS_REVIEW_NOT_ACCEPTED",
            )
        selected = reason or _RECORDED_CARE_REASON
        if reason is None and selected not in review.offered_reasons:
            raise AccessReviewRequiredError(
                "the recorded clinical-care reason was not offered by PRQ"
            )
        base = self.runtime.settings.prq_base_url.rstrip("/")
        response = self.runtime.request_text(
            _ACCESS_REVIEW,
            f"{base}/EMRProcess.do",
            data=review.payload_for(selected),
            headers={"Referer": f"{base}/QueryPatientRecord.do"},
        )
        try:
            require_patient_context(response)
        except ParseError as exc:
            raise AuthorizationError(
                "PRQ did not confirm the access-review decision",
                code="PRQ_ACCESS_REVIEW_NOT_ACCEPTED",
            ) from exc

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
        review = parse_patient_access_review_form(
            html_text,
            expected_mrn=normalize_mrn(case.patient_mrn),
            expected_hid=hid,
        )
        if review is not None:
            self._submit_access_review_raw(review)
            html_text = self.runtime.request_text(
                _CASE_DETAIL,
                f"{self.runtime.settings.prq_base_url.rstrip('/')}/QueryCaseDetail.do",
                params=params,
            )
            if parse_patient_access_review_form(
                html_text,
                expected_mrn=normalize_mrn(case.patient_mrn),
                expected_hid=hid,
            ) is not None:
                raise AuthorizationError(
                    "PRQ still requires patient-access review after submission",
                    code="PRQ_ACCESS_REVIEW_NOT_ACCEPTED",
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
        html_text = self.runtime.request_text(
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
        review = parse_patient_access_review_form(
            html_text, expected_mrn=mrn, expected_hid=hid,
        )
        if review is not None:
            self._submit_access_review_raw(review)

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
