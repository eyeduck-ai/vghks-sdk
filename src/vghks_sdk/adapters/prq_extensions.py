"""Additional patient queries from the September multi-tab recording."""

from __future__ import annotations

import re
from html import unescape
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..core.errors import ConfigurationError, ParseError
from ..core.operations import operation_spec
from ..models import OrderReportRef, PdfAttachmentRef, TextReportHistory, UploadHistory
from ..parsing.clinical import _pdf_candidates, parse_order_report
from ..parsing.documents import parse_document


class PrqExtendedOperations:
    """Mixed into PrqAdapter so all patient/session state uses the same lock."""

    def _patient_flags(self, key: str, mrn: str) -> dict[str, Any]:
        spec = operation_spec(f"prq.{key}")

        def operation() -> dict[str, Any]:
            fields = {
                **dict(spec.operation_values),
                "hhisnum": mrn,
                "hid": self.runtime.auth.hid_for("prq"),
            }
            if key == "care_cases":
                fields.update(src="PRQHIS", usrId=self.runtime.auth.credentials.username)
            value = self.runtime.request_json(spec, self._extension_url(spec.path), params=fields)
            return parse_patient_flags(key, value, mrn)

        return self.runtime.execute(spec, operation, operation_name=spec.key)

    def get_allergy(self, mrn: str) -> dict[str, Any]:
        return self._patient_flags("allergy", mrn)

    def get_advance_directives(self, mrn: str) -> dict[str, Any]:
        return self._patient_flags("advance_directives", mrn)

    def get_research_flags(self, mrn: str) -> dict[str, Any]:
        return self._patient_flags("research_flags", mrn)

    def get_bed_transfers(self, mrn: str) -> dict[str, Any]:
        return self._patient_flags("bed_transfers", mrn)

    def get_care_cases(self, mrn: str) -> dict[str, Any]:
        return self._patient_flags("care_cases", mrn)

    def get_text_report_history(
        self, mrn: str, department: str, days: int = 3650
    ) -> TextReportHistory:
        if department not in {"PATH", "RAD", "CHK"} or not 1 <= days <= 9999:
            raise ConfigurationError(
                "invalid text report filter", code="TEXT_REPORT_FILTER_INVALID"
            )
        spec = operation_spec("prq.text_report_history")

        def operation() -> TextReportHistory:
            fields = {"date": str(days)}
            if department == "CHK":
                fields["OrMainType"] = "*"
            text = self.runtime.request_text(
                spec,
                self._extension_url(spec.path),
                params={
                    "Use": "Dur",
                    "hid": self.runtime.auth.hid_for("prq"),
                    "hhisnum": mrn,
                    "queryType": "condition",
                    "reportType": "T",
                    "dept": department,
                },
                data=fields,
            )
            return parse_text_history(text, mrn, department)

        return self.runtime.execute(spec, operation, operation_name=spec.key)

    def get_text_report(self, reference: OrderReportRef):
        spec = operation_spec("prq.text_report")

        def operation():
            self._prime_key_raw()
            text = self.runtime.request_text(
                spec,
                self._extension_url(spec.path),
                params={
                    "hid": self.runtime.auth.hid_for("prq"),
                    "hhisnum": reference.mrn,
                    "caseNo": reference.case_no,
                    "caseType": reference.case_type,
                    "seqNo": reference.sequence_no,
                    "orDept": reference.department,
                    "source": reference.source,
                },
            )
            return parse_order_report(text, reference=reference)

        return self.runtime.execute(spec, operation, operation_name=spec.key)

    def get_upload_types(self) -> list[dict[str, str]]:
        spec = operation_spec("prq.upload_types")

        def operation():
            data = self.runtime.request_json(
                spec, self._extension_url(spec.path), data=dict(spec.operation_values)
            )
            if not isinstance(data, list) or any(
                not isinstance(row, dict) or not {"maintp", "mainnm"}.issubset(row) for row in data
            ):
                raise ParseError("upload type schema changed", code="UPLOAD_TYPES_INVALID")
            return data

        return self.runtime.execute(spec, operation, operation_name=spec.key)

    def get_upload_history(self, mrn: str, main_type: str = "", days: str = "*") -> UploadHistory:
        if main_type not in {"", "ADM", "AGREE", "ER", "OHP", "OPD", "OTH"} or (
            days != "*" and (not days.isdecimal() or not 1 <= int(days) <= 9999)
        ):
            raise ConfigurationError("invalid upload history filter", code="UPLOAD_FILTER_INVALID")
        spec = operation_spec("prq.upload_history")

        def operation() -> UploadHistory:
            # This endpoint has no MRN parameter; re-establish current patient
            # inside the same operation lock on every call.
            context = operation_spec("prq.patient_context")
            self.runtime.request_text(
                context,
                self._extension_url(context.path),
                params={"Use": "Case", "hid": self.runtime.auth.hid_for("prq")},
                data={"id": mrn, "queryID": "", "queryPtID": mrn, "type": "1"},
            )
            text = self.runtime.request_text(
                spec,
                self._extension_url(spec.path),
                data={"reqCode": "getUploadFile", "days": days, "mainType": main_type},
            )
            return parse_upload_history(text, mrn)

        return self.runtime.execute(spec, operation, operation_name=spec.key)

    def _extension_url(self, path: str) -> str:
        base = urlsplit(self.runtime.settings.prq_base_url)
        return f"{base.scheme}://{base.netloc}{path}"


