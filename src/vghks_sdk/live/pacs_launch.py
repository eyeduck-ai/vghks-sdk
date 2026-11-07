"""Explicit, single-examination PACS handoff verification; never an automatic profile."""

# ruff: noqa: RUF001

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from collections.abc import Callable
from dataclasses import fields
from getpass import getpass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .._version import __version__
from ..core.config import PortalCredentials, RequestPolicy, SDKSettings
from ..core.diagnostics import DiagnosticRecorder
from ..core.errors import ConfigurationError, error_info
from ..identifiers import normalize_mrn
from ..local_io import write_json_atomic
from ..models import to_jsonable
from ..models._validation import _validate_navigation_ref
from ..models.pacs_launch import PacsLaunchRef
from ..sdk import VghksSDK
from .bundle import LiveTestBundleManager
from .capture import RawCaptureRecorder
from .pacs_launch_capture import NativeCapture as NativeCapture
from .pacs_launch_capture import PacsLaunchCapture as PacsLaunchCapture
from .pacs_launch_discovery import PacsLaunchDiscovery, select_pacs_launch_candidate

_OBSERVATIONS = frozenset({"MATCHED", "NOT_LOADED", "WRONG_STUDY", "NOT_CHECKED"})


def build_plan(*, open_viewer: bool = False, discover_orders: bool = False) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "profile": "pacs_launch",
        "sdk_version": __version__,
        "max_patients": 1,
        "max_examinations": 1,
        "negative_password_post_limit": 0,
        "explicit_login_calls": 1,
        "prepare_calls": 1,
        "native_handoff_calls": 1 if open_viewer else 0,
        "local_availability_checks": 1 if open_viewer else 0,
        "native_retry": False,
        "native_success_means": "HTTP_HANDOFF_ONLY",
        "viewer_loaded_confirmation": "SEPARATE_MANUAL_OBSERVATION",
        "raw_scope": ["ORDER_DISCOVERY_DOCUMENTS", "PACS_DOCUMENTS", "NATIVE_EXCHANGES"],
        "password_capture": False,
        "order_selection": "DISCOVER_AND_SELECT" if discover_orders else "EXPLICIT_REFERENCE",
        "lookback_days": 4000 if discover_orders else None,
        "discovery_batch_size": 30 if discover_orders else None,
        "prepare_only_selected_order": True,
    }


