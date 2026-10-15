"""Safe evidence summaries and issue classification for saved bundles."""

# ruff: noqa: RUF001

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from ..contracts.order_assets import ORDER_WORKFLOW_PATH
from ..core.readiness import AUTH_CHECK_REGISTRY
from ..models.auth import PasswordStatus
from ..queries import QUERY_BY_KEY
from .bundle import BundleReader
from .replay import request_parts

_NETWORK_TARGETS = (*(spec.key for spec in AUTH_CHECK_REGISTRY), "mis")


_LEGACY_STEPS = {
    "patient_demographics": "webmaas.demographics",
    "registration_history": "webmaas.registration_query",
    "visit_cases": "prq.visit_cases",
    "order_history": "prq.order_history",
    "order_history_departmental": "prq.order_history",
    "medication_history": "prq.medication_history",
    "numeric_history": "prq.numeric_history",
    "surgery_history": "prq.surgery_history",
    "opd_patient_list": "prq.opd_patients",
    "surgery_schedule": "oppl.surgery_schedule",
    "unsigned_records": "audit.unsigned_records",
}


_CASE_STEPS = {
    "case_detail": "prq.case_detail",
    "soap": "prq.soap",
    "numeric": "prq.numeric",
    "orders": "prq.case_orders",
    "medications": "prq.case_medications",
    "consults": "prq.consults",
    "treatments": "prq.treatments",
}


def _no_sample_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    gaps = []
    for index, step in enumerate(steps, 1):
        if step.get("status") != "NO_SAMPLE":
            continue
        operation = _step_query(step)
        name = str(step.get("name") or "")
        # Never copy arbitrary step names: older tools may include patient IDs.
        known_check = re.fullmatch(
            r"visits\.(?:same_patient|list_equivalence|"
            r"filters\.(?:category_[OAE]|arrival_date|department_(?:code|name)|"
            r"doctor_(?:name|card)|combined)|followup(?:\.(?:soap|orders)_sample)?)",
            name,
        )
        login_check = re.fullmatch(
            r"login\.(?:cookie_loss|personnel\.(?:options|by_card|employee|name|title|unit|subunits|filters))",
            name,
        )
        failure_check = name in {
            "password.change_page", "password.basic_info",
            "failures.live.password_policy", "failures.live.unauthenticated",
            "failures.live.negative_before_login", "failures.live.cookie_loss",
            "failures.live.soap", "failures.live.numeric", "failures.live.orders",
            "failures.live.detail", "failures.live.report", "failures.live.pacs",
        }
        pacs_check = name in {"pacs.imgUrl_sample", "pacs.uid_sample"}
        reason = _safe_code((step.get("issue") or {}).get("code"))
        detail_reason = (step.get("details") or {}).get("reason")
        if (not reason and login_check and isinstance(detail_reason, str)
                and detail_reason in {"NO_UNAMBIGUOUS_OPTION", "DEPENDENCY_FAILED"}):
            reason = detail_reason
        if not reason and name in {"password.change_page", "password.basic_info"} and isinstance(detail_reason, str) and detail_reason in {
            "NO_TRUSTED_CHANGE_DESTINATION", "NO_AUTHORIZED_PATIENT",
        }:
            reason = detail_reason
        if name == "failures.live.password_policy" and not reason:
            reason = "PASSWORD_NOTICE_NOT_OBSERVED"
        if pacs_check and not reason:
            reason = "NO_PACS_IMGURL_REFERENCE" if name == "pacs.imgUrl_sample" else "NO_PACS_UID_REFERENCE"
        gaps.append(
            {
                "step_index": index,
                "step": name if known_check or login_check or failure_check or pacs_check else operation,
                "operation": operation,
                "reason_code": reason,
            }
        )
    return gaps


