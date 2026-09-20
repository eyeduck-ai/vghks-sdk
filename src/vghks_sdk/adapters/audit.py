"""Audit unsigned-record endpoint adapter."""

from __future__ import annotations

from datetime import date

from bs4 import BeautifulSoup

from ..core.errors import ConfigurationError, ParseError
from ..core.operations import operation_spec
from ..models import UnsignedRecord
from ..parsing.audit import parse_unsigned_records
from ..runtime import SDKRuntime

_UNSIGNED = operation_spec("audit.unsigned_records")


class AuditAdapter:
    def __init__(self, runtime: SDKRuntime) -> None:
        self.runtime = runtime

    def get_unsigned_records(
        self,
        doctor_card: str,
        start: date,
        end: date,
    ) -> list[UnsignedRecord]:
        if end < start:
            raise ConfigurationError("end date must not be before start date")

        def operation() -> list[UnsignedRecord]:
            base = self.runtime.settings.audit_base_url.rstrip("/")
            html_text = self.runtime.request_text(
                _UNSIGNED,
                f"{base}/QueryRecordList.do",
                params={
                    "bgnDt": start.isoformat(),
                    "endDt": end.isoformat(),
                    "qryStr": doctor_card,
                    "qryStr2": "",
                    "qryStr3": "",
                    "qryType": "doc",
                    "status": "99",
                    "statusType": "V",
                    "type": "doc",
                },
            )
            soup = BeautifulSoup(html_text, "html.parser")
            if not any(
                soup.find("table", id=table_id) for table_id in ("dataTbl", "pgnTbl", "pgnTb2")
            ):
                raise ParseError(
                    "unsigned-record response omitted all expected tables",
                    code="AUDIT_UNSIGNED_TABLES_MISSING",
                )
            return parse_unsigned_records(html_text)

        return self.runtime.execute(
            _UNSIGNED,
            operation,
            operation_name="get_unsigned_records",
        )