def run_pacs_launch_test(
    sdk: Any,
    reference: PacsLaunchRef | None = None,
    *,
    output_dir: Path,
    mrn: str | None = None,
    choose: Callable[[Any], str] | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    open_viewer: bool = False,
    client_factory: Callable[..., Any] | None = None,
    bridge_url: str = "http://127.0.0.1:15516",
    download_app_path: str | None = None,
    observe: Callable[[], str] | None = None,
    raw_capture: RawCaptureRecorder | None = None,
) -> dict[str, Any]:
    """Run once, saving every completed stage, including failure or interruption.

    ``observe`` records the operator's inspection, never a preselected success.
    The returned summary contains no patient, account, UID, or bridge URL.
    """
    if reference is not None and not isinstance(reference, PacsLaunchRef):
        raise ConfigurationError("PACS launch test requires a PacsLaunchRef")
    if reference is not None and mrn is not None:
        raise ConfigurationError("use a reference or patient discovery, not both")
    if reference is None:
        _validate_navigation_ref(mrn)
        if choose is None:
            raise ConfigurationError("PACS order discovery requires operator selection")
    root = Path(output_dir)
    summary: dict[str, Any] = {
        "schema_version": 1, "profile": "pacs_launch", "sdk_version": __version__,
        "status": "RUNNING", "exit_code": 2,
        "login": "NOT_RUN", "prepare": "NOT_RUN", "handoff": "NOT_REQUESTED",
        "native_requested": open_viewer, "native_result": None,
        "manual_observation": "NOT_CHECKED", "issue": None,
        "evidence": "SDK_EXECUTION", "native_calls": 0,
        "discovery": None, "selection": "NOT_RUN" if reference is None else "EXPLICIT_REFERENCE",
        "selection_index": None, "discovery_stop_reason": None,
        "bridge_availability": None,
    }
    write_json_atomic(root / "test_plan.json", build_plan(
        open_viewer=open_viewer, discover_orders=reference is None,
    ))
    client = None
    phase = "login"

    def save() -> None:
        write_json_atomic(root / "run_summary.json", summary)

    try:
        save()
        if raw_capture is not None:
            raw_capture.set_live_step("pacs_launch.login")
        sdk.auth.login()
        summary["login"] = "OK"
        save()
        if reference is None:
            phase = "discovery"
            discovery = PacsLaunchDiscovery(
                sdk, mrn, output_dir=root, on_progress=progress, raw_capture=raw_capture,
            )
            while True:
                found = discovery.scan_next(batch_size=30)
                summary["discovery"] = found.summary
                save()
                if found.summary["status"] in {"ERROR", "AUTHENTICATION_FAILED", "INTERRUPTED"}:
                    interrupted = found.summary["status"] == "INTERRUPTED"
                    summary.update(status="INTERRUPTED" if interrupted else "DISCOVERY_FAILED",
                                   exit_code=130 if interrupted else 2)
                    return summary
                if not found.candidates and not found.summary["pending"]:
                    failed = bool(found.summary["error_count"])
                    incomplete = bool(found.summary.get("gap_count"))
                    summary.update(status="DISCOVERY_FAILED" if failed else
                                   "DISCOVERY_INCOMPLETE" if incomplete else "NO_SAMPLE",
                                   exit_code=2 if failed else 0, selection="NO_CANDIDATES")
                    return summary
                phase = "selection"
                selected = choose(found).strip().casefold()
                if selected in {"", "0"}:
                    summary["discovery_stop_reason"] = "USER_CANCELLED"
                    summary.update(status="CANCELLED", exit_code=0, selection="CANCELLED")
                    return summary
                if selected == "m" and found.summary["pending"]:
                    phase = "discovery"
                    continue
                try:
                    candidate = select_pacs_launch_candidate(found, int(selected))
                except (ValueError, ConfigurationError):
                    # A custom chooser may supply invalid input; never make a ref
                    # from untrusted selection text or silently choose the first row.
                    raise ConfigurationError("invalid PACS selection", code="PACS_SELECTION_INVALID") from None
                reference = candidate.reference
                summary["selection"] = "SELECTED"
                summary["selection_index"] = int(selected)
                summary["discovery_stop_reason"] = (
                    "USER_SELECTED_BEFORE_END" if found.summary["pending"] else "ALL_ORDERS_SCANNED"
                )
                write_json_atomic(root / "private/selected_candidate.json", candidate)
                break
        write_json_atomic(root / "private/selected_order.json", {
            "mrn": reference.mrn, "request_no": reference.request_no,
        })
        if open_viewer:
            phase = "availability"
            if client_factory is None:
                from ..integrations.smartiris import SmartIrisClient
                client_factory = SmartIrisClient
            options: dict[str, Any] = {"bridge_url": bridge_url,
                                       "capture": NativeCapture(root / "private/native")}
            if download_app_path is not None:
                options["download_app_path"] = download_app_path
            client = client_factory(**options)
            availability = client.check_availability()
            summary["bridge_availability"] = to_jsonable(availability)
            save()
            if availability.status != "AVAILABLE":
                summary.update(status="BRIDGE_UNAVAILABLE" if availability.status == "UNAVAILABLE"
                               else "BRIDGE_UNCONFIRMED", exit_code=2)
                return summary
        phase = "prepare"
        if raw_capture is not None:
            raw_capture.set_live_step("pacs_launch.prepare")
        context = sdk.orders.prepare_pacs_launch(reference)
        summary["prepare"] = "OK"
        write_json_atomic(root / "parsed/launch_context_summary.json", context)
        save()
        if not open_viewer:
            gaps = bool((summary["discovery"] or {}).get("error_count"))
            summary.update(status="PREPARED_WITH_GAPS" if gaps else "PREPARED",
                           exit_code=2 if gaps else 0)
            return summary
        phase = "handoff"
        summary["native_calls"] = 1
        summary["handoff"] = "STARTED"
        save()
        result = client.open_study(context)
        summary["native_result"] = to_jsonable(result)
        summary["handoff"] = result.status
        save()
        if result.status != "HANDOFF_SENT":
            summary.update(status="ERROR", exit_code=2)
            return summary
        phase = "observation"
        if observe is not None:
            observation = observe()
            if observation not in _OBSERVATIONS:
                raise ConfigurationError("unknown PACS observation", code="PACS_OBSERVATION_INVALID")
            summary["manual_observation"] = observation
        if summary["manual_observation"] in {"NOT_LOADED", "WRONG_STUDY"}:
            summary.update(status="OBSERVATION_FAILED", exit_code=2)
        else:
            summary.update(status="HANDOFF_SENT", exit_code=0)
        if summary["status"] == "HANDOFF_SENT" and (summary["discovery"] or {}).get("error_count"):
            summary.update(status="HANDOFF_SENT_WITH_GAPS", exit_code=2)
        return summary
    except KeyboardInterrupt:
        if phase == "handoff" and client is not None:
            last_result = getattr(client, "last_result", None)
            summary["native_result"] = to_jsonable(last_result)
            summary["handoff"] = getattr(last_result, "status", "OUTCOME_UNKNOWN")
        summary.update(status="INTERRUPTED", exit_code=130)
        return summary
    except Exception as exc:
        summary["issue"] = to_jsonable(error_info(exc))
        summary.update(status="ERROR", exit_code=2)
        if phase in {"login", "prepare"}:
            summary[phase] = "ERROR"
        if phase == "handoff" and summary["native_result"] is None:
            summary["handoff"] = "OUTCOME_UNKNOWN"
        if raw_capture is not None:
            with contextlib.suppress(Exception):
                raw_capture.record_error(error=exc, step=f"pacs_launch.{phase}")
        return summary
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()
        try:
            save()
        except Exception as exc:
            # Preserve the known side-effect result in memory for the outer
            # runner even when storage remains unavailable. Never resend HTTP.
            summary["persistence_issue"] = to_jsonable(error_info(exc))
            summary.update(status="OUTPUT_WRITE_FAILED", exit_code=2)


