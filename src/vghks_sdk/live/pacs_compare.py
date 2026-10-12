"""Sequential PACS verification with independent per-hospital return bundles."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..core.config import PortalCredentials
from ..core.errors import ConfigurationError
from ..local_io import restrict_permissions, write_json_atomic
from ..models import PacsStudyRef
from .bundle import LiveTestBundleManager
from .config import LiveTestConfig
from .runner import LiveTestExecution, execute_live_test, package_bootstrap_failure


@dataclass(frozen=True, slots=True)
class PacsComparisonExecution:
    status: str
    exit_code: int
    executions: tuple[LiveTestExecution, ...]
    report_path: Path | None


def execute_pacs_comparison(
    config: LiveTestConfig,
    union_credentials: PortalCredentials,
    vghks_credentials: PortalCredentials,
    *,
    vghks_mrn: str,
    bundle_manager: LiveTestBundleManager,
    executable_directory: Path,
    overwrite: bool = False,
) -> PacsComparisonExecution:
    """Validate both inputs before starting; a failed account never replaces the other."""
    if config.profile != "pacs":
        raise ConfigurationError("PACS comparison requires the pacs profile")
    union_credentials = union_credentials.validate()
    vghks_credentials = vghks_credentials.validate()
    # Reuse the reference boundary for both MRNs before any password POST.
    for mrn in (config.test_mrn, vghks_mrn):
        PacsStudyRef(mrn, "INPUT-VALIDATION")
    configs = (
        replace(config, pacs_single_account=True).with_default_doctor(union_credentials.username),
        replace(config, test_mrn=vghks_mrn, doctor_card=None, pacs_single_account=True).with_default_doctor(vghks_credentials.username),
    )
    for item in configs:
        item.validate_for_execution()
    rows: list[dict[str, Any]] = []
    executions: list[LiveTestExecution] = []
    for index, (hospital, title, mode, credentials, account_config) in enumerate((
        ("union", "聯合醫院", "imgUrl", union_credentials, configs[0]),
        ("vghks", "高榮", "uid", vghks_credentials, configs[1]),
    ), 1):
        if executions and executions[-1].status == "INTERRUPTED":
            rows.append({"hospital": hospital, "expected_mode": mode, "status": "NOT_RUN",
                         "expected_mode_status": "NOT_RUN", "reason": "USER_INTERRUPTED"})
            break
        print(f"\n[{index}/2] {title}: 開始 PACS 測試 ({mode}), 使用獨立 Session。")
        account = {"schema_version": 1, "hospital": hospital, "expected_mode": mode, "sequence": index}
        try:
            manager = bundle_manager if index == 1 else LiveTestBundleManager(
                bundle_manager.run_directory.with_name(bundle_manager.run_directory.name + "-vghks"),
                archive_directory=bundle_manager.archive_directory, archive_label=hospital,
                overwrite=overwrite,
            )
        except Exception:
            rows.append({**account, "status": "BOOTSTRAP_FAILED", "exit_code": 2,
                         "expected_mode_status": "NOT_RUN", "error_code": "PACS_ACCOUNT_OUTPUT_FAILED"})
            print(f"{title}: 無法建立結果目錄; 先前 ZIP 已保留。")
            continue
        try:
            write_json_atomic(manager.run_directory / "pacs_account.json", account)
        except Exception as exc:
            execution = package_bootstrap_failure(exc, bundle_manager=manager)
        else:
            execution = execute_live_test(
                account_config, credentials, executable_directory=executable_directory,
                bundle_manager=manager, announce_archive=False,
            )
        executions.append(execution)
        verification = manager.run_directory / "parsed/workflows/ophthalmology_orders/pacs_verification.json"
        try:
            evidence = json.loads(verification.read_text(encoding="utf-8")) if verification.is_file() else {}
        except (OSError, ValueError):
            evidence = {}
        modes = {key: {field: value.get(field) for field in (
            "status", "references", "downloaded_files", "download_errors", "downloaded_bytes",
        )} for key, value in evidence.get("modes", {}).items()}
        rows.append({**account, "status": execution.status, "exit_code": execution.exit_code,
                     "error_code": execution.error_code,
                     "expected_mode_status": modes.get(mode, {}).get("status", "BLOCKED"),
                     "modes": modes, "run_id": manager.run_id,
                     "partial_report_steps": sum(
                         (step.details or {}).get("acquisition_status") == "PARTIAL"
                         for step in execution.result.steps
                     ) if execution.result else 0,
                     "archive": execution.archive.archive_path.name if execution.archive else None})
        print(f"{title}: {execution.status}; {mode} = {rows[-1]['expected_mode_status']}")
        if execution.archive:
            print(f"{title} ZIP: {execution.archive.archive_path}")

    interrupted = any(item.exit_code == 130 for item in executions)
    failed = any(row.get("exit_code", 0) for row in rows)
    gap = any(row["expected_mode_status"] != "OK" or row.get("partial_report_steps", 0) for row in rows)
    status = "INTERRUPTED" if interrupted else "COMPLETED_WITH_ERRORS" if failed else "COMPLETED_WITH_GAPS" if gap else "OK"
    exit_code = 130 if interrupted else max(row.get("exit_code", 0) for row in rows)
    report = {"schema_version": 1, "profile": "pacs", "status": status, "exit_code": exit_code,
              "scope": ["union.imgUrl", "vghks.uid"], "accounts": rows,
              "credentials_included": False, "sessions": "independent_per_hospital"}
    report_path = bundle_manager.archive_directory / f"vghks-pacs-comparison-{bundle_manager.run_id}.json"
    try:
        write_json_atomic(report_path, report)
        text_path = report_path.with_suffix(".txt")
        text = [f"PACS 雙院測試: {status}", "目標: 聯合醫院 imgUrl、高榮 uid", ""]
        for row in rows:
            text.append(f"{row['hospital']}: {row['status']}; {row['expected_mode']} = {row['expected_mode_status']}")
            text.append(f"ZIP: {row.get('archive') or '未產生; 請保留 run directory'}")
        text.extend(("", "請帶回 union 與 vghks 兩份 ZIP。缺樣本、未執行及失敗均不代表下載通過。"))
        text_path.write_text("\n".join(text) + "\n", encoding="utf-8")
        restrict_permissions(text_path)
    except Exception:
        print("雙院摘要無法存檔; 請保留上方兩院 ZIP 與 run directory。")
        status, exit_code, report_path = "PACKAGING_FAILED", 2, None
    print(f"\nPACS 雙院結果: {status}。請帶回 union 與 vghks 兩份 ZIP。")
    if report_path:
        print(f"雙院摘要: {report_path.with_suffix('.txt')}")
    return PacsComparisonExecution(status, exit_code, tuple(executions), report_path)
