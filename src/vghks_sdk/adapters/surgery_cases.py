"""Completed surgery search, note discovery and PDF retrieval over Requests."""

from __future__ import annotations

from ..core.errors import ConfigurationError
from ..core.operations import operation_spec
from ..models import BinaryAsset
from ..models.surgery_cases import SurgeryCase, SurgeryCaseFilter, SurgeryCaseRef, SurgeryNoteRef
from ..parsing.assets import parse_binary_asset
from ..parsing.surgery_cases import (
    parse_surgery_cases,
    parse_surgery_departments,
    parse_surgery_note,
)
from ..runtime import SDKRuntime
from .oppl_headers import oppl_ajax_headers


class SurgeryCasesAdapter:
    def __init__(self, runtime: SDKRuntime) -> None:
        self.runtime = runtime

    def _json(self, key, fields, parser):
        spec = operation_spec(f"oppl_records.{key}")

        def operation():
            hid = self.runtime.auth.hid_for("oppl_records")
            base = self.runtime.settings.oppl_base_url.rstrip("/")
            headers = oppl_ajax_headers(base)
            headers["Referer"] = f"{base}/qlogAction.do?method=qLog"
            headers["Accept"] = "application/json, text/javascript, */*; q=0.01"
            value = self.runtime.request_json(
                spec,
                f"{base}/{spec.path.rsplit('/', 1)[-1]}",
                data={**fields, **dict(spec.operation_values), "hid": hid},
                headers=headers,
            )
            return parser(value)

        return self.runtime.execute(spec, operation, operation_name=spec.key)

    def get_case_departments(self) -> list[str]:
        return self._json("departments", {}, parse_surgery_departments)

    def get_cases(self, filter: SurgeryCaseFilter) -> list[SurgeryCase]:
        if not isinstance(filter, SurgeryCaseFilter):
            raise ConfigurationError("surgery query requires SurgeryCaseFilter")
        return self._json("cases", filter.to_form(), parse_surgery_cases)

    def get_record_ref(self, ref: SurgeryCaseRef) -> SurgeryNoteRef | None:
        if not isinstance(ref, SurgeryCaseRef):
            raise ConfigurationError("surgery record requires SurgeryCaseRef")
        return self._json(
            "note",
            {"hhisnum": ref.mrn, "reqno": ref.request_no, "seqno": ref.sequence_no},
            lambda payload: parse_surgery_note(payload, ref),
        )

    def download_record(self, ref: SurgeryNoteRef) -> BinaryAsset:
        if not isinstance(ref, SurgeryNoteRef):
            raise ConfigurationError("surgery PDF requires SurgeryNoteRef")
        spec = operation_spec("oppl_records.pdf")

        def operation():
            self.runtime.auth.ensure("oppl_records")
            base = self.runtime.settings.oppl_base_url.rstrip("/")
            content, _ = self.runtime.request_binary(
                spec,
                f"{base}/jsp/pc/page/show/showPDF.jsp",
                max_bytes=64 * 1024 * 1024,
                params={"file": ref.file_path},
                headers={
                    "Referer": f"{base}/qlogAction.do?method=qLog",
                    "Accept": "application/pdf,*/*;q=0.8",
                },
            )
            return parse_binary_asset(content, media_type="application/pdf")

        return self.runtime.execute(spec, operation, operation_name=spec.key)
