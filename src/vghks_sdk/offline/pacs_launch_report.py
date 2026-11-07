"""Audit standalone PACS evidence in place; never prepare or send a command."""

# ruff: noqa: RUF001

from __future__ import annotations

import re
from hashlib import sha256
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from ..build_info import build_identity
from ..contracts.har import HarEntry
from ..models import OrderDetailRef, OrderReportRef, PacsLaunchRef, to_jsonable
from ..parsing.clinical import parse_clinical_orders, parse_order_detail, parse_order_report
from ..parsing.pacs_launch import parse_pacs_launch_bootstrap, parse_pacs_launch_context
from .bundle import BundleReader
from .evidence import _safe_build, _safe_code
from .replay import replay_bundle, summarize_replay
from .requests import identify_parts, request_parts
from .retest import build_retest_config

_ROOT = "private/pacs_discovery/"
_COMMANDS = ("login", "wonderurl", "setanonymousmode", "cmove")
_NATIVE_CODES = frozenset("SMARTIRIS_" + suffix for suffix in (
    "WINDOWS_REQUIRED", "LOCK_UNAVAILABLE", "LOCK_ABANDONED", "LOCK_TIMEOUT", "CONFIG_INVALID",
    "URL_INVALID", "SESSION_NOT_ISOLATED", "CONTEXT_INVALID", "CLOSED", "INTERRUPTED",
    "TIME_BUDGET_EXCEEDED", "RESPONSE_TOO_LARGE", "REDIRECT_REJECTED", "HTTP_ERROR",
    "CONNECT_TIMEOUT", "CONNECTION_REFUSED", "TIMEOUT", "TRANSPORT_ERROR", "CAPTURE_FAILED",
))
_STATES = frozenset({
    "NOT_RUN", "NOT_REQUESTED", "RUNNING", "OK", "ERROR", "UNKNOWN", "STARTED",
    "EXPLICIT_REFERENCE", "SELECTED", "NO_CANDIDATES", "CANCELLED", "NO_SAMPLE",
    "PREPARED", "PREPARED_WITH_GAPS", "HANDOFF_SENT", "HANDOFF_SENT_WITH_GAPS",
    "OBSERVATION_FAILED", "NOT_CHECKED", "MATCHED", "NOT_LOADED", "WRONG_STUDY",
    "BRIDGE_UNAVAILABLE", "BRIDGE_UNCONFIRMED", "INTERRUPTED", "BOOTSTRAP_FAILED",
    "OUTPUT_WRITE_FAILED", "DISCOVERY_FAILED", "DISCOVERY_INCOMPLETE", "PACKAGING_FAILED",
    "COMPLETED_WITH_GAPS", "COMPLETED_WITH_ERRORS", "FAILED", "OUTCOME_UNKNOWN",
})
_COUNTS = ("total", "scanned", "pending", "candidate_count", "source_rows", "error_count",
           "gap_count", "history_queries", "detail_report_queries", "not_executed_count",
           "no_followup_count", "unknown_date_count", "lookback_days")


def _state(value: Any) -> str:
    return value if isinstance(value, str) and value in _STATES else "UNKNOWN"