def _loopback_settings(path: Path | None) -> tuple[SDKSettings, str, str | None]:
    """A synthetic-only override cannot leave any SDK endpoint on the intranet."""
    if path is None:
        return SDKSettings(), "http://127.0.0.1:15516", None
    data = json.loads(path.read_text(encoding="utf-8"))
    required_keys = {"endpoint_overrides", "bridge_url", "ca_bundle"}
    if (not isinstance(data, dict) or not required_keys <= set(data)
            or set(data) - required_keys - {"download_app_path"}):
        raise ConfigurationError("invalid synthetic PACS config", code="PACS_TEST_CONFIG_INVALID")
    endpoints = data["endpoint_overrides"]
    required = {item.name for item in fields(SDKSettings) if item.name.endswith("_base_url")}
    if not isinstance(endpoints, dict) or set(endpoints) != required:
        raise ConfigurationError("every SDK endpoint must be overridden", code="PACS_TEST_CONFIG_INVALID")
    for value in [*endpoints.values(), data["bridge_url"]]:
        if not isinstance(value, str):
            raise ConfigurationError("invalid loopback endpoint", code="PACS_TEST_CONFIG_INVALID")
        address = urlsplit(value)
        if (address.scheme not in {"http", "https"}
                or address.hostname not in {"localhost", "127.0.0.1", "::1"}
                or address.username or address.password or address.query or address.fragment):
            raise ConfigurationError("synthetic endpoints must be loopback", code="PACS_TEST_CONFIG_INVALID")
    settings = SDKSettings(
        **endpoints, ca_bundle=data["ca_bundle"], allow_unverified_tls=False,
        request_policy=RequestPolicy(
            min_delay_seconds=0, max_delay_seconds=0, max_attempts=1,
            connect_timeout_seconds=1, read_timeout_seconds=2,
        ),
    )
    return settings, data["bridge_url"], data.get("download_app_path")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mrn", help="one explicitly selected patient's MRN")
    parser.add_argument("--request-no", help="PACS button reqno / AccNo")
    parser.add_argument("--username", help="Portal account (password is prompted or read from environment)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--open-viewer", action="store_true", help="send one local SmartIris handoff")
    mode.add_argument("--prepare-only", action="store_true", help="prepare the selected study without local handoff")
    interaction = parser.add_mutually_exclusive_group()
    interaction.add_argument("--non-interactive", action="store_true", help="require MRN and request-no; no prompts")
    interaction.add_argument("--interactive", action="store_true", help="enable selection prompts, including with redirected stdin")
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--plan", action="store_true", help="print the plan without any HTTP")
    parser.add_argument("--self-check", action="store_true", help="check imports without any HTTP")
    parser.add_argument("--loopback-config", type=Path, help=argparse.SUPPRESS)
    return parser


