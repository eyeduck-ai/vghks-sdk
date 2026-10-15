"""Additional patient queries from the September multi-tab recording."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from ..core.errors import ConfigurationError
from ..core.operations import operation_spec
from ..models import OrderReportRef, TextReportHistory, UploadHistory
from ..parsing.clinical import parse_order_report
from ..parsing.prq_extensions import (
    parse_patient_flags,
    parse_text_history,
    parse_upload_history,
    parse_upload_types,
)


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
            return parse_upload_types(data)

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
            self._establish_patient_context_raw(mrn, "", None)
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
