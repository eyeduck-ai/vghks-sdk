"""Replay recorded response bytes through production parsers, never HTTP.

Only structural outcomes/counts escape this module. Original request values
are used in memory to reconstruct parser context, not exported in reports.
"""

from __future__ import annotations

import base64
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from bs4 import BeautifulSoup

from .. import parsing
from ..adapters.oppl import mutation_acknowledged
from ..adapters.prq_extensions import parse_text_history, parse_upload_history
from ..contracts.check import discover_har_files
from ..contracts.har import BASELINE_CONTRACTS, HarEntry, HarSignature, load_har
from ..core.errors import ParseError, error_code
from ..core.network_errors import recorded_network_error_code
from ..core.operations import OPERATION_BY_KEY, OPERATIONS
from ..local_io import write_json_atomic
from ..models import (
    OrderDetailRef,
    OrderReport,
    OrderReportRef,
    PacsStudyRef,
    SurgeryCaseRef,
    TextReportHistory,
    UploadHistory,
    VisitCase,
)
from ..models.review import ReviewCasePart, ReviewCaseRef
from ..parsing.assets import parse_binary_asset
from ..parsing.documents import parse_form
from ..parsing.oppl import OPPL_JSON_FIELDS, parse_oppl_payload
from ..parsing.review import (
    parse_login_info,
    parse_review_case,
    parse_review_cases,
    parse_review_doctors,
    parse_review_options,
    parse_review_part,
)
from ..parsing.surgery_cases import (
    parse_surgery_cases,
    parse_surgery_departments,
    parse_surgery_note,
)
from ..queries import QUERY_BY_KEY
from .bundle import BundleReader

_CONTRACTS = {contract.operation_key: contract for contract in BASELINE_CONTRACTS}


def request_parts(request: Mapping[str, Any]) -> tuple[str, str, dict[str, str], dict[str, str]]:
    url = urlsplit(str(request.get("url", "")))
    query = dict(parse_qsl(url.query, keep_blank_values=True))
    unprepared = request.get("unprepared_kwargs") or {}
    params = unprepared.get("params", {})
    if isinstance(params, dict):
        query.update({str(key): str(value) for key, value in params.items()})
    body = request.get("body") or {}
    form: dict[str, str] = {}
    if isinstance(body.get("value"), dict):
        form = {str(key): str(value) for key, value in body["value"].items()}
    else:
        text = body.get("text")
        if text is None and body.get("base64"):
            text = base64.b64decode(body["base64"], validate=True).decode("utf-8")
        if isinstance(text, str):
            form = dict(parse_qsl(text, keep_blank_values=True))
    return str(request.get("method", "")).upper(), url.path, query, form


def identify_operation(request: Mapping[str, Any]) -> str:
    method, path, query, form = request_parts(request)
    entry = HarEntry(
        method, path, frozenset(query), frozenset(form), {**query, **form}, 0, None, ""
    )
    matched = [spec for spec in OPERATIONS if HarSignature.from_operation(spec).matches(entry)]
    if not matched:
        return ""
    matched.sort(
        key=lambda spec: len(spec.query_keys) + len(spec.form_keys) + len(spec.operation_values),
        reverse=True,
    )
    return matched[0].key