def parse_patient_flags(key: str, value: Any, mrn: str) -> dict[str, Any]:
    expected = {
        "allergy": ("hhisnum", "allergy", "allergyMsg"),
        "advance_directives": ("hhisnum", "adSign", "adSignMsg"),
        "research_flags": ("hhisnum", "irbtype"),
        "bed_transfers": ("hhisnum", "frontBed", "nextBed"),
        "care_cases": ("status", "caseList"),
    }[key.removeprefix("prq.")]
    if not isinstance(value, dict) or not all(name in value for name in expected):
        raise ParseError("patient query JSON schema changed", code="PATIENT_JSON_SHAPE_INVALID")
    if "hhisnum" in value and str(value["hhisnum"]) != mrn:
        raise ParseError("patient query returned another context", code="PATIENT_CONTEXT_MISMATCH")
    return value


def parse_text_history(text: str, mrn: str, department: str) -> TextReportHistory:
    refs = []
    for match in re.finditer(r"QueryResText\.do\?([^\s'\"<>]+)", unescape(text)):
        query = parse_qs(match.group(1))
        # The page includes a comment documenting QueryResText.do?source=...
        # It is not a report link and carries no record identifiers.
        if not {"caseNo", "caseType", "seqNo"}.issubset(query):
            continue

        def item(key: str, query=query) -> str:
            return query.get(key, [""])[0]

        if item("hhisnum") and item("hhisnum") != mrn:
            raise ParseError(
                "report link belongs to another patient", code="PATIENT_CONTEXT_MISMATCH"
            )
        refs.append(
            OrderReportRef(
                mrn,
                item("caseNo"),
                item("caseType"),
                item("seqNo"),
                department=item("orDept"),
                source=item("source"),
            )
        )
    if not refs and "height_bd" not in text:
        raise ParseError("text history structure missing", code="TEXT_HISTORY_STRUCTURE_MISSING")
    return TextReportHistory(mrn, department, parse_document(text), tuple(dict.fromkeys(refs)))


def parse_upload_history(text: str, mrn: str) -> UploadHistory:
    from bs4 import BeautifulSoup

    if BeautifulSoup(text, "html.parser").find(id="tbObj") is None:
        raise ParseError(
            "upload history structure missing", code="UPLOAD_HISTORY_STRUCTURE_MISSING"
        )
    refs = tuple(dict.fromkeys(PdfAttachmentRef(mrn, path) for path in _pdf_candidates(text)))
    return UploadHistory(mrn, parse_document(text), refs)