def _password_policy_summary(steps: list[dict], rows: list[dict]) -> dict[str, Any]:
    """Keep the original observation separate from parsing the saved responses."""

    recorded = next(
        ((step.get("details") or {}).get("password_status") for step in steps
         if step.get("name") in {"failures.live.password_policy", "password.policy"}),
        None,
    )
    try:
        status = PasswordStatus(**recorded) if isinstance(recorded, dict) else None
    except (TypeError, ValueError):
        status = None
    return {
        "recorded": (
            {"status": status.status, "remaining_days": status.remaining_days,
             "evidence": status.evidence} if status else None
        ),
        "offline_observations": [
            {"capture_id": row["capture_id"], **row["password_status"]}
            for row in rows if row.get("password_status")
        ],
        "scope": "Saved response parsing only; the original EXE result remains unchanged. This does not establish natural session TTL or a future forced password change.",
    }


def _data_quality_summary(steps: list[dict], rows: list[dict]) -> dict[str, Any]:
    return {
        "recorded_partial_steps": [
            {"operation": _step_query(step),
             "issue_codes": [_safe_code(code) for code in (step.get("details") or {}).get("parsing_issues", [])]}
            for step in steps if _step_query(step)
            and (step.get("details") or {}).get("acquisition_status") == "PARTIAL"
        ],
        "offline_partial_responses": [
            {"capture_id": row["capture_id"], "operation": row["operation"],
             "issue_codes": row["parsing_issues"]}
            for row in rows if row.get("data_complete") is False
        ],
        "offline_warnings": [
            {"capture_id": row["capture_id"], "operation": row["operation"],
             "warning_codes": row["parsing_warnings"]}
            for row in rows if row.get("parsing_warnings")
        ],
    }


def _ophthalmology_summary(reader: BundleReader) -> dict[str, Any]:
    path = ORDER_WORKFLOW_PATH + "manifest.json"
    if path not in reader.names:
        return {"status": "NOT_RUN"}
    manifest = reader.json(path)
    # Aggregate only; individual orders and report content remain in the ZIP.
    return {
        "status": manifest.get("status", "UNKNOWN"),
        "counts": manifest.get("counts", {}),
        "coverage": manifest.get("coverage", {}),
        "entry_path": "order_list",
        "manifest": path,
    }