def replay_bundle(reader: BundleReader) -> list[dict[str, Any]]:
    summary = reader.json("run_summary.json")
    context_mrn = str(summary.get("test_mrn") or "")
    context_national_id = ""
    results: list[dict[str, Any]] = []
    exchanges = [
        row for row in reader.jsonl("capture_manifest.jsonl")
        if row.get("kind") in {"HTTP_EXCHANGE", "NETWORK_ERROR"}
    ]
    for sequence, row in enumerate(exchanges, 1):
        request = row.get("request") or {}
        _, path, query, form = request_parts(request)
        params = {**query, **form}
        if path.endswith("/QueryPatientRecord.do"):
            context_national_id = (
                (params.get("queryID") or params.get("id", "")) if params.get("type") == "2" else ""
            )
            context_mrn = (
                "" if context_national_id else params.get("queryPtID") or params.get("id", "")
            )
        operation = identify_operation(request)
        capture_id = str(row.get("capture_id", ""))
        # Never copy arbitrary labels from a returned file into safe reports.
        if not capture_id.isascii() or not capture_id.isdigit() or len(capture_id) > 12:
            capture_id = f"entry-{sequence:06d}"
        response = row.get("response") or {}
        result = {
            "capture_id": capture_id,
            "operation": operation,
            "status": "SKIPPED",
            "error_code": "",
            "record_count": None,
        }
        probe = (
            str(row.get("live_test_step", "")).startswith("network.")
            or row.get("connection_probe") is True
        )
        result["preflight"] = probe
        probe_name = str(row.get("live_test_step", ""))
        result["probe"] = (
            probe_name
            if re.fullmatch(
                r"network\.(portal|prq|sectord|webmaas|oppl|oppl_records|review|audit|mis)\.https(_direct)?(_tls12(_compat)?)?(_unverified)?",
                probe_name,
            )
            else ""
        )
        if row.get("kind") == "NETWORK_ERROR":
            result.update(
                status="NETWORK_ERROR",
                error_code=recorded_network_error_code(row.get("error") or {}),
            )
        elif probe:
            # A response from an unauthenticated base URL proves reachability,
            # including 401/403/404; it is not a failed clinical operation.
            result.update(status="PROBE_HTTP", operation="")
        elif not 200 <= int(response.get("status_code", 0)) < 300:
            status = int(response.get("status_code", 0))
            result.update(
                status="REDIRECT" if 300 <= status < 400 else "HTTP_ERROR",
                error_code="" if 300 <= status < 400 else f"HTTP_{status}",
            )
        elif (
            operation in QUERY_BY_KEY
            or operation in _CONTRACTS
            or operation == "prq.patient_identity"
        ):
            body_path = row.get("response_file")
            if not body_path:
                result.update(status="UNAVAILABLE", error_code="RESPONSE_BODY_MISSING")
            else:
                content = reader.read(body_path)
                headers = response.get("headers", [])
                mime = next(
                    (str(value) for key, value in headers if str(key).lower() == "content-type"), ""
                )
                result.update(
                    replay_response(
                        operation,
                        content,
                        params,
                        mime=mime,
                        context_mrn=context_mrn,
                        context_national_id=context_national_id,
                    )
                )
                if operation == "prq.patient_identity":
                    context_mrn = (
                        parsing.parse_patient_identity(
                            HarEntry(
                                "", "", frozenset(), frozenset(), {}, 200, content, mime
                            ).text()
                        )
                        if result["status"] == "PARSED"
                        else ""
                    )
        results.append(result)
    # A later HTTP response in the same transport retry group proves recovery
    # of the connection failure. Keep that failure as evidence, not a new root
    # cause. A later HTTP or parser error still remains an independent problem.
    responded_groups: set[str] = set()
    for recorded, result in zip(reversed(exchanges), reversed(results)):
        group = recorded.get("request_group_id")
        if not isinstance(group, str) or not group:
            continue
        if recorded.get("kind") == "HTTP_EXCHANGE":
            responded_groups.add(group)
        elif recorded.get("will_retry") is True and group in responded_groups:
            result["recovered"] = True
    return results