def _manual_observation() -> str:
    print("請在 SmartIris 核對病人與檢查：1=相符且已顯示，2=未載入，3=病人或檢查不符，0=未確認。")
    while True:
        value = input("觀察結果 [0]: ").strip()
        if value in {"", "0", "1", "2", "3"}:
            return {"1": "MATCHED", "2": "NOT_LOADED", "3": "WRONG_STUDY"}.get(value, "NOT_CHECKED")


def _discovery_progress(value: dict[str, Any]) -> None:
    if value.get("scanned") is not None:
        print(f"已確認 {value['scanned']} / {value.get('total', '?')} 筆醫囑…", flush=True)


def _display_text(value: Any) -> str:
    # Source labels should never clear or move the console or span menu rows.
    return " ".join("".join(c if c.isprintable() else " " for c in str(value or "")).split())


def _choose_order(found: Any, *, open_viewer: bool) -> str:
    print("\n已確認有 PACS 按鈕的醫囑：")
    for number, candidate in enumerate(found.candidates, 1):
        date_label = candidate.execution_date or candidate.order_date or "日期未提供"
        print(f"{number:>3}. {_display_text(date_label)} | {_display_text(candidate.name)}"
              f" | {_display_text(candidate.department)} | 檢查單號 {_display_text(candidate.reference.request_no)}")
    if not found.candidates:
        print("目前這批尚未找到 PACS 按鈕。")
    if found.summary["error_count"]:
        print(f"有 {found.summary['error_count']} 項查詢／解析問題，已保存；清單可能不完整。")
    pending = found.summary["pending"]
    if pending:
        print(f"尚有 {pending} 筆醫囑未確認；輸入 m 繼續查下一批。")
    action = "開啟檢閱器" if open_viewer else "取得啟動資訊"
    while True:
        value = input(f"輸入編號即{action}；0 或 Enter 取消：").strip().casefold()
        if value in {"", "0"} or (value == "m" and pending):
            return value
        try:
            select_pacs_launch_candidate(found, int(value))
        except (ValueError, ConfigurationError):
            print("請輸入清單中的編號" + ("、m" if pending else "") + "，或 0 取消。")
        else:
            return value


