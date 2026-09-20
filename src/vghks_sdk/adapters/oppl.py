"""OPPL surgery endpoint adapter."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date
from typing import Any
from urllib.parse import urlsplit

from ..core.errors import ConfigurationError, ParseError, RequestError, SDKError
from ..core.operations import operation_spec
from ..models import FormSnapshot, MutationReceipt, SurgeryCommand, SurgeryRecord
from ..parsing.documents import parse_form
from ..parsing.oppl import parse_oppl_payload, parse_surgery_records
from ..runtime import SDKRuntime
from ..surgery_commands import prepare_command
from .oppl_headers import oppl_ajax_headers

_DOCTOR_RESOLUTION = operation_spec("oppl.doctor_resolution")
_SURGERY_SCHEDULE = operation_spec("oppl.surgery_schedule")


class OpplAdapter:
    def __init__(self, runtime: SDKRuntime) -> None:
        self.runtime = runtime
        self._attempted_commands: set[str] = set()

    def get_schedule(
        self,
        doctor_card: str,
        start: date,
        end: date,
        *,
        department: str = "OPH",
        mrn: str = "",
        room: str = "",
    ) -> list[SurgeryRecord]:
        if end < start:
            raise ConfigurationError("end date must not be before start date")

        def operation() -> list[SurgeryRecord]:
            hid = self.runtime.auth.hid_for("oppl")
            base = self.runtime.settings.oppl_base_url.rstrip("/")
            url = f"{base}/surgAction.do"
            doctor_response = self.runtime.request_text(
                _DOCTOR_RESOLUTION,
                url,
                data={"cardno": doctor_card, "hid": hid, "method": "searchDr"},
                headers=oppl_ajax_headers(base),
            ).strip()
            if not doctor_response:
                raise ParseError(
                    "surgery system did not resolve the doctor card",
                    code="OPPL_DOCTOR_RESOLUTION_EMPTY",
                )
            payload = self.runtime.request_json(
                _SURGERY_SCHEDULE,
                url,
                headers=oppl_ajax_headers(base),
                data={
                    "anevisit": "Y",
                    "bgn": start.isoformat(),
                    # searchDr returns a display name. The HAR's schedule POST
                    # uses the original physician card, not that response text.
                    "drno": doctor_card,
                    "end": end.isoformat(),
                    "hhisnum": mrn,
                    "hid": hid,
                    "method": "doSearchByConition",
                    "room": room,
                    "sect": department,
                    "wait": "Y",
                },
            )
            return parse_surgery_records(payload)

        return self.runtime.execute(
            _SURGERY_SCHEDULE,
            operation,
            operation_name="get_surgery_schedule",
        )

    def _json(self, key: str, fields: Mapping[str, str]) -> dict[str, Any]:
        spec = operation_spec(f"oppl.{key}")

        def operation() -> dict[str, Any]:
            payload = self.runtime.request_json(
                spec,
                self._url(spec.path),
                headers=oppl_ajax_headers(self.runtime.settings.oppl_base_url),
                data={
                    **fields,
                    **dict(spec.operation_values),
                    "hid": self.runtime.auth.hid_for("oppl"),
                },
            )
            return parse_oppl_payload(key, payload, fields.get("hhisnum", ""))

        return self.runtime.execute(spec, operation, operation_name=spec.key)

    def get_patient_info(self, mrn: str) -> dict[str, Any]:
        return self._json("patient_info", {"hhisnum": mrn})

    def get_patient_consents(self, mrn: str) -> dict[str, Any]:
        return self._json("patient_consents", {"hhisnum": mrn})

    def get_request_numbers(self, mrn: str) -> dict[str, Any]:
        return self._json("request_numbers", {"hhisnum": mrn})

    def get_anticoagulants(self, mrn: str) -> dict[str, Any]:
        return self._json("anticoagulants", {"hhisnum": mrn})

    def get_sglt2(self, mrn: str) -> dict[str, Any]:
        return self._json("sglt2", {"hhisnum": mrn})

    def get_procedure_catalog(self) -> dict[str, Any]:
        return self._json("procedure_catalog", {})

    def get_holidays(self) -> dict[str, Any]:
        return self._json("holidays", {})

    def get_supply_model(self, key: str, department: str) -> dict[str, Any]:
        return self._json("supply_model", {"key": key, "sect": department})

    def get_consent_catalog(self) -> dict[str, Any]:
        return self._json("consent_catalog", {})

    def get_consent_template(self, name: str, department: str) -> dict[str, Any]:
        return self._json(
            "consent_template",
            {"formname": name, "opdept": department},
        )

    def resolve_consent_doctor(self, card_no: str) -> dict[str, Any]:
        return self._json("consent_doctor", {"cardno": card_no})

    def open_schedule_form(self, fields: Mapping[str, Any], *, mode: str = "edit") -> FormSnapshot:
        statuses = {"create": "S", "edit": "E", "cancel": "C"}
        if mode not in statuses:
            raise ConfigurationError("unknown surgery form mode", code="SURGERY_FORM_MODE_INVALID")
        if mode != "create" and not fields.get("orreqno"):
            raise ConfigurationError(
                "existing schedule reference required", code="SURGERY_FORM_REFERENCE_MISSING"
            )
        return self._open_form("schedule_form", {**fields, "status": statuses[mode]}, "openForm")

    def open_consent_form(self, fields: Mapping[str, Any]) -> FormSnapshot:
        return self._open_form("consent_form", fields, "saveConsent")

    def _open_form(self, key: str, fields: Mapping[str, Any], identifier: str) -> FormSnapshot:
        spec = operation_spec(f"oppl.{key}")

        def operation() -> FormSnapshot:
            mrn = str(fields.get("orhisnum" if key == "schedule_form" else "hhisnum", ""))
            if not mrn:
                raise ConfigurationError(
                    "patient reference required", code="SURGERY_FORM_REFERENCE_MISSING"
                )
            # JSPs depend on the current patient. Rebind it while holding the
            # same operation lock, even when another query changed it earlier.
            context_key = "patient_info" if key == "schedule_form" else "patient_consents"
            context_spec = operation_spec(f"oppl.{context_key}")
            context = self.runtime.request_json(
                context_spec,
                self._url(context_spec.path),
                data={
                    **dict(context_spec.operation_values),
                    "hhisnum": mrn,
                    "hid": self.runtime.auth.hid_for("oppl"),
                },
                headers=oppl_ajax_headers(self.runtime.settings.oppl_base_url),
            )
            parse_oppl_payload(context_key, context, mrn)

            # jQuery's form encoding flattens the selected JSON schedule record.
            def flatten(value: Any, prefix: str) -> list[tuple[str, str]]:
                if isinstance(value, Mapping):
                    return [
                        pair
                        for key, item in value.items()
                        for pair in flatten(item, f"{prefix}[{key}]")
                    ]
                if isinstance(value, list):
                    return [
                        pair
                        for index, item in enumerate(value)
                        for pair in flatten(item, f"{prefix}[{index}]")
                    ]
                return [(prefix, "" if value is None else str(value))]

            payload = [
                pair
                for key, value in fields.items()
                if key not in {"hid", "HID", "method"}
                for pair in flatten(value, key)
            ]
            payload.append(("hid", self.runtime.auth.hid_for("oppl")))
            html = self.runtime.request_text(
                spec,
                self._url(spec.path),
                data=payload,
                headers=oppl_ajax_headers(self.runtime.settings.oppl_base_url),
            )
            return parse_form(html, identifier)

        return self.runtime.execute(spec, operation, operation_name=spec.key)

    def submit_command(self, command: SurgeryCommand) -> MutationReceipt:
        # Revalidate even if a caller constructed the dataclass directly.
        checked = prepare_command(command.operation, command.fields)
        spec = operation_spec(checked.operation)

        def operation() -> MutationReceipt:
            if command.command_id in self._attempted_commands:
                raise ConfigurationError(
                    "command was already attempted; query its result before preparing another",
                    code="MUTATION_ALREADY_ATTEMPTED",
                )
            hid = self.runtime.auth.hid_for("oppl")
            self._attempted_commands.add(command.command_id)
            payload = [*checked.fields, ("hid", hid), *spec.operation_values]
            try:
                response = self.runtime.request_response(
                    spec,
                    self._url(spec.path),
                    data=payload,
                    headers=oppl_ajax_headers(self.runtime.settings.oppl_base_url),
                )
                if response.status_code != 200:
                    raise ParseError(
                        "write did not return an acknowledgment", code="MUTATION_ACK_MISSING"
                    )
                text = self.runtime.transport.text(response).strip()
                result = json.loads(text) if spec.response_kind == "json" else text
                acknowledged = mutation_acknowledged(spec.key, result)
                if not acknowledged:
                    raise ParseError(
                        "write acknowledgment not recognized", code="MUTATION_ACK_MISSING"
                    )
                return MutationReceipt(spec.key, command.command_id, "ACKNOWLEDGED", result)
            except (SDKError, ValueError) as exc:
                raise RequestError(
                    "write outcome is unknown; query the current record before any new submission",
                    code="MUTATION_OUTCOME_UNKNOWN",
                ).with_context(operation=spec.key, app="oppl") from exc

        return self.runtime.execute(spec, operation, operation_name=spec.key)

    def _url(self, path: str) -> str:
        base = urlsplit(self.runtime.settings.oppl_base_url)
        return f"{base.scheme}://{base.netloc}{path}"


def mutation_acknowledged(key: str, payload: Any) -> bool:
    if key == "oppl.create_schedule":
        return (
            isinstance(payload, dict)
            and payload.get("msg") == "手術排程新增成功!!!"
            and bool(payload.get("orreqno"))
        )
    if key == "oppl.create_consent":
        return payload == "Y"
    expected = {
        "oppl.edit_schedule": "手術排程編輯成功!!!",
        "oppl.cancel_schedule": "手術排程取消成功!!!",
    }.get(key)
    if not expected or not isinstance(payload, str):
        return False
    # Some HAR exporters decode UTF-8 text/plain as Windows-1252, including
    # undefined control bytes. Recognize only the exact observed success text.
    mojibake = "".join(
        bytes([byte]).decode("cp1252", errors="replace")
        if byte not in {0x81, 0x8D, 0x8F, 0x90, 0x9D}
        else chr(byte)
        for byte in expected.encode("utf-8")
    )
    return payload in {expected, mojibake, expected.encode("utf-8").decode("latin1")}
