"""Offline semantic contracts for authorized HAR archives.

The report surface is intentionally count-only.  Request values, hosts,
filenames, response text, and parsed clinical values never leave this module.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlsplit

from bs4 import BeautifulSoup

from ..core.errors import ConfigurationError, ParseError, error_code
from ..core.operations import OPERATION_BY_KEY, OperationSpec
from ..models import OrderDetailRef, OrderReportRef, PacsStudyRef, VisitCase
from ..parsing import (
    parse_clinical_orders,
    parse_consults,
    parse_medication_orders,
    parse_numeric_history,
    parse_numeric_report,
    parse_opd_patients,
    parse_order_detail,
    parse_order_report,
    parse_pacs_study,
    parse_registration_records,
    parse_surgery_history,
    parse_surgery_records,
    parse_treatments,
    parse_unsigned_records,
    parse_visit_cases,
)
from ..parsing.portal import parse_login_target

_OPERATION_KEYS = frozenset(
    {"method", "ordertype", "reqCode", "querymethod", "queryType", "qryType", "Use", "type"}
)
_SYNTHETIC_CASE = VisitCase(
    mrn="CONTRACT",
    visit_date=date(2000, 1, 1),
    case_type="O",
    case_no="CONTRACT",
    section_code="CONTRACT",
    section_name="CONTRACT",
)


@dataclass(frozen=True, slots=True)
class HarEntry:
    method: str
    path: str
    query_keys: frozenset[str]
    form_keys: frozenset[str]
    operation_values: Mapping[str, str]
    response_status: int
    response_body: bytes | None
    response_mime_type: str
    response_text: str | None = None
    context_mrn: str = field(default="", repr=False)

    @property
    def has_body(self) -> bool:
        return bool(self.response_body)

    def text(self) -> str:
        if self.response_text is not None:
            return self.response_text
        body = self.response_body or b""
        declared = _declared_charset(self.response_mime_type)
        if declared in {"big5", "big-5", "cp950", "ms950"}:
            candidates = ("cp950", "big5", "utf-8")
        elif declared in {"utf-8", "utf8"}:
            candidates = ("utf-8", "cp950")
        elif declared:
            candidates = (declared, "utf-8", "cp950")
        else:
            candidates = ("utf-8", "cp950")
        tried: set[str] = set()
        for encoding in candidates:
            if encoding.casefold() in tried:
                continue
            tried.add(encoding.casefold())
            try:
                return body.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                continue
        return body.decode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class HarArchive:
    entries: tuple[HarEntry, ...]


@dataclass(frozen=True, slots=True)
class HarSignature:
    method: str
    path: str
    query_keys: frozenset[str] = frozenset()
    form_keys: frozenset[str] = frozenset()
    operation_values: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_operation(cls, spec: OperationSpec) -> HarSignature:
        return cls(
            method=spec.method,
            path=spec.path,
            query_keys=spec.query_keys,
            form_keys=spec.form_keys,
            operation_values=spec.operation_values,
        )

    def matches(self, entry: HarEntry) -> bool:
        return (
            entry.method == self.method
            and entry.path == self.path
            and self.query_keys.issubset(entry.query_keys)
            and self.form_keys.issubset(entry.form_keys)
            and all(
                entry.operation_values.get(key) == value for key, value in self.operation_values
            )
        )


@dataclass(frozen=True, slots=True)
class HarContract:
    name: str
    operation_key: str
    signature: HarSignature
    validator: Callable[[HarEntry], None]
    minimum_baseline_matches: int = 1


@dataclass(frozen=True, slots=True)
class HarContractResult:
    contract: str
    matched_count: int
    passed_count: int
    status: str
    error_codes: tuple[str, ...] = ()

    @property
    def error_code(self) -> str:
        return self.error_codes[0] if self.error_codes else ""


@dataclass(frozen=True, slots=True)
class HarCheckReport:
    schema_version: int
    status: str
    archive_count: int
    entry_count: int
    contracts: tuple[HarContractResult, ...]

    @property
    def ok(self) -> bool:
        return self.status == "OK"


def load_har(path: Path) -> HarArchive:
    """Load one HAR 1.2 archive without retaining its filename in the result."""

    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ConfigurationError(
            "unable to read HAR input",
            code="HAR_INPUT_READ_FAILED",
            operation="har.load",
            app="local",
            cause_type=exc.__class__.__name__,
        ) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError(
            "HAR input is not valid UTF-8 JSON",
            code="HAR_INPUT_INVALID_JSON",
            operation="har.load",
            app="local",
            cause_type=exc.__class__.__name__,
        ) from exc
    if not isinstance(document, Mapping):
        raise ConfigurationError(
            "HAR input root must be an object",
            code="HAR_ROOT_INVALID",
            operation="har.load",
            app="local",
        )
    log = document.get("log")
    if not isinstance(log, Mapping):
        raise ConfigurationError(
            "HAR input does not contain a log object",
            code="HAR_LOG_MISSING",
            operation="har.load",
            app="local",
        )
    version = str(log.get("version", ""))
    if version != "1.2":
        raise ConfigurationError(
            "HAR input must use HAR version 1.2",
            code="HAR_VERSION_UNSUPPORTED",
            operation="har.load",
            app="local",
        )
    raw_entries = log.get("entries")
    if not isinstance(raw_entries, list):
        raise ConfigurationError(
            "HAR input does not contain an entries array",
            code="HAR_ENTRIES_MISSING",
            operation="har.load",
            app="local",
        )

    entries: list[HarEntry] = []
    for raw_entry in raw_entries:
        entries.append(_load_entry(raw_entry))
    return HarArchive(entries=tuple(entries))


def evaluate_har_contracts(
    archives: Iterable[HarArchive], *, require_baseline: bool
) -> HarCheckReport:
    archive_list = tuple(archives)
    entries = tuple(entry for archive in archive_list for entry in archive.entries)
    results: list[HarContractResult] = []
    contracts = BASELINE_CONTRACTS

    for contract in contracts:
        matched = tuple(entry for entry in entries if contract.signature.matches(entry))
        if not matched:
            if require_baseline:
                results.append(
                    HarContractResult(
                        contract.name,
                        0,
                        0,
                        "MISSING",
                        ("CONTRACT_MISSING",),
                    )
                )
            continue
        candidates = tuple(
            entry for entry in matched if 200 <= entry.response_status < 300 and entry.has_body
        )
        passed = 0
        failed_codes: set[str] = set()
        for entry in candidates:
            try:
                contract.validator(entry)
            except Exception as exc:
                failed_codes.add(error_code(exc))
            else:
                passed += 1
        if failed_codes:
            status, codes = "ERROR", tuple(sorted(failed_codes))
        elif not candidates:
            status, codes = "ERROR", ("NO_VALIDATABLE_RESPONSE",)
        elif require_baseline and len(matched) < contract.minimum_baseline_matches:
            status, codes = "MISSING", ("CONTRACT_COVERAGE_MISSING",)
        else:
            status, codes = "OK", ()
        results.append(
            HarContractResult(
                contract=contract.name,
                matched_count=len(matched),
                passed_count=passed,
                status=status,
                error_codes=codes,
            )
        )

    if not require_baseline and not results:
        results.append(
            HarContractResult(
                contract="recognized_contracts",
                matched_count=0,
                passed_count=0,
                status="ERROR",
                error_codes=("NO_RECOGNIZED_CONTRACT",),
            )
        )
    if require_baseline:
        category_error = _order_category_distinction_error(entries)
        if category_error:
            results = [
                replace(
                    result,
                    status="ERROR",
                    error_codes=tuple(sorted({*result.error_codes, category_error})),
                )
                if result.contract == "prq_order_history"
                else result
                for result in results
            ]
    status = "OK" if results and all(item.status == "OK" for item in results) else "ERROR"
    return HarCheckReport(
        schema_version=2,
        status=status,
        archive_count=len(archive_list),
        entry_count=len(entries),
        contracts=tuple(results),
    )


def _order_category_distinction_error(entries: tuple[HarEntry, ...]) -> str:
    identities: dict[str, set[tuple[str, ...]]] = {"*": set(), "OR": set()}
    matched_categories: set[str] = set()
    for entry in entries:
        if (
            entry.path != "/PRQWeb/QueryOrderResult.do"
            or entry.operation_values.get("Use") != "Dur"
            or entry.operation_values.get("ordertype") not in identities
            or not entry.has_body
            or not entry.context_mrn
        ):
            continue
        category = entry.operation_values["ordertype"]
        matched_categories.add(category)
        try:
            rows = parse_clinical_orders(entry.text(), mrn=entry.context_mrn)
        except Exception:
            return "ORDER_CATEGORY_COMPARISON_INVALID"
        identities[category].update(row.identity for row in rows)
    if matched_categories != {"*", "OR"}:
        return "ORDER_CATEGORY_FIXTURE_MISSING"
    if identities["*"] == identities["OR"]:
        return "ORDER_CATEGORY_RESULTS_NOT_DISTINCT"
    return ""


def _load_entry(raw_entry: Any) -> HarEntry:
    if not isinstance(raw_entry, Mapping):
        raise ConfigurationError("HAR entry must be an object")
    request = raw_entry.get("request")
    response = raw_entry.get("response")
    if not isinstance(request, Mapping) or not isinstance(response, Mapping):
        raise ConfigurationError("HAR entry requires request and response objects")
    method = str(request.get("method", "")).upper()
    url = str(request.get("url", ""))
    parsed_url = urlsplit(url)
    path = parsed_url.path
    if not method or not path:
        raise ConfigurationError("HAR request requires method and URL path")

    query_pairs = _name_value_pairs(request.get("queryString"))
    if not query_pairs and parsed_url.query:
        query_pairs = [
            (str(key), str(value))
            for key, value in parse_qsl(parsed_url.query, keep_blank_values=True)
        ]
    post_data = request.get("postData")
    form_pairs: list[tuple[str, str]] = []
    if isinstance(post_data, Mapping):
        form_pairs = _name_value_pairs(post_data.get("params"))
        if (
            not form_pairs
            and "x-www-form-urlencoded" in str(post_data.get("mimeType", "")).casefold()
        ):
            form_pairs = [
                (str(key), str(value))
                for key, value in parse_qsl(str(post_data.get("text", "")), keep_blank_values=True)
            ]
    operation_values: dict[str, str] = {}
    for key, value in (*query_pairs, *form_pairs):
        if key in _OPERATION_KEYS:
            operation_values[key] = value

    try:
        status = int(response.get("status", 0))
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("HAR response status must be an integer") from exc
    content = response.get("content")
    body: bytes | None = None
    decoded_text: str | None = None
    mime_type = ""
    if isinstance(content, Mapping):
        mime_type = str(content.get("mimeType", ""))
        text_value = content.get("text")
        if isinstance(text_value, str):
            if str(content.get("encoding", "")).casefold() == "base64":
                try:
                    body = base64.b64decode("".join(text_value.split()), validate=True)
                except (ValueError, binascii.Error) as exc:
                    raise ConfigurationError("HAR response contains invalid base64") from exc
            else:
                decoded_text = text_value
                body = text_value.encode("utf-8")
    return HarEntry(
        method=method,
        path=path,
        query_keys=frozenset(key for key, _ in query_pairs),
        form_keys=frozenset(key for key, _ in form_pairs),
        operation_values=operation_values,
        response_status=status,
        response_body=body,
        response_mime_type=mime_type,
        response_text=decoded_text,
        context_mrn=next((value for key, value in (*query_pairs, *form_pairs)
                          if key in {"hhisnum", "patno", "queryPtID"} and value), ""),
    )


def _name_value_pairs(value: Any) -> list[tuple[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigurationError("HAR parameter collection must be an array")
    pairs: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping) or "name" not in item:
            raise ConfigurationError("HAR parameter must contain a name")
        pairs.append((str(item["name"]), str(item.get("value", ""))))
    return pairs


def _declared_charset(mime_type: str) -> str:
    match = re.search(r"charset\s*=\s*['\"]?([^;'\"\s]+)", mime_type, re.I)
    return match.group(1).casefold() if match else ""


def _require(condition: bool, message: str) -> None:
    if not condition:
        normalized = re.sub(r"[^A-Z0-9]+", "_", message.upper()).strip("_")
        raise ParseError(message, code=f"HAR_{normalized}"[:80])


def _soup(entry: HarEntry) -> BeautifulSoup:
    text = entry.text()
    _require(bool(text.strip()), "contract response was empty")
    lowered = text.casefold()
    _require("syserrorexception" not in lowered, "contract response was an error page")
    return BeautifulSoup(text, "html.parser")


def _validate_login(entry: HarEntry) -> None:
    parse_login_target(entry.text())


def _validate_session(entry: HarEntry) -> None:
    text = entry.text().strip()
    _require(bool(text), "session response was empty")
    _require("mpassword" not in text.casefold(), "session response was a login page")


def _validate_sso_form(entry: HarEntry) -> None:
    form = _soup(entry).find("form")
    _require(form is not None, "SSO form was missing")
    values = {
        str(node.get("name")): str(node.get("value", ""))
        for node in form.find_all("input")
        if node.get("name")
    }
    required = {"HID", "ssID", "keyOne", "keyTwo", "keyThree", "targetURL"}
    _require(required.issubset(values), "SSO fields were missing")
    _require(all(values[key] for key in required), "SSO fields were blank")
    _require(
        any(values.get(key) for key in ("uid", "stUsrId", "USR_ID")),
        "SSO user field was missing",
    )


def _validate_rsa_bridge(entry: HarEntry) -> None:
    bundle = dict(parse_qsl(entry.text().strip(), keep_blank_values=True))
    required = {"ssID", "keyOne", "keyTwo", "keyThree"}
    _require(required.issubset(bundle), "bridge key bundle was missing")
    _require(all(bundle[key] for key in required), "bridge key bundle was blank")


def _validate_demographics(entry: HarEntry) -> None:
    try:
        payload = json.loads(entry.text())
    except json.JSONDecodeError as exc:
        raise ParseError(
            "demographic response was not JSON",
            code="HAR_DEMOGRAPHICS_JSON_INVALID",
        ) from exc
    rows = [payload] if isinstance(payload, Mapping) else payload
    _require(isinstance(rows, list) and bool(rows), "demographic rows were missing")
    _require(all(isinstance(row, Mapping) for row in rows), "demographic row shape changed")
    _require(
        any("patno" in row and "patname" in row for row in rows),
        "demographic keys changed",
    )


def _validate_registration_landing(entry: HarEntry) -> None:
    soup = _soup(entry)
    form = soup.find("form", id="RSV11WForm") or soup.find("form")
    _require(form is not None, "registration form was missing")
    _require(
        form.find("input", attrs={"name": "org.apache.struts.taglib.html.TOKEN"}) is not None,
        "registration token was missing",
    )


def _validate_registration_query(entry: HarEntry) -> None:
    soup = _soup(entry)
    _require(soup.find("table", id="row") is not None, "registration table was missing")
    _require(
        soup.find(attrs={"name": "displaytagPageSizeSelector"}) is not None
        or any("d-" in str(link.get("href", "")) for link in soup.find_all("a")),
        "registration pagination structure was missing",
    )
    parse_registration_records(entry.text())


def _validate_patient_context(entry: HarEntry) -> None:
    text = entry.text()
    _require(bool(text.strip()), "patient context response was empty")
    _require("syserrorexception" not in text.casefold(), "patient context returned an error")


def _validate_case_list(entry: HarEntry) -> None:
    soup = _soup(entry)
    _require(soup.find(id="typeO") is not None, "outpatient case container was missing")
    _require(
        bool(parse_visit_cases(entry.text(), entry.context_mrn)),
        "case links were not parsed",
    )


def _validate_case_detail(entry: HarEntry) -> None:
    soup = _soup(entry)
    _require(
        soup.find(id="tabs") is not None or soup.find(id="tab_ul") is not None,
        "case tabs were missing",
    )


def _validate_soap(entry: HarEntry) -> None:
    soup = _soup(entry)
    _require(soup.find(id="data") is not None, "SOAP data container was missing")
    _require(bool(soup.select("#data .soap pre")), "SOAP blocks were missing")


def _validate_numeric_case(entry: HarEntry) -> None:
    soup = _soup(entry)
    _require(soup.find(id="data") is not None, "numeric data container was missing")
    _require(
        bool(parse_numeric_report(entry.text(), _SYNTHETIC_CASE).tables),
        "numeric tables were missing",
    )


def _validate_numeric_duration(entry: HarEntry) -> None:
    soup = _soup(entry)
    _require(soup.find(id="data") is not None, "duration report data was missing")
    _require(soup.find(id="Chart") is not None, "duration report chart was missing")


def _validate_opd(entry: HarEntry) -> None:
    soup = _soup(entry)
    _require(soup.find(attrs={"name": "docCode"}) is not None, "OPD doctor field was missing")
    _require(soup.find(attrs={"name": "opdDate"}) is not None, "OPD date field was missing")
    rows = parse_opd_patients(entry.text(), visit_date=date(2000, 1, 1), doctor_card="CONTRACT")
    _require(bool(rows), "OPD patient rows were not parsed")


def _validate_doctor_resolution(entry: HarEntry) -> None:
    _require(bool(entry.text().strip()), "doctor resolution response was empty")


def _validate_surgery(entry: HarEntry) -> None:
    try:
        payload = json.loads(entry.text())
    except json.JSONDecodeError as exc:
        raise ParseError(
            "surgery response was not JSON",
            code="HAR_SURGERY_JSON_INVALID",
        ) from exc
    parse_surgery_records(payload)


def _validate_audit(entry: HarEntry) -> None:
    soup = _soup(entry)
    for table_id in ("dataTbl", "pgnTbl", "pgnTb2"):
        _require(soup.find("table", id=table_id) is not None, "audit table was missing")
    parse_unsigned_records(entry.text())


def _validate_orders(entry: HarEntry) -> None:
    text = entry.text()
    _require("aryCase" in text or "new KSCase" in text, "order list structure was missing")
    parse_clinical_orders(text, mrn=(entry.context_mrn or "CONTRACT"))


def _validate_medications(entry: HarEntry) -> None:
    text = entry.text()
    _require(
        "aryCase" in text or "new KSCase" in text,
        "medication list structure was missing",
    )
    parse_medication_orders(text, mrn=(entry.context_mrn or "CONTRACT"))


def _validate_numeric_history(entry: HarEntry) -> None:
    soup = _soup(entry)
    _require(
        soup.find(id="data") is not None or "resnumTable" in entry.text(),
        "numeric history structure was missing",
    )
    parse_numeric_history(entry.text(), mrn=(entry.context_mrn or "CONTRACT"))


def _validate_surgery_history(entry: HarEntry) -> None:
    parse_surgery_history(entry.text(), mrn=(entry.context_mrn or "CONTRACT"))


def _validate_consults(entry: HarEntry) -> None:
    case = VisitCase((entry.context_mrn or "CONTRACT"), date(2000, 1, 1), "O", "CONTRACT", "", "")
    parse_consults(entry.text(), case=case)


def _validate_treatments(entry: HarEntry) -> None:
    case = VisitCase((entry.context_mrn or "CONTRACT"), date(2000, 1, 1), "O", "CONTRACT", "", "")
    parse_treatments(entry.text(), case=case)


def _validate_order_detail(entry: HarEntry) -> None:
    reference = OrderDetailRef((entry.context_mrn or "CONTRACT"), "CONTRACT", "O", "1")
    detail = parse_order_detail(entry.text(), reference=reference)
    _require(bool(detail.fields), "order detail fields were missing")


def _validate_order_report(entry: HarEntry) -> None:
    reference = OrderReportRef((entry.context_mrn or "CONTRACT"), "CONTRACT", "O", "1")
    report = parse_order_report(entry.text(), reference=reference)
    _require(bool(report.fields), "order report fields were missing")
    _require(len(report.pdf_refs) == 2, "order report PDF references changed")


def _validate_pacs_study(entry: HarEntry) -> None:
    soup = _soup(entry)
    image = soup.find("img", src=True)
    _require(image is not None, "PACS image list was missing")
    query = parse_qs(urlsplit(str(image.get("src"))).query, keep_blank_values=True)
    request_no = query.get("reqno", [""])[-1]
    _require(bool(request_no), "PACS request reference was missing")
    study = parse_pacs_study(
        entry.text(),
        reference=PacsStudyRef((entry.context_mrn or "CONTRACT"), request_no),
    )
    _require(len(study.images) == 7, "PACS image count changed")


def _validate_pacs_image(entry: HarEntry) -> None:
    body = entry.response_body or b""
    _require(body.startswith(b"\xff\xd8"), "PACS JPEG SOI was missing")
    _require(body.endswith(b"\xff\xd9"), "PACS JPEG EOI was missing")
    _require(len(body) <= 64 * 1024 * 1024, "PACS JPEG exceeded size limit")


def _validate_pdf_negative_fixture(entry: HarEntry) -> None:
    body = entry.response_body or b""
    _require(not body.startswith(b"%PDF"), "PDF negative fixture unexpectedly became binary")
    _require("about:blank" in entry.text().casefold(), "PDF negative fixture changed")


def _contract(
    name: str,
    operation_key: str,
    validator: Callable[[HarEntry], None],
    *,
    minimum_baseline_matches: int = 1,
) -> HarContract:
    return HarContract(
        name=name,
        operation_key=operation_key,
        signature=HarSignature.from_operation(OPERATION_BY_KEY[operation_key]),
        validator=validator,
        minimum_baseline_matches=minimum_baseline_matches,
    )


BASELINE_CONTRACTS: tuple[HarContract, ...] = (
    _contract("portal_login_target", "portal.login", _validate_login),
    _contract("portal_session_check", "portal.session_check", _validate_session),
    _contract(
        "portal_sso_hidden_fields",
        "portal.sso_from_dn",
        _validate_sso_form,
        minimum_baseline_matches=3,
    ),
    _contract(
        "sectord_webmaas_key_bridge",
        "sectord.key_bridge",
        _validate_rsa_bridge,
    ),
    _contract(
        "webmaas_demographics",
        "webmaas.demographics",
        _validate_demographics,
    ),
    _contract(
        "webmaas_registration_landing",
        "webmaas.registration_landing",
        _validate_registration_landing,
    ),
    _contract(
        "webmaas_registration_query",
        "webmaas.registration_query",
        _validate_registration_query,
    ),
    _contract(
        "prq_patient_context",
        "prq.patient_context",
        _validate_patient_context,
    ),
    _contract("prq_visit_case_list", "prq.visit_cases", _validate_case_list),
    _contract("prq_case_detail", "prq.case_detail", _validate_case_detail),
    _contract("prq_soap", "prq.soap", _validate_soap),
    _contract("prq_numeric_case_get", "prq.numeric", _validate_numeric_case),
    _contract("prq_numeric_duration_post", "prq.numeric_duration", _validate_numeric_duration),
    _contract("prq_case_orders", "prq.case_orders", _validate_orders),
    _contract("prq_order_history", "prq.order_history", _validate_orders),
    _contract("prq_case_medications", "prq.case_medications", _validate_medications),
    _contract(
        "prq_medication_history",
        "prq.medication_history",
        _validate_medications,
    ),
    _contract("prq_numeric_history", "prq.numeric_history", _validate_numeric_history),
    _contract("prq_surgery_history", "prq.surgery_history", _validate_surgery_history),
    _contract("prq_consults", "prq.consults", _validate_consults),
    _contract("prq_treatments", "prq.treatments", _validate_treatments),
    _contract("prq_order_detail", "prq.order_detail", _validate_order_detail),
    _contract("prq_order_report", "prq.order_report", _validate_order_report),
    _contract("prq_pacs_study", "prq.pacs_study", _validate_pacs_study),
    _contract(
        "prq_pacs_jpeg",
        "prq.pacs_image",
        _validate_pacs_image,
        minimum_baseline_matches=7,
    ),
    _contract(
        "prq_pdf_negative_fixture",
        "prq.pdf_attachment",
        _validate_pdf_negative_fixture,
    ),
    _contract("prq_opd_landing", "prq.opd_landing", _validate_opd),
    _contract("prq_opd_patient_list", "prq.opd_patients", _validate_opd),
    _contract("oppl_doctor_resolution", "oppl.doctor_resolution", _validate_doctor_resolution),
    _contract("oppl_surgery_schedule", "oppl.surgery_schedule", _validate_surgery),
    _contract("audit_unsigned_records", "audit.unsigned_records", _validate_audit),
)


def validate_contract_coverage() -> None:
    required = {spec.key for spec in OPERATION_BY_KEY.values() if spec.contract_required}
    covered = {contract.operation_key for contract in BASELINE_CONTRACTS}
    if covered != required:
        missing = ",".join(sorted(required - covered))
        extra = ",".join(sorted(covered - required))
        raise RuntimeError(f"HAR contract coverage mismatch (missing={missing}; extra={extra})")


validate_contract_coverage()