def replay_hars(input_path: Path, *, output_path: Path) -> dict[str, Any]:
    files, _ = discover_har_files(input_path)
    rows = []
    for archive_index, path in enumerate(files, 1):
        archive = load_har(path)
        raw_entries = json.loads(path.read_text(encoding="utf-8-sig"))["log"]["entries"]
        context_mrn = ""
        context_national_id = ""
        for index, (entry, raw) in enumerate(zip(archive.entries, raw_entries), 1):
            original = raw["request"]
            post = original.get("postData") or {}
            body = post.get("text") or urlencode(
                [(item["name"], item.get("value", "")) for item in post.get("params", [])]
            )
            request = {"method": original["method"], "url": original["url"], "body": {"text": body}}
            operation = identify_operation(request)
            _, request_path, query, form = request_parts(request)
            params = {**query, **form}
            if request_path.endswith("/QueryPatientRecord.do"):
                context_national_id = (
                    (params.get("queryID") or params.get("id", ""))
                    if params.get("type") == "2"
                    else ""
                )
                context_mrn = (
                    "" if context_national_id else params.get("queryPtID") or params.get("id", "")
                )
            if params.get("hhisnum") or params.get("patno"):
                context_mrn = params.get("hhisnum") or params["patno"]
            if operation not in QUERY_BY_KEY and not (
                operation
                and (OPERATION_BY_KEY[operation].mutates or operation == "prq.patient_identity")
            ):
                continue
            if not 200 <= entry.response_status < 300:
                location = next(
                    (
                        header.get("value", "")
                        for header in raw["response"].get("headers", [])
                        if header.get("name", "").lower() == "location"
                    ),
                    "",
                )
                login_redirect = (
                    operation == "review.login_info"
                    and entry.response_status == 302
                    and urlsplit(location).path == "/Pck/HISLogin"
                )
                result = {
                    "status": "EXPECTED_NEGATIVE" if login_redirect else "HTTP_ERROR",
                    "error_code": "REVIEW_LOGIN_REQUIRED"
                    if login_redirect
                    else f"HTTP_{entry.response_status}",
                    "record_count": None,
                }
            else:
                mime = (
                    "text/html; charset=utf-8"
                    if entry.response_text is not None
                    else entry.response_mime_type
                )
                result = replay_response(
                    operation,
                    entry.response_body or b"",
                    params,
                    mime=mime,
                    context_mrn=context_mrn,
                    context_national_id=context_national_id,
                )
                if operation == "prq.patient_identity":
                    context_mrn = (
                        parsing.parse_patient_identity(entry.text())
                        if result["status"] == "PARSED"
                        else ""
                    )
                if (
                    operation == "prq.pdf_attachment"
                    and result["error_code"] == "PDF_BINARY_INVALID"
                ):
                    try:
                        _CONTRACTS[operation].validator(entry)
                    except Exception:
                        pass
                    else:
                        result["status"] = "EXPECTED_NEGATIVE"
            rows.append(
                {
                    "capture_id": f"har-{archive_index:03d}-{index:06d}",
                    "operation": operation,
                    **result,
                }
            )
    report = {"schema_version": 1, "archive_count": len(files), **summarize_replay(rows)}
    report["status"] = (
        "ERROR"
        if not rows or any(row["status"] in {"PARSE_ERROR", "HTTP_ERROR"} for row in rows)
        else "INCOMPLETE_CAPTURE"
        if any(row["status"] == "UNAVAILABLE" for row in rows)
        else "OK"
    )
    write_json_atomic(output_path, report)
    return report


def replay_response(
    operation: str,
    content: bytes,
    params: Mapping[str, str],
    *,
    mime: str = "",
    context_mrn: str = "",
    context_national_id: str = "",
) -> dict[str, Any]:
    try:
        if not content and operation != "portal.login":
            return {
                "status": "UNAVAILABLE",
                "error_code": "RESPONSE_BODY_MISSING",
                "record_count": None,
            }
        entry = HarEntry("", "", frozenset(), frozenset(), {}, 200, content, mime)
        text = entry.text()
        if operation == "prq.patient_identity":
            parsing.parse_patient_identity(text, expected_national_id=context_national_id)
            return {"status": "PARSED", "error_code": "", "record_count": 1}
        if operation in OPERATION_BY_KEY and OPERATION_BY_KEY[operation].mutates:
            value = (
                json.loads(text)
                if OPERATION_BY_KEY[operation].response_kind == "json"
                else text.strip()
            )
            _require(mutation_acknowledged(operation, value), "MUTATION_ACK_MISSING")
            return {"status": "RECORDED_ACK", "error_code": "", "record_count": None}
        if operation == "prq.opd_landing":
            soup = BeautifulSoup(text, "html.parser")
            _require(soup.find(attrs={"name": "docCode"}) is not None, "OPD_DOCTOR_FIELD_MISSING")
            _require(soup.find(attrs={"name": "opdDate"}) is not None, "OPD_DATE_FIELD_MISSING")
            value = parsing.parse_opd_patients(
                text, visit_date=date(2000, 1, 1), doctor_card=params.get("docCode", "")
            )
            if not value:
                _require(
                    "查無" in soup.get_text() and "病患清單" in soup.get_text(),
                    "OPD_LANDING_CONTENT_UNRECOGNIZED",
                )
            return {
                "status": "PARSED" if value else "EMPTY",
                "error_code": "",
                "record_count": len(value),
            }
        if operation not in QUERY_BY_KEY:
            _CONTRACTS[operation].validator(entry)
            return {"status": "CONTRACT_OK", "error_code": "", "record_count": None}
        if operation == "prq.pacs_image":
            value = parse_binary_asset(content, media_type="image/jpeg")
        elif operation in {"prq.pdf_attachment", "oppl_records.pdf"}:
            value = parse_binary_asset(content, media_type="application/pdf")
        else:
            soup = BeautifulSoup(text, "html.parser")
            if soup.find(attrs={"name": "muid"}) and soup.find(attrs={"name": "mpassword"}):
                raise ParseError(
                    "recorded response is a login form", code="AUTH_SESSION_LOGIN_FORM"
                )
            value = _parse(operation, text, params, context_mrn, soup, context_national_id)
        count = _count(value)
        return {
            "status": "EMPTY" if count == 0 else "PARSED",
            "error_code": "",
            "record_count": count,
            **(
                {
                    "report_data_status": value.report_data_status,
                    "report_text_characters": len(value.report_text),
                    "text_extraction_notes": list(value.text_extraction_notes),
                }
                if isinstance(value, OrderReport)
                else {}
            ),
            **(
                {
                    "section_counts": dict(Counter(row.section_code or "UNKNOWN" for row in value)),
                    "missing_mrn_count": sum(not row.mrn for row in value),
                }
                if operation == "prq.opd_patients"
                else {}
            ),
        }
    except Exception as exc:
        return {"status": "PARSE_ERROR", "error_code": error_code(exc), "record_count": None}


