"""Check PACS reference-to-file completeness without HTTP or live execution imports."""

from __future__ import annotations

from typing import Any

from ..contracts.order_assets import ORDER_WORKFLOW_PATH
from ..core.errors import ConfigurationError
from ..models import PacsImageRef
from .assets import verify_saved_order_asset
from .bundle import BundleReader

_MODES = ("imgUrl", "uid")
_TARGETS = {"union": "imgUrl", "vghks": "uid"}
_SUMMARY = ORDER_WORKFLOW_PATH + "pacs_verification.json"
_ORDERS = ORDER_WORKFLOW_PATH + "stage2_orders/"
_ASSETS = ORDER_WORKFLOW_PATH + "stage3_assets/"
_UPSTREAM = {"prq.order_history", "prq.order_detail", "prq.order_report", "prq.pacs_study"}


def pacs_test_summary(reader: BundleReader, steps: list[dict]) -> dict[str, Any]:
    """Recompute each mode from unique references, outcomes and actual saved bytes.

    Original EXE statuses are separate from this audit. Only fixed labels,
    counters and issue codes leave this helper; no patient or image identifiers.
    Hospital labels describe the supplied test account, not inferred affiliation.
    """
    run_summary = reader.json("run_summary.json")
    profile = run_summary.get("profile")
    has_checks = any(step.get("name") in {"pacs.imgUrl_sample", "pacs.uid_sample"} for step in steps)
    if _SUMMARY not in reader.names and not has_checks and (
        profile != "pacs" or not any(name.startswith(_ORDERS) for name in reader.names)
    ):
        return {"status": "NOT_RUN", "issues": []}
    issues: list[dict[str, str]] = []

    def issue(code: str):
        value = {"operation": "prq.pacs_image", "code": code}
        if value not in issues:
            issues.append(value)

    def read(name, code):
        try:
            value = reader.json(name)
        except ConfigurationError:
            value = None
        if not isinstance(value, dict):
            issue(code)
            return {}
        return value

    recorded = read(_SUMMARY, "PACS_SUMMARY_MISSING" if _SUMMARY not in reader.names else "PACS_SUMMARY_INVALID")
    recorded_modes = recorded.get("modes", {})
    if not isinstance(recorded_modes, dict):
        issue("PACS_SUMMARY_INVALID")
        recorded_modes = {}
    config = reader.json("run_config.json") if "run_config.json" in reader.names else {}
    download_assets = config.get("download_assets", recorded.get("download_assets", True))
    if type(download_assets) is not bool:
        issue("PACS_SUMMARY_INVALID")
        download_assets = True
    elif recorded and recorded.get("download_assets") is not download_assets:
        issue("PACS_SUMMARY_MISMATCH")
    account = {}
    if "pacs_account.json" in reader.names:
        raw_account = read("pacs_account.json", "PACS_ACCOUNT_INVALID")
        hospital = raw_account.get("hospital")
        if not isinstance(hospital, str) or _TARGETS.get(hospital) != raw_account.get("expected_mode"):
            issue("PACS_ACCOUNT_INVALID")
        else:
            account = {"hospital": hospital, "expected_mode": _TARGETS[hospital]}
            embedded = run_summary.get("pacs_account")
            if embedded is not None and (not isinstance(embedded, dict) or any(
                embedded.get(key) != value for key, value in account.items()
            )):
                issue("PACS_ACCOUNT_MISMATCH")

    def reference(value):
        try:
            return PacsImageRef(**value) if isinstance(value, dict) else None
        except (ConfigurationError, TypeError, ValueError, AttributeError):
            return None

    refs: set[PacsImageRef] = set()
    patient_mrn = run_summary.get("test_mrn")
    for name in sorted(reader.names):
        if not name.startswith(_ORDERS) or not name.endswith(".json"):
            continue
        row = read(name, "PACS_REFERENCES_INVALID")
        values = row.get("image_refs", [])
        if not isinstance(values, list):
            issue("PACS_REFERENCES_INVALID")
            continue
        for value in values:
            ref = reference(value)
            if ref is None:
                issue("PACS_REFERENCES_INVALID")
            else:
                refs.add(ref)
                if isinstance(patient_mrn, str) and patient_mrn and ref.mrn != patient_mrn:
                    issue("PACS_REFERENCE_PATIENT_MISMATCH")
    outcomes = {}
    for name in sorted(reader.names):
        if not name.startswith(_ASSETS) or not name.endswith(".outcome.json"):
            continue
        row = read(name, "PACS_DOWNLOAD_OUTCOME_INVALID")
        if row.get("operation") != "prq.pacs_image":
            continue
        ref = reference(row.get("ref"))
        if ref is None or not isinstance(row.get("status"), str) or row["status"] not in {"OK", "ERROR", "PENDING"}:
            issue("PACS_DOWNLOAD_OUTCOME_INVALID")
            continue
        if ref in outcomes:
            issue("PACS_DOWNLOAD_DUPLICATE")
        else:
            outcomes[ref] = (name, row)
        if ref not in refs:
            issue("PACS_DOWNLOAD_UNEXPECTED")
    blocked = any(step.get("operation") in _UPSTREAM and step.get("status") in {"ERROR", "BLOCKED"}
                  for step in steps)
    modes = {}
    for mode in _MODES:
        selected = {ref for ref in refs if ("imgUrl" if ref.img_url else "uid") == mode}
        audits = [outcomes[ref] for ref in selected if ref in outcomes]
        successful = [(name, row) for name, row in audits if row["status"] == "OK"]
        errors = sum(row["status"] == "ERROR" for _, row in audits)
        missing = len(selected) - len(successful) - errors
        verified_bytes = []
        for name, row in successful:
            code = verify_saved_order_asset(reader, name, row, "prq.pacs_image")
            if code:
                issue(code)
            else:
                verified_bytes.append(row["byte_count"])
        status = ("BLOCKED" if not selected and blocked else "NO_SAMPLE" if not selected else
                  "SKIPPED" if not download_assets else
                  "ERROR" if errors or missing else "OK")
        if missing and download_assets:
            issue("PACS_DOWNLOAD_MISSING")
        original = recorded_modes.get(mode, {})
        if not isinstance(original, dict):
            original = {}
        counts = {"references": len(selected), "downloaded_files": len(successful), "download_errors": errors,
                  "downloaded_bytes": sum(row.get("byte_count", 0) for _, row in successful
                                          if type(row.get("byte_count")) is int)}
        if recorded and (original.get("status") != status or any(
            type(original.get(key)) is not int or original[key] != value for key, value in counts.items()
        )):
            issue("PACS_SUMMARY_MISMATCH")
        modes[mode] = {"recorded_status": original.get("status") if isinstance(original.get("status"), str) and original["status"] in {
            "OK", "ERROR", "BLOCKED", "NO_SAMPLE", "SKIPPED"} else "UNKNOWN",
            "status": "ERROR" if len(verified_bytes) != len(successful) else status,
            **counts, "verified_files": len(verified_bytes), "verified_bytes": sum(verified_bytes),
            "missing_downloads": missing}
    return {
        "status": "ERROR" if issues else "OBSERVED", "account": account, "modes": modes,
        "expected_mode_status": ("ERROR" if issues else modes[account["expected_mode"]]["status"])
        if account else "NOT_ASSIGNED",
        "issues": issues,
        "scope": "Unique saved references, outcomes, metadata, SHA-256 and JPEG boundaries; original live results unchanged. No OCR, full image decoding or new intranet requests.",
    }