def _print_result(summary: dict[str, Any], archive: Any) -> None:
    messages = {
        "PREPARED": "已取得所選檢查的啟動資訊。",
        "PREPARED_WITH_GAPS": "已取得所選檢查的啟動資訊；醫囑查詢另有問題，已保存供分析。",
        "HANDOFF_SENT": "已完成本機交接；影像是否正確顯示，以你填寫的觀察結果為準。",
        "HANDOFF_SENT_WITH_GAPS": "已完成本機交接；醫囑查詢另有問題，已保存供分析。",
        "OBSERVATION_FAILED": "已保存影像未載入或不符的觀察結果。",
        "NO_SAMPLE": "已查詢的醫囑沒有可用 PACS 按鈕，本次未開啟檢閱器。",
        "CANCELLED": "已取消選擇，本次未開啟檢閱器。",
        "INTERRUPTED": "測試已中斷，已保存完成的步驟。",
        "DISCOVERY_FAILED": "醫囑查詢未完成，已保存問題，本次未開啟檢閱器。",
        "DISCOVERY_INCOMPLETE": "來源報告解析有缺漏，尚未確認可用 PACS 項目，已保存供分析。",
        "OUTPUT_WRITE_FAILED": "測試期間曾無法寫入部分結果；已保存可取得的狀態，請確認原始結果目錄。",
        "BRIDGE_UNAVAILABLE": "DownloadApp 尚未就緒，本次未取得啟動資訊或送出本機開啟指令。",
        "BRIDGE_UNCONFIRMED": "無法確認 DownloadApp 與連接埠持有者，本次未送出開啟指令。",
    }
    print("\n" + messages.get(summary["status"], "測試未完成，已保存可取得的結果。"))
    issue = summary.get("issue") or {}
    if issue.get("code"):
        print(f"錯誤碼：{issue['code']}")
    availability = summary.get("bridge_availability")
    if availability is not None and availability.get("status") != "AVAILABLE":
        print(f"本機檢查：檔案 {availability['installation']}；程序 {availability['process']}；"
              f"連接埠 {availability['listener']}；持有者 {availability['owner']}")
        print("請確認原系統的 DownloadApp 已啟動，並使用預期的本機安裝位置。")
    discovery = summary.get("discovery")
    if discovery is not None and discovery.get("pending"):
        print(f"本次確認 {discovery['scanned']} / {discovery['total']} 筆醫囑，"
              f"尚有 {discovery['pending']} 筆未掃描。")
        if summary.get("discovery_stop_reason") == "USER_SELECTED_BEFORE_END":
            print("已依所選醫囑結束掃描；清單未掃完與本機交接結果分開記錄。")
    print(f"測試結果 ZIP：{archive.archive_path}")
    print(f"原始結果目錄：{archive.run_directory}" if hasattr(archive, "run_directory") else "")


