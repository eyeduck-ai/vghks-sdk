from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

from ..adapters.protocols import OpplAdapterProtocol
from ..core.errors import ConfigurationError
from ..extension_protocols import SurgeryCasesProtocol
from ..models import (
    BinaryAsset,
    FormSnapshot,
    MutationReceipt,
    SurgeryCase,
    SurgeryCaseFilter,
    SurgeryCaseRef,
    SurgeryCommand,
    SurgeryNoteRef,
    SurgeryRecord,
)
from ..surgery_commands import prepare_command


class SurgeryService:
    def __init__(
        self, adapter: OpplAdapterProtocol, records: SurgeryCasesProtocol | None = None
    ) -> None:
        self._adapter = adapter
        self._records = records

    def _case_adapter(self) -> SurgeryCasesProtocol:
        if self._records is None:
            raise ConfigurationError("surgery record adapter is not configured")
        return self._records

    def get_case_departments(self) -> list[str]:
        return self._case_adapter().get_case_departments()

    def get_cases(self, filter: SurgeryCaseFilter) -> list[SurgeryCase]:
        return self._case_adapter().get_cases(filter)

    def get_record_ref(self, ref: SurgeryCaseRef) -> SurgeryNoteRef | None:
        return self._case_adapter().get_record_ref(ref)

    def download_record(self, ref: SurgeryNoteRef) -> BinaryAsset:
        return self._case_adapter().download_record(ref)

    def get_schedule(
        self, card_no: str, start: date, end: date, **filters: str
    ) -> list[SurgeryRecord]:
        return self._adapter.get_schedule(card_no, start, end, **filters)

    def get_patient_info(self, mrn: str) -> dict[str, Any]:
        return self._adapter.get_patient_info(mrn)

    def get_patient_consents(self, mrn: str) -> dict[str, Any]:
        return self._adapter.get_patient_consents(mrn)

    def get_request_numbers(self, mrn: str) -> dict[str, Any]:
        return self._adapter.get_request_numbers(mrn)

    def get_anticoagulants(self, mrn: str) -> dict[str, Any]:
        return self._adapter.get_anticoagulants(mrn)

    def get_sglt2(self, mrn: str) -> dict[str, Any]:
        return self._adapter.get_sglt2(mrn)

    def get_procedure_catalog(self) -> dict[str, Any]:
        return self._adapter.get_procedure_catalog()

    def get_holidays(self) -> dict[str, Any]:
        return self._adapter.get_holidays()

    def get_supply_model(self, key: str, department: str) -> dict[str, Any]:
        return self._adapter.get_supply_model(key, department)

    def get_consent_catalog(self) -> dict[str, Any]:
        return self._adapter.get_consent_catalog()

    def get_consent_template(self, name: str, department: str) -> dict[str, Any]:
        return self._adapter.get_consent_template(name, department)

    def resolve_consent_doctor(self, card_no: str) -> dict[str, Any]:
        return self._adapter.resolve_consent_doctor(card_no)

    def open_schedule_form(self, fields: Mapping[str, Any], *, mode: str = "edit") -> FormSnapshot:
        return self._adapter.open_schedule_form(fields, mode=mode)

    def open_consent_form(self, fields: Mapping[str, Any]) -> FormSnapshot:
        return self._adapter.open_consent_form(fields)

    @staticmethod
    def prepare_command(
        action: str, fields: Mapping[str, str] | Iterable[tuple[str, str]]
    ) -> SurgeryCommand:
        return prepare_command(action, fields)

    def create_schedule(self, command: SurgeryCommand) -> MutationReceipt:
        return self._submit(command, "create_schedule")

    def edit_schedule(self, command: SurgeryCommand) -> MutationReceipt:
        return self._submit(command, "edit_schedule")

    def cancel_schedule(self, command: SurgeryCommand) -> MutationReceipt:
        return self._submit(command, "cancel_schedule")

    def create_consent(self, command: SurgeryCommand) -> MutationReceipt:
        return self._submit(command, "create_consent")

    def _submit(self, command: SurgeryCommand, action: str) -> MutationReceipt:
        from ..core.errors import ConfigurationError

        if command.operation != f"oppl.{action}":
            raise ConfigurationError("command action mismatch", code="SURGERY_ACTION_MISMATCH")
        return self._adapter.submit_command(command)