def _parse(
    key: str,
    text: str,
    params: Mapping[str, str],
    context_mrn: str,
    soup: BeautifulSoup,
    context_national_id: str = "",
) -> Any:
    mrn = params.get("hhisnum") or params.get("patno") or context_mrn
    if key.startswith("review."):
        payload = json.loads(text)
        parser = {
            "review.login_info": parse_login_info,
            "review.options": parse_review_options,
            "review.doctors": parse_review_doctors,
            "review.cases": parse_review_cases,
        }.get(key)
        if parser:
            return parser(payload)
        ref = ReviewCaseRef(params.get("ApplySeq", ""))
        return (
            parse_review_case(payload, ref)
            if key == "review.case_detail"
            else parse_review_part(payload, ref, key.split(".")[1])
        )
    if key == "oppl_records.departments":
        return parse_surgery_departments(json.loads(text))
    if key == "oppl_records.cases":
        return parse_surgery_cases(json.loads(text))
    if key == "oppl_records.note":
        return parse_surgery_note(
            json.loads(text), SurgeryCaseRef(mrn, params.get("reqno", ""), params.get("seqno", ""))
        )
    if key.startswith("oppl.") and key.removeprefix("oppl.") in OPPL_JSON_FIELDS:
        return parse_oppl_payload(key, json.loads(text), params.get("hhisnum", ""))
    if key == "oppl.schedule_form":
        return parse_form(text, "openForm")
    if key == "prq.upload_types":
        payload = json.loads(text)
        _require(
            isinstance(payload, list)
            and all(
                isinstance(row, dict) and {"maintp", "mainnm"}.issubset(row) for row in payload
            ),
            "UPLOAD_TYPES_INVALID",
        )
        return payload
    if key in {
        "prq.allergy",
        "prq.advance_directives",
        "prq.research_flags",
        "prq.bed_transfers",
        "prq.care_cases",
    }:
        from ..adapters.prq_extensions import parse_patient_flags

        return parse_patient_flags(key, json.loads(text), mrn)
    if key == "prq.text_report_history":
        return parse_text_history(text, mrn, params.get("dept", ""))
    if key == "prq.upload_history":
        return parse_upload_history(text, mrn)
    if not mrn and not QUERY_BY_KEY[key].scope.startswith("doctor_"):
        raise ParseError("recorded parser context is missing", code="REPLAY_CONTEXT_MISSING")
    case = VisitCase(
        mrn,
        _date(params.get("caseDT") or params.get("casedt")),
        params.get("caseType") or "O",
        params.get("caseNo") or params.get("caseNoO") or "REPLAY",
        params.get("caseSec") or params.get("casesec", ""),
        params.get("caseSectC", ""),
    )
    if key == "webmaas.demographics":
        return parsing.parse_patient_demographics(json.loads(text), mrn)
    if key == "webmaas.basic_info":
        return parsing.parse_patient_basic_info(text, mrn)
    if key == "webmaas.registration_query":
        _require(soup.find("table", id="row") is not None, "WEBMAAS_REGISTRATION_STRUCTURE_MISSING")
        return parsing.parse_registration_records(text)
    if key == "prq.visit_cases":
        rows = parsing.parse_visit_cases(text, mrn, expected_national_id=context_national_id)
        _require(bool(rows) or soup.find(id="typeO") is not None, "PRQ_CASE_LIST_STRUCTURE_MISSING")
        return rows
    if key == "prq.case_detail":
        _require(
            soup.find(id="tabs") is not None or soup.find(id="tab_ul") is not None,
            "PRQ_CASE_DETAIL_STRUCTURE_MISSING",
        )
        return parsing.parse_case_detail(text, case)
    if key == "prq.soap":
        _require(soup.find(id="data") is not None, "PRQ_SOAP_CONTAINER_MISSING")
        return parsing.parse_soap(text, case)
    if key == "prq.numeric":
        return parsing.parse_numeric_report(text, case)
    if key in {"prq.case_orders", "prq.order_history"}:
        return parsing.parse_clinical_orders(
            text, mrn=mrn, case=case if key == "prq.case_orders" else None
        )
    if key in {"prq.case_medications", "prq.medication_history"}:
        return parsing.parse_medication_orders(
            text, mrn=mrn, case=case if key == "prq.case_medications" else None
        )
    if key == "prq.numeric_history":
        return parsing.parse_numeric_history(text, mrn=mrn)
    if key == "prq.surgery_history":
        return parsing.parse_surgery_history(text, mrn=mrn)
    if key == "prq.consults":
        return parsing.parse_consults(text, case=case)
    if key == "prq.treatments":
        return parsing.parse_treatments(text, case=case)
    if key == "prq.order_detail":
        ref = OrderDetailRef(
            mrn,
            case.case_no,
            case.case_type,
            params.get("seqNo", ""),
            params.get("orRsType", ""),
            params.get("orStep", ""),
            params.get("source", ""),
        )
        return parsing.parse_order_detail(text, reference=ref)
    if key in {"prq.order_report", "prq.text_report"}:
        ref = OrderReportRef(
            mrn,
            case.case_no,
            case.case_type,
            params.get("seqNo", ""),
            params.get("orDept", ""),
            params.get("orRsType", ""),
            params.get("orpfcode", ""),
            params.get("source", ""),
        )
        return parsing.parse_order_report(text, reference=ref)
    if key == "prq.pacs_study":
        return parsing.parse_pacs_study(text, reference=PacsStudyRef(mrn, params.get("reqno", "")))
    if key == "prq.opd_patients":
        return parsing.parse_opd_patients(
            text,
            visit_date=_date(params.get("opdDate")) or date(2000, 1, 1),
            doctor_card=params.get("docCode", ""),
        )
    if key == "oppl.surgery_schedule":
        return parsing.parse_surgery_records(json.loads(text))
    if key == "audit.unsigned_records":
        return parsing.parse_unsigned_records(text)
    raise ParseError("no replay parser", code="REPLAY_UNSUPPORTED")


def _count(value: Any) -> int:
    if isinstance(value, ReviewCasePart):
        return value.total
    if value is None:
        return 0
    if isinstance(value, TextReportHistory):
        return len(value.report_refs)
    if isinstance(value, UploadHistory):
        return len(value.pdf_refs)
    if isinstance(value, dict):
        for key in ("surgs", "reqs", "pfiles", "forms", "caseList"):
            if isinstance(value.get(key), (list, dict)):
                return len(value[key])
    if isinstance(value, OrderReport):
        return int(bool(value.fields or value.report_text or value.pdf_refs or value.pacs_refs))
    if isinstance(value, (list, tuple)):
        return len(value)
    for field in ("blocks", "tables", "images"):
        if hasattr(value, field):
            return len(getattr(value, field))
    return 1


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ParseError("recorded response structure is missing", code=code)


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value or "")
    except ValueError:
        return None


def summarize_replay(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    items = list(rows)
    statuses = {row["status"] for row in items}
    return {
        "exchange_count": len(items),
        "status_counts": {
            status: sum(row["status"] == status for row in items) for status in sorted(statuses)
        },
        "exchanges": items,
        "scope": "response parsers only; no authentication, request construction, or network replay",
    }