def _report_content_summary(
    steps: list[dict[str, Any]], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    operations = {"prq.order_report", "prq.text_report"}
    statuses = ("TEXT_AVAILABLE", "ATTACHMENT_ONLY", "METADATA_ONLY", "EMPTY", "UNASSESSED")
    live = [
        (step.get("details") or {}).get("report_data_status", "UNASSESSED")
        for step in steps
        if _step_query(step) in operations and step.get("status") in {"OK", "EMPTY"}
    ]
    replay = [
        row["report_data_status"] for row in rows if row.get("report_data_status") in statuses
    ]
    return {
        "live_steps": {status: live.count(status) for status in statuses},
        "offline_responses": {status: replay.count(status) for status in statuses},
        "scope": "Report text availability; metadata, links and downloaded binaries do not establish extracted report text. Attachment errors do not revoke available text.",
    }


def _earnings_results(steps: list[dict[str, Any]]) -> list[dict[str, str]]:
    output = []
    for key in ("mis.performance", "mis.payroll"):
        selected = [step for step in steps if step.get("operation") == key]
        if not selected:
            continue
        statuses = {step.get("status") for step in selected}
        failed = next(
            (status for status in ("ERROR", "BLOCKED", "MISSING") if status in statuses), ""
        )
        report = next((step for step in selected if step.get("name") == f"{key}.report"), {})
        status = failed or ("VERIFIED" if report.get("status") == "OK" else "NOT_TESTED")
        output.append(
            {
                "operation": key,
                "live_status": status,
                "error_code": next(
                    (
                        _safe_code((step.get("issue") or {}).get("code"))
                        for step in selected
                        if step.get("issue")
                    ),
                    "",
                ),
            }
        )
    return output


def _opd_evidence(reader, steps, exchanges):
    missing_sections = 0
    for step in steps:
        if _step_query(step) != "prq.opd_patients":
            continue
        name = step.get("output", "")
        if name not in reader.names:
            continue
        data = reader.json(name)
        if isinstance(data, list):
            missing_sections += sum(
                isinstance(row, dict) and not row.get("section_code") for row in data
            )
    return {
        "recorded_rows_without_section": missing_sections,
        "offline_responses": [
            {
                "capture_id": row["capture_id"],
                "section_counts": row["section_counts"],
                "missing_mrn_count": row.get("missing_mrn_count", 0),
            }
            for row in exchanges
            if "section_counts" in row
        ],
    }


def _weekly_opd_summary(reader):
    path = "parsed/workflows/opd_soap_week/manifest.json"
    if path not in reader.names:
        return {"status": "NOT_TESTED", "counts": {}, "stages": {}}
    value = reader.json(path)
    keys = (
        "days_total",
        "days_completed",
        "days_failed",
        "registrations",
        "dedicated_registrations",
        "shared_registrations",
        "unclassified_registrations",
        "unqueryable_registrations",
        "patients_total",
        "patients_checked",
        "visit_query_errors",
        "registrations_with_visit",
        "registrations_without_visit",
        "registrations_unknown",
        "soap_cases",
        "soap_checked",
        "soap_errors",
        "soap_missing",
        "matches",
        "matched_patients",
    )
    return {
        "status": _safe_code(value.get("status")) or "UNKNOWN",
        "counts": {
            key: value.get("counts", {}).get(key)
            for key in keys
            if type(value.get("counts", {}).get(key)) is int
        },
        "stages": {
            stage: _safe_code(value.get("stages", {}).get(stage))
            for stage in ("stage1_opd", "stage2_visits", "stage3_soap")
        },
    }


def _safe_network_step(value: Any) -> str:
    candidate = str(value or "")
    return (
        candidate
        if re.fullmatch(
            r"network\.(portal|prq|sectord|webmaas|oppl|oppl_records|review|audit|mis|personnel|attendance)\."
            r"(dns|tcp|configure|https(_direct)?(_tls12(_compat)?)?(_unverified)?|https_schannel)",
            candidate,
        )
        else ""
    )


def _authentication_results(
    readiness: dict[str, Any], steps: list[dict[str, Any]]
) -> list[dict[str, str]]:
    result = []
    for key in _NETWORK_TARGETS:
        if key == "mis" and not any(row.get("name") == "auth_check.mis" for row in steps):
            continue
        target = next((row for row in readiness.get("targets", []) if row.get("target") == key), {})
        if not target:
            target = next((row for row in steps if row.get("name") == f"auth_check.{key}"), {})
        if not target and key == "portal":
            target = next((row for row in steps if row.get("name") == "password.login"), {})
        status = target.get("status")
        result.append(
            {
                "target": key,
                "status": status if status in {"OK", "ERROR", "BLOCKED"} else "NOT_TESTED",
                "error_code": _safe_code((target.get("issue") or {}).get("code")),
            }
        )
    return result


def _connection_trials(reader: BundleReader, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep tested modes/results, never arbitrary URLs, headers or server values."""
    result = []
    for step in steps:
        name = _safe_network_step(step.get("name"))
        if not name:
            continue
        _, app, phase = name.split(".")
        path = f"parsed/network/{app}-{phase}-context.json"
        context = reader.json(path) if path in reader.names else {}
        status = step.get("status")
        result.append(
            {
                "step": name,
                "status": status if status in {"OK", "ERROR"} else "UNKNOWN",
                "error_code": _safe_code((step.get("issue") or {}).get("code")),
                "effective_proxy_present": context.get("effective_proxy_present") is True,
                "certificate_verification": context.get("certificate_verification")
                if isinstance(context.get("certificate_verification"), bool)
                else None,
            }
        )
    return result


def _certificate_comparisons(trials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_step = {row["step"]: row for row in trials}
    result = []
    for trial in trials:
        if not trial["step"].endswith("_unverified"):
            continue
        paired = by_step.get(trial["step"].removesuffix("_unverified"), {})
        result.append(
            {
                "probe": trial["step"],
                "verified_status": paired.get("status", "NOT_TESTED"),
                "verified_error": paired.get("error_code", ""),
                "unverified_status": trial["status"],
                "unverified_error": trial["error_code"],
                "anonymous_probe": True,
            }
        )
    return result


def _connection_profiles(reader: BundleReader) -> list[dict[str, Any]]:
    """Report selected TLS evidence separately from authentication readiness."""
    path = "parsed/network/selected_profiles.json"
    if path not in reader.names:
        return []
    selections = reader.json(path)
    captures = reader.jsonl("capture_manifest.jsonl")
    output = []
    for key in _NETWORK_TARGETS:
        choice = selections.get(key)
        if not isinstance(choice, dict):
            continue
        probe = _safe_network_step(choice.get("probe"))
        negotiated = next(
            (
                (row.get("response") or {}).get("tls", {})
                for row in captures
                if probe
                and row.get("live_test_step") == probe
                and row.get("kind") == "HTTP_EXCHANGE"
            ),
            {},
        )
        profile = choice.get("tls_profile")
        protocol = negotiated.get("negotiated_protocol")
        cipher = str(negotiated.get("cipher") or "")
        output.append(
            {
                "target": key,
                "profile": profile
                if profile in {"DEFAULT", "TLS12", "TLS12_COMPAT"}
                else "UNKNOWN",
                "applied": choice.get("applied") is True,
                "certificate_verification": choice.get("certificate_verification")
                if isinstance(choice.get("certificate_verification"), bool)
                else None,
                "protocol": protocol if protocol in {"TLSv1.2", "TLSv1.3"} else "UNKNOWN",
                "cipher": cipher
                if re.fullmatch(r"(?:TLS_|ECDHE-|DHE-|AES)[A-Z0-9_-]{1,80}", cipher)
                else "UNKNOWN",
            }
        )
    return output


def _portal_login_evidence(reader: BundleReader) -> list[dict[str, Any]]:
    """Export structural login facts only, without headers, form values or URLs."""
    result = []
    entry_seen = False
    for row in reader.jsonl("capture_manifest.jsonl"):
        if str(row.get("live_test_step", "")).startswith("network."):
            continue
        method, path, _, _ = request_parts(row.get("request") or {})
        if path == "/index.do" and method == "GET":
            entry_seen = True
        if path != "/login.do" or method != "POST" or row.get("kind") != "HTTP_EXCHANGE":
            continue
        response = row.get("response") or {}
        body = reader.read(row["response_file"]) if row.get("response_file") else b""
        headers = {
            str(key).casefold(): str(value)
            for key, value in (row.get("request") or {}).get("headers", [])
        }
        result.append(
            {
                "http_status": int(response.get("status_code", 0)),
                "response_bytes": len(body),
                "response_blank": not body.strip(),
                "entry_page_requested_before_login": entry_seen,
                "origin_header_present": "origin" in headers,
                "referer_is_entry_page": urlsplit(headers.get("referer", "")).path == "/index.do",
            }
        )
    return result


def _step_query(step: dict[str, Any]) -> str:
    # Source-level probes are observations, never Service result coverage.
    if step.get("name") in {"password.unauthenticated_catalog", "password.existing_cookie_catalog"}:
        return ""
    key = step.get("operation")
    if key in QUERY_BY_KEY:
        return key
    name = str(step.get("name", ""))
    if name in _LEGACY_STEPS:
        return _LEGACY_STEPS[name]
    match = re.fullmatch(r"(?:latest|visit_\d+)_(.+)", name)
    return _CASE_STEPS.get(match[1], "") if match else ""


def _safe_code(value: Any) -> str:
    candidate = str(value or "")
    return candidate if re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", candidate) else ""


def _safe_build(value: dict[str, Any]) -> dict[str, Any]:
    version = str(value.get("sdk_version") or "")
    build_id = str(value.get("build_id") or "")
    return {
        "sdk_version": version if re.fullmatch(r"\d+\.\d+\.\d+", version) else "unknown",
        "build_id": build_id if re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", build_id) else None,
    }


def _structured_root_issue(issue: dict[str, Any]) -> dict[str, Any]:
    current = issue
    for _ in range(7):
        linked = current.get("cause")
        if not isinstance(linked, dict) or not _safe_code(linked.get("code")):
            break
        current = linked
    return current


def _phase(code: str) -> str:
    if code.startswith(("NETWORK_", "TLS_", "CA_")):
        return "CONNECTIVITY"
    if code in {"WEBMAAS_SESSION_TIMEOUT", "WEBMAAS_SSO_RECOVERY_FAILED"} or code.startswith(("AUTH_", "PORTAL_", "SSO_", "SECTORD_")):
        return "AUTHENTICATION"
    if code in {"WEBMAAS_QUERY_FORM_MISSING", "WEBMAAS_QUERY_TOKEN_MISSING"}:
        return "PARSE"
    if code.startswith("HTTP_"):
        return "HTTP"
    if any(term in code for term in ("PARSE", "INVALID", "STRUCTURE", "CONTAINER", "REPLAY_")):
        return "PARSE"
    if (
        "MISSING" in code
        or "EMPTY" in code
        or code in {"QUERY_INPUT_UNAVAILABLE", "NO_MATCHING_VISIT"}
    ):
        return "DATA"
    return "EXECUTION"


def _next_action(main: dict[str, str] | None, *, has_gaps: bool = False) -> str:
    if main is None:
        if has_gaps:
            return "未發現未恢復的執行錯誤；NO_SAMPLE 表示缺少欄位或候選資料，部分解析另列於 data_quality，均保留尚未完成的範圍。需要驗證缺樣本項目時，先準備相應樣本；不必只為同一份缺樣本資料重跑 EXE。"
        return "檢查操作驗證矩陣；NOT_TESTED 與 EMPTY 尚未證實能取得目標資料。"
    code = main["code"]
    if code.startswith(("ORDER_ASSET_", "PACS_DOWNLOAD_", "PACS_SUMMARY_", "PACS_REFERENCE", "PACS_ACCOUNT_")):
        return "先保留原 ZIP，核對 stage2 的圖片參照、stage3 的 outcome／metadata 與實際檔案；摘要計數不能替代完整性證據。原實測狀態保留，缺檔或不一致不自動重送登入／下載。"
    if code == "TLS_VERIFY_FAILED":
        return "已收到憑證驗證失敗證據；檢查 Windows 信任庫、憑證主機名稱與有效期，需要時使用院內提供的 CA PEM。新版 comprehensive 預設比較略過驗證的 HTTPS，連線成功就同輪繼續登入／查詢。不能推論帳密錯誤。"
    if code in {"TLS_EOF", "TLS_PROTOCOL_FAILED", "NETWORK_TLS_FAILED"}:
        return "雙擊新版 EXE 重跑 comprehensive：先測正常憑證驗證、TLS 1.2 與 AES／RSA 相容套件；全部失敗時比較略過憑證驗證及 Windows Schannel。預設允許採用能連線的未驗證 HTTPS，在同輪繼續 SSO／查詢，並標示套用的服務。TLS_EOF 本身尚不能判定是 CA、帳密或特定套件問題，略過驗證也可能仍然失敗。"
    if code == "PORTAL_LOGIN_RESPONSE_EMPTY":
        return "登入 HTTP 回應只有空白，不能判定帳密正確或錯誤。新版先載入 index.do、保留同一個 Session／隱藏欄位並補上 Origin／Referer；仍須在內網雙擊新版 EXE 驗證。"
    if code == "PORTAL_PASSWORD_CHANGE_REQUIRED":
        return "回應明確要求變更密碼；先透過院方入口完成變更，再以新密碼執行 SDK 或 EXE。SDK 不會自動變更密碼，也不會因這個錯誤重新送出登入。"
    if code == "WEBMAAS_SESSION_TIMEOUT":
        return "WebMAAS 明確回傳 timeout 頁；這不證明 Portal 過期或自然 TTL。檢查 session_test 的原錯誤、獨立 SSO 複查及直接 API 恢復結果；新版唯讀查詢最多重建 WebMAAS SSO 一次，原始錯誤仍保留。"
    if main["phase"] == "CONNECTIVITY":
        return "先重測 auth profile，依錯誤碼檢查內網連線、DNS、Proxy 或逾時設定。"
    if main["phase"] == "AUTHENTICATION":
        return "依登入／SSO 表定位失敗的子系統，檢查該服務的表單與轉址；其他服務已通過的查詢仍是有效實測證據。修正後重測該子系統及其查詢。"
    return "使用 capture_id 對照原始回應，修改 parser 後重新執行離線分析，再攜帶新版 EXE 與 retest-config.json 重測。"