def _main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    interactive = (args.interactive or sys.stdin.isatty()) and not args.non_interactive
    open_viewer = args.open_viewer or (
        interactive and not args.request_no and not args.prepare_only
    )
    if args.plan:
        print(json.dumps(build_plan(open_viewer=open_viewer, discover_orders=not args.request_no), indent=2))
        return 0
    if args.self_check:
        from ..integrations.smartiris import SmartIrisClient
        print(json.dumps({"sdk_version": __version__, "profile": "pacs_launch",
                          "native_client": SmartIrisClient.__name__, "network_used": False}))
        return 0
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path.cwd()
    manager = LiveTestBundleManager.create_auto(
        args.output_root or base / "pacs-launch-results", archive_label="pacs-launch",
    )
    capture = PacsLaunchCapture(manager.run_directory, prepare_root=False, run_id=manager.run_id)
    diagnostics = DiagnosticRecorder(manager.diagnostics_directory, run_id=manager.run_id)
    summary: dict[str, Any] = {"schema_version": 1, "profile": "pacs_launch",
                               "status": "BOOTSTRAP_FAILED", "exit_code": 2}
    sdk = None
    try:
        settings, bridge_url, download_app_path = _loopback_settings(args.loopback_config)
        username = args.username or os.environ.get("VGHKS_USERNAME", "")
        mrn = args.mrn or ""
        request_no = args.request_no or ""
        if interactive:
            print(f"PACS 院內測試 {__version__}：輸入病歷號後，選擇一筆醫囑開啟。")
            username = username or input("Portal 帳號: ").strip()
        password = os.environ.get("VGHKS_PASSWORD", "")
        if interactive:
            if not password:
                password = getpass("Portal 密碼: ")
            mrn = mrn or input("本次授權病歷號: ").strip()
        if not interactive and not request_no:
            raise ConfigurationError("non-interactive mode requires --request-no",
                                     code="PACS_SELECTION_REQUIRED")
        credentials = PortalCredentials(username, password).validate()
        mrn = normalize_mrn(mrn)
        _validate_navigation_ref(mrn)
        reference = PacsLaunchRef(mrn, request_no) if request_no else None
        manager.write_config({
            "schema_version": 1, "profile": "pacs_launch", "test_mrn": mrn,
            "request_no": request_no or None, "open_viewer": open_viewer,
            "selection_mode": "EXPLICIT_REFERENCE" if reference else "DISCOVER_AND_SELECT",
            "credentials_included": False, "synthetic_loopback": args.loopback_config is not None,
        })
        manager.log("PACS launch: one examination; no automatic native retry.")
        if interactive:
            print(f"本次病歷號：{mrn}")
            print("模式：選號後交接 SmartIris。" if open_viewer else "模式：選號後只取得啟動資訊。")
            if reference is None:
                print("查詢近 4000 天醫囑，每批確認最新 30 筆；保留院方預設請求間隔。", flush=True)
        sdk = VghksSDK(settings=settings, credentials=credentials,
                       diagnostics=diagnostics, raw_capture=capture)
        summary = run_pacs_launch_test(
            sdk, reference, output_dir=manager.run_directory,
            mrn=mrn if reference is None else None,
            choose=(lambda found: _choose_order(found, open_viewer=open_viewer)) if interactive else None,
            progress=_discovery_progress if interactive else None,
            open_viewer=open_viewer, bridge_url=bridge_url,
            download_app_path=download_app_path,
            observe=_manual_observation if interactive else None, raw_capture=capture,
        )
    except KeyboardInterrupt:
        summary.update(status="INTERRUPTED", exit_code=130)
    except Exception as exc:
        summary["issue"] = to_jsonable(error_info(exc))
        capture.record_error(error=exc, step="pacs_launch.bootstrap")
    finally:
        if sdk is not None:
            with contextlib.suppress(Exception):
                sdk.close()
        summary["run_id"] = manager.run_id
        discovery_gaps = summary.get("discovery") and not summary["discovery"].get("complete", False)
        summary["bundle_status"] = ("COMPLETED_WITH_GAPS" if summary["exit_code"] == 0 and discovery_gaps else
                                    "OK" if summary["exit_code"] == 0 else
                                    "INTERRUPTED" if summary["exit_code"] == 130 else
                                    "COMPLETED_WITH_ERRORS")
        # Bundle packaging can replace run_summary with PACKAGING_FAILED. Keep
        # native side-effect and manual-observation evidence independently.
        try:
            write_json_atomic(manager.run_directory / "parsed/pacs_launch_execution.json", summary)
            capture.close()
            diagnostics.finalize(command="pacs_launch", status=summary["status"],
                                 exit_code=summary["exit_code"])
            archive = manager.finalize(status=summary["bundle_status"], summary=summary)
        except (Exception, KeyboardInterrupt):
            print(f"結果封裝未完成。已執行的本機交接狀態：{summary.get('handoff', 'NOT_RUN')}")
            print(f"請保留原始結果目錄：{manager.run_directory}")
            if summary.get("native_calls"):
                print("本次曾呼叫本機檢閱器，請先核對 SmartIris 畫面，再決定是否重新執行。")
            raise
    print(json.dumps({"status": summary["status"], "exit_code": summary["exit_code"],
                      "archive": str(archive.archive_path)}, ensure_ascii=False))
    if interactive:
        _print_result(summary, archive)
        with contextlib.suppress(EOFError, KeyboardInterrupt):
            input("\n結果已保存。按 Enter 關閉視窗…")
    return int(summary["exit_code"])


def main(argv: list[str] | None = None) -> int:
    """Keep a double-click console visible even if output setup/finalization fails."""
    try:
        return _main(argv)
    except (Exception, KeyboardInterrupt) as exc:
        interrupted = isinstance(exc, KeyboardInterrupt)
        print("測試已中斷。" if interrupted else "無法建立或完成測試結果；請確認 EXE 所在目錄可寫入。")
        if not interrupted:
            print(f"錯誤碼：{error_info(exc).code}")
        arguments = sys.argv[1:] if argv is None else argv
        if ("--interactive" in arguments or sys.stdin.isatty()) and "--non-interactive" not in arguments:
            with contextlib.suppress(EOFError, KeyboardInterrupt):
                input("按 Enter 關閉視窗…")
        return 130 if interrupted else 2


if __name__ == "__main__":
    raise SystemExit(main())