def _number(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _identity(row: dict) -> tuple:
    ref = row.get("detail_ref") or row.get("report_ref") or {}
    return (row.get("mrn"), row.get("case_type"), row.get("case_no"), ref.get("sequence_no", ""),
            row.get("name"), row.get("order_date"))


class _Audit:
    def __init__(self, reader: BundleReader) -> None:
        self.reader = reader
        self.issues: list[dict] = []
        self.problem_count = 0
        self.gaps: list[str] = []
        self.captures = [row for row in reader.jsonl("capture_manifest.jsonl")
                         if row.get("kind") in {"HTTP_EXCHANGE", "NETWORK_ERROR"}]
        self.pages: dict[str, dict] = {}

    def issue(self, code: str, phase: str) -> None:
        self.problem_count += 1
        row = {"code": code, "phase": phase}
        if row not in self.issues:
            self.issues.append(row)

    def gap(self, code: str) -> None:
        if code not in self.gaps:
            self.gaps.append(code)

    def json(self, name: str, expected: type = dict, *, required: bool = True) -> Any:
        if name not in self.reader.names:
            if required:
                self.issue("PACS_EVIDENCE_FILE_MISSING", "INTEGRITY")
            return expected()
        try:
            value = self.reader.json(name)
            if isinstance(value, expected):
                return value
        except Exception:
            pass
        self.issue("PACS_EVIDENCE_FILE_INVALID", "INTEGRITY")
        return expected()

    def text(self, capture: dict) -> str:
        response = capture.get("response") or {}
        name = capture.get("response_file")
        if (capture.get("kind") != "HTTP_EXCHANGE" or response.get("status_code") != 200
                or not isinstance(name, str) or name not in self.reader.names):
            raise ValueError("response unavailable")
        body = self.reader.read(name)
        if response.get("body_length") != len(body):
            self.issue("PACS_RESPONSE_LENGTH_MISMATCH", "INTEGRITY")
        mime = next((str(v) for k, v in response.get("headers", [])
                     if str(k).lower() == "content-type"), "")
        return HarEntry("", "", frozenset(), frozenset(), {}, 200, body, mime).text()

    def matching(self, operation: str, parameters: dict) -> list[dict]:
        result = []
        for row in self.captures:
            method, path, query, form = request_parts(row.get("request") or {})
            if identify_parts(method, path, query, form) == operation:
                values = {**query, **form}
                if all(values.get(k, "") == str(v) for k, v in parameters.items()):
                    result.append(row)
        return result

    def page(self, name: str, stage: str, reference: dict) -> dict:
        if name in self.pages:
            saved = self.pages[name]
            if saved.get("reference") != reference or not saved.get("verified"):
                self.issue("PACS_DISCOVERY_SOURCE_MISMATCH", "DISCOVERY")
                return {}
            return saved.get("value") or {}
        if not re.fullmatch(r"pages/\d{6}\.json", name):
            self.issue("PACS_DISCOVERY_SOURCE_INVALID", "DISCOVERY")
            return {}
        saved = self.json(_ROOT + name)
        self.pages[name] = {"reference": reference, "value": {}, "verified": False}
        start = self.problem_count
        value = saved.get("value") or {}
        if saved.get("reference") != reference or value.get("reference") != reference:
            self.issue("PACS_DISCOVERY_SOURCE_MISMATCH", "DISCOVERY")
        try:
            detail = stage == "DETAIL"
            ref = (OrderDetailRef if detail else OrderReportRef)(**reference)
            parameters = {"hhisnum": ref.mrn, "caseNo": ref.case_no,
                          "caseType": ref.case_type, "seqNo": ref.sequence_no,
                          "orRsType": ref.result_type, "source": ref.source}
            if detail:
                parameters["orStep"] = ref.order_step
            else:
                parameters.update(orDept=ref.department, orpfcode=ref.fee_code)
            rows = self.matching("prq.order_detail" if detail else "prq.order_report", parameters)
            if not rows:
                raise ValueError("capture missing")
            parsed = (parse_order_detail if detail else parse_order_report)(
                self.text(rows[-1]), reference=ref)
            parsed_value = to_jsonable(parsed)
            if parsed_value != value:
                self.issue("PACS_DISCOVERY_REPLAY_MISMATCH", "DISCOVERY")
            self.pages[name] = {"reference": reference, "value": parsed_value,
                                "verified": self.problem_count == start}
            return parsed_value
        except Exception:
            self.issue("PACS_DISCOVERY_SOURCE_UNVERIFIED", "DISCOVERY")
            return {}

    def discovery(self, run: dict, config: dict) -> tuple[dict, list]:
        recorded = run.get("discovery")
        if recorded is None:
            return {"status": "NOT_RUN"}, []
        start = self.problem_count
        summary = self.json(_ROOT + "summary.json")
        if summary != recorded:
            self.issue("PACS_DISCOVERY_SUMMARY_MISMATCH", "DISCOVERY")
        request = self.json(_ROOT + "request.json")
        mrn = config.get("test_mrn")
        if request != {"mrn": mrn, "lookback_days": 4000, "categories": ["*", "OR"]}:
            self.issue("PACS_DISCOVERY_REQUEST_MISMATCH", "DISCOVERY")
        originals: list[tuple[str, dict]] = []
        for i, category in enumerate(("*", "OR"), 1):
            name = _ROOT + f"history_{i}.json"
            if name not in self.reader.names:
                continue  # A failed history query has no successful result file.
            values = self.json(name, list)
            try:
                rows = self.matching("prq.order_history", {"hhisnum": mrn, "date": "4000", "ordertype": category})
                if not rows:
                    raise ValueError("history missing")
                parsed = to_jsonable(parse_clinical_orders(self.text(rows[-1]), mrn=mrn))
                if values != parsed:
                    self.issue("PACS_DISCOVERY_REPLAY_MISMATCH", "DISCOVERY")
                if any(row.get("mrn") != mrn for row in values):
                    self.issue("PACS_DISCOVERY_PATIENT_MISMATCH", "DISCOVERY")
                originals.extend((category, row) for row in values)
            except Exception:
                self.issue("PACS_DISCOVERY_HISTORY_UNVERIFIED", "DISCOVERY")
        candidates = self.json(_ROOT + "candidates.json", list)
        issues = self.json(_ROOT + "issues.json", list)
        if any(not isinstance(row, dict) for row in candidates + issues):
            raise ValueError("discovery row shape")
        if not issues and any(_ROOT + f"history_{i}.json" not in self.reader.names
                              for i in range(1, (summary.get("history_queries") or 0) + 1)):
            self.issue("PACS_DISCOVERY_HISTORY_MISSING", "DISCOVERY")
        outcomes = [self.json(name) for name in sorted(self.reader.names)
                    if re.fullmatch(_ROOT + r"orders/\d{6}\.json", name)]
        identities = {_identity(row) for _, row in originals}
        counts = {key: _number(summary.get(key)) for key in _COUNTS}
        if (counts["source_rows"] != len(originals) or counts["total"] != len(identities)
                or counts["candidate_count"] != len(candidates)
                or counts["scanned"] != len(outcomes) or counts["error_count"] != len(issues)
                or counts["pending"] != len(identities) - len(outcomes)):
            self.issue("PACS_DISCOVERY_SUMMARY_MISMATCH", "DISCOVERY")
        seen_groups, outcome_refs = set(), set()
        for i, outcome in enumerate(outcomes, 1):
            if outcome.get("scan_index") != i:
                self.issue("PACS_DISCOVERY_JOURNAL_MISMATCH", "DISCOVERY")
            variants = outcome.get("source_rows") or []
            group_ids = set()
            for source in variants:
                index = source.get("index")
                if type(index) is not int or not 1 <= index <= len(originals) or (
                    originals[index - 1] != (source.get("category"), source.get("order"))
                ):
                    self.issue("PACS_DISCOVERY_JOURNAL_MISMATCH", "DISCOVERY")
                else:
                    group_ids.add(_identity(source["order"]))
            if len(group_ids) != 1 or group_ids & seen_groups:
                self.issue("PACS_DISCOVERY_JOURNAL_MISMATCH", "DISCOVERY")
            seen_groups.update(group_ids)
            for ref in outcome.get("references") or []:
                outcome_refs.add((ref.get("mrn"), ref.get("request_no")))
            if outcome.get("issues") or outcome.get("status") in {"PARTIAL", "AUTHENTICATION_FAILED", "ERROR"}:
                self.issue("PACS_DISCOVERY_RECORDED_ERROR", "DISCOVERY")
        source_count, verified_candidates, references = 0, 0, set()
        for candidate in candidates:
            before = self.problem_count
            ref = candidate.get("reference") or {}
            try:
                PacsLaunchRef(**ref)
            except Exception:
                self.issue("PACS_DISCOVERY_REFERENCE_INVALID", "DISCOVERY")
            key = (ref.get("mrn"), ref.get("request_no"))
            if key in references or ref.get("mrn") != mrn:
                self.issue("PACS_DISCOVERY_REFERENCE_MISMATCH", "DISCOVERY")
            references.add(key)
            sources = candidate.get("sources") or []
            if not sources:
                self.issue("PACS_DISCOVERY_SOURCE_MISSING", "DISCOVERY")
            for source in sources:
                source_count += 1
                index, stage = source.get("order_index"), source.get("stage")
                order = source.get("order") or {}
                if (type(index) is not int or not 1 <= index <= len(originals)
                        or originals[index - 1] != (source.get("category"), order)
                        or _identity(order) not in seen_groups):
                    self.issue("PACS_DISCOVERY_SOURCE_MISMATCH", "DISCOVERY")
                if stage == "ORDER":
                    expected_file = "history_1.json" if source.get("category") == "*" else "history_2.json"
                    valid = (source.get("source_file") == expected_file
                             and source.get("source_ref") is None and order.get("pacs_launch_ref") == ref)
                elif stage in {"DETAIL", "REPORT"}:
                    page_ref = source.get("source_ref") or {}
                    binding = ("mrn", "case_no", "case_type", "sequence_no")
                    base = order.get("detail_ref") or order.get("report_ref") or {}
                    valid = all(page_ref.get(k) == base.get(k) for k in binding)
                    value = self.page(source.get("source_file", ""), stage, page_ref)
                    valid = valid and ref in (value.get("pacs_launch_refs") or [])
                else:
                    valid = False
                if not valid:
                    self.issue("PACS_DISCOVERY_SOURCE_MISMATCH", "DISCOVERY")
            if sources:
                order = sources[0].get("order") or {}
                if any(candidate.get(k) != order.get(k) for k in ("name", "order_date", "execution_date")):
                    self.issue("PACS_DISCOVERY_LABEL_MISMATCH", "DISCOVERY")
            verified_candidates += self.problem_count == before
        if references != outcome_refs:
            self.issue("PACS_DISCOVERY_JOURNAL_MISMATCH", "DISCOVERY")
        # Also inspect non-candidate saved pages, so a successful handoff cannot
        # hide an upstream query or extraction failure on another order.
        for outcome in outcomes:
            for stage in outcome.get("stages") or []:
                if stage.get("status") == "ERROR":
                    self.issue("PACS_DISCOVERY_RECORDED_ERROR", "DISCOVERY")
                elif stage.get("status") == "OK":
                    ref = stage.get("reference") or {}
                    self.page(stage.get("source_file", ""), "REPORT" if "department" in ref else "DETAIL", ref)
        if issues:
            self.issue("PACS_DISCOVERY_RECORDED_ERROR", "DISCOVERY")
        if counts["pending"]:
            self.gap("PACS_DISCOVERY_PENDING")
        if counts["gap_count"]:
            self.gap("PACS_DISCOVERY_EXTRACTION_INCOMPLETE")
        expected_complete = summary.get("loaded") is True and not (
            counts["pending"] or issues or counts["gap_count"]
            or summary.get("status") in {"AUTHENTICATION_FAILED", "INTERRUPTED", "ERROR"})
        if summary.get("complete") is not expected_complete:
            self.issue("PACS_DISCOVERY_SUMMARY_MISMATCH", "DISCOVERY")
        return {"status": "VERIFIED" if self.problem_count == start else "INCOMPLETE",
                **counts, "complete": summary.get("complete") is True,
                "candidate_source_count": source_count, "verified_candidates": verified_candidates}, candidates

    def preparation(self, run: dict, selected: dict) -> tuple[dict, Any]:
        start = self.problem_count
        rows = []
        for row in self.captures:
            parts = request_parts(row.get("request") or {})
            if parts[1] in {"/SmartWonderRAD/Query/qryExternalAP.jsp", "/SmartWonderRAD/Viewer/vwrIris.jsp"}:
                rows.append(row)
        if run.get("prepare") != "OK":
            if rows and run.get("prepare") != "ERROR":
                self.issue("PACS_PREPARE_STAGE_MISMATCH", "PREPARE")
            return {"status": "NOT_RUN" if not rows else "INCOMPLETE", "response_count": len(rows)}, None
        try:
            if len(rows) != 2:
                raise ValueError("prepare pair missing")
            a, b = rows
            ap, bp = request_parts(a["request"]), request_parts(b["request"])
            if (identify_parts(*ap) != "smartwonder.launch_bootstrap"
                    or identify_parts(*bp) != "smartwonder.launch_viewer"
                    or not a.get("sdk_operation_id") or a.get("sdk_operation_id") != b.get("sdk_operation_id")):
                raise ValueError("prepare scope mismatch")
            x, y = {**ap[2], **ap[3]}, {**bp[2], **bp[3]}
            if x.get("LoginLevel") != "RAD" or x.get("PatID") != selected.get("mrn") or x.get("AccNo") != selected.get("request_no"):
                raise ValueError("prepare selection mismatch")
            first, second = urlsplit(a["request"]["url"]), urlsplit(b["request"]["url"])
            if (first.scheme, first.netloc) != (second.scheme, second.netloc):
                raise ValueError("prepare origin mismatch")
            bootstrap = parse_pacs_launch_bootstrap(self.text(a), reference=PacsLaunchRef(**selected),
                                                   expected_login_id=x.get("LoginID", ""))
            if y.get("LoginID") != bootstrap.login_id or y.get("WonderClientIP") != bootstrap.client_ip:
                raise ValueError("viewer context mismatch")
            context = parse_pacs_launch_context(bootstrap, self.text(b))
            if self.json("parsed/launch_context_summary.json") != to_jsonable(context):
                self.issue("PACS_CONTEXT_SUMMARY_MISMATCH", "PREPARE")
            return {"status": "VERIFIED" if self.problem_count == start else "INCOMPLETE",
                    "response_count": 2}, context
        except Exception:
            self.issue("PACS_PREPARE_EVIDENCE_UNVERIFIED", "PREPARE")
            return {"status": "INCOMPLETE", "response_count": len(rows)}, None

    def native(self, run: dict, context: Any, config: dict) -> dict:
        start = self.problem_count
        files = sorted(name for name in self.reader.names if re.fullmatch(r"private/native/\d{3}\.json", name))
        all_files = {name for name in self.reader.names if name.startswith("private/native/")}
        expected_files = set(files) | {name[:-5] + ".body" for name in files}
        if all_files - expected_files:
            self.issue("PACS_NATIVE_UNEXPECTED_FILE", "NATIVE")
        result = run.get("native_result") or {}
        if not isinstance(result, dict):
            raise ValueError("native result shape")
        recorded_steps = result.get("steps") or []
        called = run.get("native_calls", 0)
        if not called and not files and not recorded_steps:
            return {"status": "NOT_RUN", "response_count": 0, "steps": []}
        if called != 1 or len(files) != len(recorded_steps) or len(files) > 4:
            self.issue("PACS_NATIVE_COUNT_MISMATCH", "NATIVE")
        if run.get("handoff") == "HANDOFF_SENT" and (len(files) != 4 or result.get("status") != "HANDOFF_SENT"):
            self.issue("PACS_NATIVE_SEQUENCE_INCOMPLETE", "NATIVE")
        if not context:
            self.issue("PACS_NATIVE_CONTEXT_UNVERIFIED", "NATIVE")
        expected = [] if context is None else [
            {"id": context.login_id, "pwd": ""}, {"url": context.wonder_url + "/"}, {"mode": "0"},
            {"cmoveurl": context.cmove_url, "url": context.wado_url, "dicomserver": context.dicom_server,
             "level": context.level, "uid": context.study_uid, "command": "1"},
        ]
        steps, last_timestamp, port = [], -1, None
        for i, name in enumerate(files):
            metadata = self.json(name)
            step = recorded_steps[i] if i < len(recorded_steps) and isinstance(recorded_steps[i], dict) else {}
            command = _COMMANDS[i] if i < 4 else "UNKNOWN"
            complete, response = metadata.get("complete") is True, metadata.get("http_status")
            body, body_ok, binding = b"", False, False
            try:
                if name != f"private/native/{i + 1:03d}.json" or metadata.get("body_file") != f"{i + 1:03d}.body":
                    raise ValueError("body name mismatch")
                body = self.reader.read(f"private/native/{i + 1:03d}.body")
                digest = sha256(body).hexdigest() if complete or body else ""
                body_ok = (step.get("response_size") == len(body) and step.get("response_sha256") == digest)
                address = urlsplit(metadata.get("request_url", ""))
                pairs = parse_qsl(address.query, keep_blank_values=True)
                parameters = dict(pairs)
                timestamp = parameters.pop("timestamp", "")
                timing = timestamp.isascii() and timestamp.isdecimal() and int(timestamp) > last_timestamp
                if timing:
                    last_timestamp = int(timestamp)
                if port is None:
                    port = address.port
                binding = (i < len(expected) and parameters == expected[i] and timing
                           and len(pairs) == len(dict(pairs)) and metadata.get("method") == "GET"
                           and address.scheme == "http" and address.hostname == "127.0.0.1"
                           and not address.username and not address.password and not address.fragment
                           and address.port == port and (config.get("synthetic_loopback") is True or port == 15516)
                           and address.path == "/" + command and metadata.get("endpoint_path") == address.path)
                if not body_ok:
                    self.issue("PACS_NATIVE_BODY_MISMATCH", "NATIVE")
                if not binding:
                    self.issue("PACS_NATIVE_REQUEST_MISMATCH", "NATIVE")
            except Exception:
                self.issue("PACS_NATIVE_EVIDENCE_MISSING", "NATIVE")
            transport_ok = complete and response == 200 and not metadata.get("error_code")
            if (step.get("name") != command or step.get("http_status") != response
                    or (transport_ok and step.get("status") != "HTTP_RESPONSE_RECEIVED")
                    or step.get("error_code", "") != metadata.get("error_code", "")):
                self.issue("PACS_NATIVE_SUMMARY_MISMATCH", "NATIVE")
            if not transport_ok:
                self.issue("PACS_NATIVE_TRANSPORT_INCOMPLETE", "NATIVE")
            steps.append({"index": i + 1, "name": command, "complete": complete,
                          "http_status": _number(response), "size": len(body),
                          "response_sha256": sha256(body).hexdigest(), "body_matches": body_ok,
                          "request_matches": binding, "body_class": "LITERAL_OK" if body == b"ok" else "OTHER",
                          "error_code": metadata.get("error_code") if isinstance(metadata.get("error_code"), str)
                          and metadata["error_code"] in _NATIVE_CODES else "UNKNOWN" if metadata.get("error_code") else ""})
        if result.get("viewer_loaded") is not None or result.get("retry_safe") is not False:
            self.issue("PACS_NATIVE_RESULT_INVALID", "NATIVE")
        return {"status": "VERIFIED" if self.problem_count == start and len(files) == 4 else "INCOMPLETE",
                "response_count": len(files), "steps": steps,
                "viewer_loaded": None, "retry_safe": False}


def _availability(audit: _Audit, run: dict, selected: bool) -> dict:
    source = run.get("bridge_availability")
    needed = run.get("native_requested") is True and selected and run.get("login") == "OK"
    if source is None:
        if needed:
            audit.gap("PACS_AVAILABILITY_NOT_RECORDED")
            version = str(run.get("sdk_version", ""))
            if re.fullmatch(r"\d+\.\d+\.\d+", version) and tuple(map(int, version.split("."))) >= (0, 24, 1):
                audit.issue("PACS_AVAILABILITY_EVIDENCE_MISSING", "AVAILABILITY")
        return {"status": "NOT_RECORDED" if needed else "NOT_REQUIRED"}
    allowed = {
        "status": {"AVAILABLE", "UNAVAILABLE", "UNKNOWN", "UNSUPPORTED"},
        "installation": {"PRESENT", "MISSING", "UNKNOWN"},
        "process": {"RUNNING", "NOT_RUNNING", "UNKNOWN"},
        "listener": {"LISTENING", "NOT_LISTENING", "UNKNOWN"},
        "owner": {"MATCH_EXPECTED_PATH", "OTHER_PROCESS", "UNKNOWN", "NOT_APPLICABLE"},
    }
    if not isinstance(source, dict):
        audit.issue("PACS_AVAILABILITY_INVALID", "AVAILABILITY")
        return {"status": "UNKNOWN"}
    safe = {k: source.get(k) if isinstance(source.get(k), str) and source[k] in values else "UNKNOWN"
            for k, values in allowed.items()}
    if any(not isinstance(source.get(k), str) for k in allowed):
        audit.issue("PACS_AVAILABILITY_INVALID", "AVAILABILITY")
    if safe["status"] == "AVAILABLE" and (safe["listener"] != "LISTENING" or safe["owner"] != "MATCH_EXPECTED_PATH"):
        audit.issue("PACS_AVAILABILITY_INCONSISTENT", "AVAILABILITY")
    if safe["status"] in {"UNKNOWN", "UNSUPPORTED"}:
        audit.gap("PACS_AVAILABILITY_UNCONFIRMED")
    if safe["status"] != "AVAILABLE" and (run.get("native_calls") or run.get("prepare") == "OK") and needed:
        audit.issue("PACS_AVAILABILITY_GATE_BYPASSED", "AVAILABILITY")
    return safe


def inspect_pacs_launch_bundle(reader: BundleReader) -> tuple[dict[str, Any], dict[str, Any]]:
    audit = _Audit(reader)
    summary = audit.json("run_summary.json")
    config = audit.json("run_config.json", required=False)
    snapshot = audit.json("parsed/pacs_launch_execution.json", required=False)
    packaging_failed = summary.get("status") == "PACKAGING_FAILED"
    run = snapshot if packaging_failed and snapshot.get("profile") == "pacs_launch" else summary
    if snapshot and not packaging_failed and snapshot != summary:
        audit.issue("PACS_EXECUTION_SNAPSHOT_MISMATCH", "INTEGRITY")
    if packaging_failed:
        audit.issue("PACKAGING_FAILED", "PACKAGING")
    recorded = {key: _state(run.get(key)) for key in (
        "status", "login", "prepare", "handoff", "selection", "manual_observation")}
    recorded.update(native_calls=_number(run.get("native_calls")), exit_code=_number(run.get("exit_code")),
                    selection_index=_number(run.get("selection_index")))
    issue = run.get("issue") or {}
    code = _safe_code(issue.get("code")) if isinstance(issue, dict) else "PACS_RECORDED_ISSUE_INVALID"
    if code:
        audit.issue(code, "AUTHENTICATION" if run.get("login") != "OK" else "EXECUTION")
    if run.get("status") not in {"PREPARED", "HANDOFF_SENT", "CANCELLED", "NO_SAMPLE", "DISCOVERY_INCOMPLETE"}:
        audit.issue("PACS_RECORDED_EXECUTION_INCOMPLETE", "EXECUTION")
    if run.get("exit_code") not in {0, None}:
        audit.issue("PACS_RECORDED_EXECUTION_FAILED", "EXECUTION")
    if any(run.get(key) == "ERROR" for key in ("login", "prepare")):
        audit.issue("PACS_RECORDED_STAGE_FAILED", "EXECUTION")
    if ((run.get("login") != "OK" and (run.get("prepare") == "OK" or run.get("native_calls")))
            or (run.get("prepare") != "OK" and run.get("native_calls"))
            or (run.get("native_calls") and run.get("native_requested") is not True)
            or (run.get("status") in {"HANDOFF_SENT", "HANDOFF_SENT_WITH_GAPS"}
                and (run.get("handoff") != "HANDOFF_SENT" or run.get("native_calls") != 1))
            or (run.get("status") in {"PREPARED", "PREPARED_WITH_GAPS"}
                and (run.get("prepare") != "OK" or run.get("native_calls")))):
        audit.issue("PACS_EXECUTION_STAGE_MISMATCH", "EXECUTION")
    if run.get("manual_observation") in {"NOT_LOADED", "WRONG_STUDY"}:
        audit.issue("PACS_MANUAL_OBSERVATION_FAILED", "OBSERVATION")
    elif run.get("handoff") == "HANDOFF_SENT" and run.get("manual_observation") != "MATCHED":
        audit.gap("PACS_MANUAL_OBSERVATION_NOT_RECORDED")
    try:
        discovery, candidates = audit.discovery(run, config)
    except Exception:
        audit.issue("PACS_DISCOVERY_EVIDENCE_INVALID", "DISCOVERY")
        discovery, candidates = {"status": "INCOMPLETE", **dict.fromkeys(_COUNTS)}, []
    selected: dict = {}
    selection = run.get("selection") in {"SELECTED", "EXPLICIT_REFERENCE"} and run.get("login") == "OK"
    if selection:
        selected = audit.json("private/selected_order.json")
        try:
            PacsLaunchRef(**selected)
            if selected.get("mrn") != config.get("test_mrn"):
                raise ValueError("patient mismatch")
            if run.get("selection") == "SELECTED":
                candidate = audit.json("private/selected_candidate.json")
                index = run.get("selection_index")
                # Older standalone bundles stored the exact selected object,
                # but not its menu index. Match that object uniquely; never
                # infer the first candidate or reconstruct a missing choice.
                if index is None and run.get("sdk_version") == "0.24.0":
                    if sum(item == candidate for item in candidates) != 1:
                        raise ValueError("legacy selection ambiguous")
                    audit.gap("PACS_SELECTION_INDEX_NOT_RECORDED")
                elif type(index) is not int or not 1 <= index <= len(candidates) or candidates[index - 1] != candidate:
                    raise ValueError("selection mismatch")
                if candidate.get("reference") != selected:
                    raise ValueError("selected reference mismatch")
            elif selected.get("request_no") != config.get("request_no"):
                raise ValueError("explicit reference mismatch")
        except Exception:
            audit.issue("PACS_SELECTION_MISMATCH", "SELECTION")
    availability = _availability(audit, run, selection)
    try:
        prepare, context = audit.preparation(run, selected)
    except Exception:
        audit.issue("PACS_PREPARE_EVIDENCE_UNVERIFIED", "PREPARE")
        prepare, context = {"status": "INCOMPLETE", "response_count": 0}, None
    try:
        native = audit.native(run, context, config)
    except Exception:
        audit.issue("PACS_NATIVE_EVIDENCE_INVALID", "NATIVE")
        native = {"status": "INCOMPLETE", "response_count": 0, "steps": []}
    try:
        replay = replay_bundle(reader)
        for row in replay:
            if row.get("status") in {"HTTP_ERROR", "NETWORK_ERROR", "PARSE_ERROR", "UNAVAILABLE"}:
                audit.issue(_safe_code(row.get("error_code")) or "PACS_CAPTURE_UNVERIFIED", "UPSTREAM")
    except Exception:
        replay = []
        audit.issue("PACS_REPLAY_INCOMPLETE", "UPSTREAM")
    if run.get("status") in {"CANCELLED", "NO_SAMPLE", "DISCOVERY_INCOMPLETE"}:
        audit.gap("PACS_HANDOFF_NOT_TESTED")
    environment = audit.json("environment.json", required=False)
    recorded_build = _safe_build(environment.get("build") or {"sdk_version": run.get("sdk_version")})
    analysis_status = "NEEDS_ATTENTION" if audit.issues else "COMPLETED_WITH_GAPS" if audit.gaps else "OK"
    report = {
        "schema_version": 3, "profile": "pacs_launch", "bundle_status": _state(summary.get("status")),
        "analysis_status": analysis_status, "bundle": {"status": "READABLE", "file_count": len(reader.names)},
        "recorded_build": recorded_build, "analyzer_build": build_identity(),
        "root_cause": audit.issues[0] if audit.issues else None,
        "problems": audit.issues, "replay": summarize_replay(replay),
        "next_action": "Review the saved evidence and the operator observation. Any new login or native handoff requires a separate manual run; this report never retries it.",
        "operations": [
            {"operation": "pacs_launch.prepare", "live_status": "VERIFIED" if prepare["status"] == "VERIFIED" else "FAILED" if run.get("prepare") == "ERROR" else "NOT_TESTED"},
            {"operation": "pacs_launch.native_transport", "live_status": "VERIFIED" if native["status"] == "VERIFIED" else "FAILED" if run.get("native_calls") else "NOT_TESTED"},
        ],
        "pacs_launch_test": {"recorded": recorded, "discovery": discovery, "availability": availability,
                             "prepare": prepare, "native": native, "gaps": audit.gaps, "issues": audit.issues},
        "scope": "Recorded evidence only. HTTP completion is not image loading; MATCHED is the operator's separate observation. No browser, authentication, hospital query or native command is executed.",
    }
    return report, build_retest_config({"profile": "pacs_launch"}, [], None, report["bundle_status"])


def pacs_launch_markdown(report: dict[str, Any]) -> str:
    data = report["pacs_launch_test"]
    run, discovery = data["recorded"], data["discovery"]
    lines = ["# PACS 啟動回傳分析", "", f"- 原執行狀態：`{report['bundle_status']}`",
             f"- 分析狀態：`{report['analysis_status']}`",
             f"- 登入／準備／交接：`{run['login']}`／`{run['prepare']}`／`{run['handoff']}`",
             f"- 本機可用性：`{data['availability']['status']}`；人工觀察：`{run['manual_observation']}`",
             f"- 啟動資訊核對：`{data['prepare']['status']}`；本機存檔核對：`{data['native']['status']}`", ""]
    if discovery["status"] != "NOT_RUN":
        lines.append(f"已掃描 {discovery['scanned']} / {discovery['total']} 筆，尚餘 {discovery['pending']} 筆；"
                     f"候選 {discovery['candidate_count']} 筆，原查詢錯誤 {discovery['error_count']} 項。")
    lines += ["", "HTTP 完成只證明本機交接；人工 MATCHED 是另外記錄的顯示與檢查核對。"
              "尚未掃描與原查詢錯誤分開保留，已開啟一筆不能證明整份清單。", "",
              "| 本機步驟 | HTTP | 內容大小 | 內容分類 | 參數綁定 |",
              "| --- | ---: | ---: | --- | --- |"]
    for step in data["native"]["steps"]:
        lines.append(f"| {step['name']} | {step['http_status']} | {step['size']} | {step['body_class']} | {step['request_matches']} |")
    lines += ["", "問題：" + ("、".join(item["code"] for item in report["problems"]) or "無"),
              "缺漏：" + ("、".join(data["gaps"]) or "無"), "",
              "本報告只讀原始存檔，沒有瀏覽器、網路或本機命令。retest-config.json 是人工複核描述，"
              "不能自動執行、重送密碼或重送本機開啟。原始執行／封裝錯誤不因離線重解析而改寫。", ""]
    return "\n".join(lines)
