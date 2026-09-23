"""Dedicated console application for one-click VGHKS live testing."""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
import platform
import sys
import tempfile
import zipfile
from collections.abc import Sequence
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import bs4
import requests

from . import __version__
from .build_info import build_identity
from .core.config import EarningsCredentials, PortalCredentials
from .core.errors import ConfigurationError
from .core.tls import WINDOWS_SYSTEM, WindowsSystemTrustAdapter, create_requests_session
from .live.atomic import build_test_plan
from .live.bundle import (
    LiveTestBundleManager,
    find_incomplete_runs,
    pack_incomplete_run,
)
from .live.config import (
    LiveTestConfig,
    load_live_test_config,
    resolve_live_test_config,
)
from .live.console import configure_console_output
from .live.defaults import SYNTHETIC_MRN
from .live.environment import environment_report
from .live.presets import combined_round, login_test_round, visit_search_round
from .live.profile import LIVE_TEST_MRN, LIVE_TEST_SCHEMA_VERSION
from .live.runner import (
    create_live_test_bundle,
    execute_live_test,
    package_bootstrap_failure,
)
from .models import SoapRecord, VisitCase, VisitFilter
from .offline.analyze import analyze_bundle
from .offline.replay import replay_hars
from .queries import QUERY_SPECS
from .search import SoapSearch, evaluate_soap_search


def add_live_test_arguments(
    parser: argparse.ArgumentParser,
    *,
    include_policy: bool = False,
    include_maintenance: bool = False,
) -> None:
    parser.add_argument("--config", type=Path, help="credential-free live-test JSON")
    parser.add_argument("--test-mrn", help="authorized patient identifier for live queries")
    parser.add_argument(
        "--patient-national-id", help="visits profile: patient ID; omitted = read from basic info"
    )
    parser.add_argument(
        "--profile",
        choices=("login", "auth", "atomic", "comprehensive", "ophthalmology", "visits", "core", "full"),
        default=None,
        help="explicit test depth; double-click uses the profile selected at build time",
    )
    parser.add_argument(
        "--login-negative-attempts", type=int, choices=(0, 1, 2),
        help="login profile: real wrong-password attempts, after successful positive checks (default: 2)",
    )
    parser.add_argument(
        "--only",
        action="append",
        dest="only_operations",
        help="query key to test; repeatable; selects atomic profile",
    )
    parser.add_argument("--max-cases", type=int, help="visit samples (comprehensive: 6; atomic: 1)")
    parser.add_argument(
        "--max-items", type=int, help="reference/asset samples (comprehensive: 8; atomic: 2)"
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="exact run directory (default: automatic timestamped run)",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        help="root containing automatic runs/ and archives/ directories",
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--doctor-card")
    parser.add_argument("--date", type=_iso_date, dest="opd_date")
    parser.add_argument("--start", type=_iso_date, dest="range_start")
    parser.add_argument("--end", type=_iso_date, dest="range_end")
    parser.add_argument("--include-surgery", action="store_true", default=None)
    parser.add_argument("--include-unsigned", action="store_true", default=None)
    parser.add_argument(
        "--include-earnings",
        action="store_true",
        default=None,
        help="also test own MIS reports; prompts for the secondary password",
    )
    parser.add_argument("--section-name", action="append", default=None, metavar="TEXT")
    parser.add_argument("--section-code", action="append", default=None, metavar="CODE")
    parser.add_argument("--all-sections", action="store_true", default=None)
    parser.add_argument("--case-start", type=_iso_date)
    parser.add_argument("--case-end", type=_iso_date)
    parser.add_argument("--visit-date", type=_iso_date)
    parser.add_argument("--order-date", type=_iso_date)
    assets = parser.add_mutually_exclusive_group()
    assets.add_argument("--download-assets", dest="download_assets", action="store_true")
    assets.add_argument("--no-download-assets", dest="download_assets", action="store_false")
    parser.set_defaults(download_assets=None)
    parser.add_argument("--asset-term", action="append", default=None, metavar="TEXT")
    parser.add_argument("--all-matching-orders", action="store_true", default=None)
    search = parser.add_mutually_exclusive_group()
    search.add_argument("--text", help="literal text to search in already fetched SOAP")
    search.add_argument("--regex", help="restricted regex to search in fetched SOAP")
    search.add_argument("--skip-search", action="store_true")
    parser.add_argument("--ignore-case", action="store_true", default=None)
    parser.add_argument("--multiline", action="store_true", default=None)
    parser.add_argument("--dotall", action="store_true", default=None)
    parser.add_argument("--ca-bundle", type=Path)
    for field_name in (
        "portal_base_url",
        "prq_base_url",
        "sectord_base_url",
        "webmaas_base_url",
        "oppl_base_url",
        "audit_base_url",
        "mis_base_url",
        "review_base_url",
        "personnel_base_url",
    ):
        parser.add_argument("--" + field_name.replace("_", "-"), dest=field_name)
    if include_policy:
        parser.add_argument("--delay-min", type=float)
        parser.add_argument("--delay-max", type=float)
        parser.add_argument("--max-attempts", type=int)
        parser.add_argument("--connect-timeout", type=float)
        parser.add_argument("--read-timeout", type=float)
    if include_maintenance:
        maintenance = parser.add_mutually_exclusive_group()
        maintenance.add_argument(
            "--self-check",
            action="store_true",
            help="offline runtime, parser, configuration, and ZIP self-check",
        )
        maintenance.add_argument(
            "--pack-incomplete",
            type=Path,
            metavar="DIRECTORY",
            help="package a prior interrupted run without network access",
        )
        maintenance.add_argument(
            "--list-operations",
            action="store_true",
            help="list query units without network or credentials",
        )
        maintenance.add_argument(
            "--plan", action="store_true", help="preview auth/atomic/comprehensive checks offline"
        )
        maintenance.add_argument(
            "--analyze-bundle",
            type=Path,
            metavar="ZIP_OR_DIRECTORY",
            help="verify and replay a return bundle offline",
        )
        maintenance.add_argument(
            "--replay-har",
            type=Path,
            help="replay HAR query responses through local parsers offline",
        )
        parser.add_argument("--analysis-output", type=Path, default=Path("output/bundle-analysis"))
        parser.add_argument(
            "--compare-bundle",
            type=Path,
            help="previous bundle for comparison during offline analysis",
        )
        parser.add_argument("--no-pause", action="store_true", help=argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vghks-live-test",
        description="One-click unredacted validation bundle for an authorized test patient",
    )
    add_live_test_arguments(parser, include_policy=True, include_maintenance=True)
    return parser


def run_live_test_namespace(
    args: argparse.Namespace,
    *,
    executable_mode: bool = False,
    force_interactive: bool = False,
) -> int:
    """Resolve config, optionally run the wizard, then execute and package."""

    executable_directory = Path(sys.executable).resolve().parent if executable_mode else Path.cwd()
    if getattr(args, "list_operations", False):
        print_query_catalog()
        return 0
    if getattr(args, "replay_har", None) is not None:
        return replay_har_command(args.replay_har, args.analysis_output)
    if getattr(args, "analyze_bundle", None) is not None:
        return analyze_bundle_command(
            args.analyze_bundle, args.analysis_output, getattr(args, "compare_bundle", None)
        )
    if getattr(args, "compare_bundle", None) is not None:
        raise ConfigurationError("--compare-bundle requires --analyze-bundle")
    if getattr(args, "pack_incomplete", None) is not None:
        archive = pack_incomplete_run(args.pack_incomplete, archive_directory=executable_directory)
        print(f"Return ZIP: {archive.archive_path}")
        return 0
    if getattr(args, "self_check", False):
        return run_self_check(config_path=getattr(args, "config", None))
    if getattr(args, "plan", False):
        config = resolve_live_test_config(
            cli_values=_namespace_cli_values(args),
            json_values=_configuration_values(args),
        )
        if config.profile not in {"login", "auth", "atomic", "comprehensive", "ophthalmology", "visits"}:
            raise ConfigurationError(
                "--plan requires login, auth, atomic, comprehensive, ophthalmology or visits profile"
            )
        print(json.dumps(build_test_plan(config), ensure_ascii=True, indent=2))
        return 0

    default_root = executable_directory / "live-test-results"
    manager: LiveTestBundleManager | None = None
    try:
        json_values = _configuration_values(args)
        cli_values = _namespace_cli_values(args)
        if (
            force_interactive
            and getattr(args, "config", None)
            and not json_values.get("profile")
            and not cli_values.get("profile")
            and not os.getenv("VGHKS_LIVE_PROFILE")
        ):
            cli_values["profile"] = "ophthalmology"
        config = resolve_live_test_config(
            cli_values=cli_values,
            json_values=json_values,
            default_output_root=default_root,
        )
        interactive = not bool(getattr(args, "non_interactive", False)) and (
            force_interactive or sys.stdin.isatty()
        )
        plan = build_test_plan(config) if config.profile not in {"core", "full"} else None
        patient_required = plan is None or any(
            row["scope"] in {"patient", "history", "text_history"} for row in plan["operations"]
        )
        supplied_mrn = (
            getattr(args, "test_mrn", None)
            or json_values.get("test_mrn")
            or os.getenv("VGHKS_TEST_MRN")
        )
        if (
            patient_required
            and config.test_mrn == SYNTHETIC_MRN
            and not supplied_mrn
            and not (interactive and config.profile == "visits")
        ):
            if interactive:
                config = replace(
                    config, test_mrn=_prompt_required("Authorized test MRN / 病歷號", "")
                )
            else:
                raise ConfigurationError(
                    "provide --test-mrn or VGHKS_TEST_MRN for patient queries",
                    code="TEST_MRN_REQUIRED",
                )
        if interactive:
            _offer_incomplete_packaging(
                config.output_root or default_root, archive_directory=executable_directory
            )
        manager = create_live_test_bundle(
            output_dir=getattr(args, "output", None),
            output_root=config.output_root or default_root,
            overwrite=bool(getattr(args, "overwrite", False)),
            archive_directory=executable_directory,
        )
        if interactive:
            credentials = _interactive_credentials()
            config = config.with_default_doctor(credentials.username)
            config = _interactive_wizard(config, quick=force_interactive)
        else:
            credentials = _noninteractive_credentials()

        patient_national_id = None
        if config.profile == "visits":
            patient_national_id = getattr(args, "patient_national_id", None) or os.getenv(
                "VGHKS_PATIENT_NATIONAL_ID"
            )
            if interactive and not patient_national_id:
                patient_national_id = (
                    input("病人身分證字號 (非登入帳號; 直接 Enter 從上述病歷號自動取得): ").strip()
                    or None
                )

        earnings_credentials = None
        if config.include_earnings:
            if interactive:
                print("\n業績與薪水測試: 請輸入身分證字號與薪資系統密碼。")
                print("同一組資料會用於醫療業績、專勤工作獎金兩份報表; 密碼輸入不顯示。")
            earnings_id = os.getenv("VGHKS_EARNINGS_NATIONAL_ID")
            earnings_password = os.getenv("VGHKS_EARNINGS_PASSWORD")
            if interactive and not earnings_id:
                earnings_id = input("MIS national ID / 身分證字號 (Enter to skip): ").strip()
            if interactive and earnings_id and not earnings_password:
                earnings_password = getpass.getpass("薪資系統密碼 / MIS password (Enter to skip): ")
            if earnings_id and earnings_password:
                earnings_credentials = EarningsCredentials(earnings_id, earnings_password)
            elif interactive:
                print("未提供完整薪資登入資料: 本次兩份報表標為未測, 其餘項目繼續。")
        execution = execute_live_test(
            config,
            credentials,
            executable_directory=executable_directory,
            bundle_manager=manager,
            earnings_credentials=earnings_credentials,
            patient_national_id=patient_national_id,
        )
        return execution.exit_code
    except BaseException as exc:
        if isinstance(exc, SystemExit):
            raise
        execution = package_bootstrap_failure(
            exc,
            bundle_manager=manager,
            output_dir=getattr(args, "output", None),
            output_root=getattr(args, "output_root", None) or default_root,
            overwrite=bool(getattr(args, "overwrite", False)),
            executable_directory=executable_directory,
        )
        return execution.exit_code


def _configuration_values(args: argparse.Namespace) -> dict[str, Any]:
    if getattr(args, "config", None) is not None:
        return load_live_test_config(args.config)
    if getattr(args, "bundled_visits", False):
        return visit_search_round()
    if getattr(args, "bundled_login", False):
        return login_test_round()
    return combined_round() if getattr(args, "bundled_round", False) else {}


def main(argv: Sequence[str] | None = None) -> int:
    configure_console_output()
    supplied = list(argv) if argv is not None else sys.argv[1:]
    zero_argument_start = not supplied
    parser = build_parser()
    args = parser.parse_args(supplied)
    if zero_argument_start or (
        args.plan and not args.config and not args.profile and not args.only_operations
    ):
        # A portable double-click uses the profile selected at build time.
        # Old sidecars / profile environment settings cannot change that scope.
        # An explicit --config or --profile remains available for development.
        args.profile = build_identity().get("default_profile", "comprehensive")
        args.bundled_visits = args.profile == "visits"
        args.bundled_login = args.profile == "login"
        args.bundled_round = args.profile == "comprehensive"
    exit_code = 2
    try:
        exit_code = run_live_test_namespace(
            args,
            executable_mode=bool(getattr(sys, "frozen", False)),
            force_interactive=zero_argument_start,
        )
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        exit_code = 130
    except (ConfigurationError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        exit_code = 2
    finally:
        if zero_argument_start and not getattr(args, "no_pause", False):
            with contextlib.suppress(EOFError, KeyboardInterrupt):
                input("Press Enter to close...")
    return exit_code


def run_self_check(*, config_path: Path | None = None) -> int:
    """Exercise imports, safe regex, filesystem hashing, and ZIP without network."""

    checks: list[tuple[str, bool, str]] = []
    checks.append(("Python x64", platform.architecture()[0] == "64bit", platform.python_version()))
    version_parts = __version__.split(".")
    checks.append(
        (
            "SDK version",
            len(version_parts) == 3 and all(part.isdigit() for part in version_parts),
            __version__,
        )
    )
    checks.append(("requests", bool(requests.__version__), requests.__version__))
    checks.append(("BeautifulSoup", bool(bs4.__version__), bs4.__version__))
    try:
        search = SoapSearch(r"APPLY\s+OD", mode="regex", ignore_case=True)
        case = VisitCase(LIVE_TEST_MRN, date(2026, 1, 1), "O", "SELF", "70", "眼科")
        evaluation = evaluate_soap_search(
            SoapRecord(case, ("S: self-check", "P: apply OD")), search
        )
        checks.append(("safe SOAP regex", evaluation.status == "MATCHED", evaluation.status))
    except Exception as exc:
        checks.append(("safe SOAP regex", False, exc.__class__.__name__))
    resolved_config = LiveTestConfig()
    if config_path is not None:
        try:
            resolved_config = resolve_live_test_config(
                json_values=load_live_test_config(config_path)
            )
            resolved_config.build_settings()
            checks.append(("configuration structure", True, "OK"))
        except Exception as exc:
            checks.append(("configuration structure", False, exc.__class__.__name__))
    else:
        checks.append(("configuration structure", True, "default"))
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = LiveTestBundleManager(root / "run", run_id="self-check")
            environment = environment_report(
                manager.run_directory,
                ca_bundle=resolved_config.ca_bundle,
            )
            manager.write_config(resolved_config.to_safe_dict())
            checks.append(
                (
                    "output directory writable",
                    bool(environment["output"]["writable"]),
                    str(environment["output"]["writable"]),
                )
            )
            try:
                tls_session, trust_mode = create_requests_session(
                    ca_bundle=(
                        str(resolved_config.ca_bundle) if resolved_config.ca_bundle else None
                    )
                )
                try:
                    tls_adapter = tls_session.get_adapter("https://self-check.invalid")
                    adapter_ok = trust_mode != WINDOWS_SYSTEM or isinstance(
                        tls_adapter, WindowsSystemTrustAdapter
                    )
                finally:
                    tls_session.close()
                tls_ok = environment["ca_mode"] != "INVALID" and adapter_ok
            except Exception as exc:
                trust_mode = exc.__class__.__name__
                tls_ok = False
            checks.append(("TLS trust store", tls_ok, str(trust_mode)))
            console_encodings = environment["console"]
            console_ok = bool(
                console_encodings["stdout_encoding"] and console_encodings["stderr_encoding"]
            )
            checks.append(
                (
                    "console encoding",
                    console_ok,
                    str(console_encodings["stdout_encoding"]),
                )
            )
            archive = manager.finalize(
                status="OK",
                summary={
                    "schema_version": LIVE_TEST_SCHEMA_VERSION,
                    "run_id": "self-check",
                    "status": "OK",
                },
            )
            with zipfile.ZipFile(archive.archive_path) as bundle:
                roundtrip_ok = bundle.testzip() is None and "run_config.json" in bundle.namelist()
            archive_ok = archive.archive_path.is_file() and roundtrip_ok
            checks.append(("bundle ZIP roundtrip", archive_ok, "single ZIP"))
    except Exception as exc:
        checks.append(("bundle ZIP roundtrip", False, exc.__class__.__name__))

    print("VGHKS live-test offline self-check (no network requests)")
    for name, passed, detail in checks:
        print(f"{'PASS' if passed else 'FAIL':4}  {name}: {detail}")
    return 0 if all(item[1] for item in checks) else 1


def _namespace_cli_values(args: argparse.Namespace) -> dict[str, Any]:
    if getattr(args, "overwrite", False) and getattr(args, "output", None) is None:
        raise ConfigurationError("--overwrite requires an explicit --output directory")
    if getattr(args, "output", None) is not None and getattr(args, "output_root", None) is not None:
        raise ConfigurationError("--output and --output-root cannot be combined")
    if getattr(args, "all_sections", False) and (
        getattr(args, "section_name", None) or getattr(args, "section_code", None)
    ):
        raise ConfigurationError("--all-sections cannot be combined with section names or codes")
    if getattr(args, "text", None) is not None and (
        getattr(args, "multiline", False) or getattr(args, "dotall", False)
    ):
        raise ConfigurationError("--multiline and --dotall require --regex")
    if getattr(args, "skip_search", False) and any(
        bool(getattr(args, key, False)) for key in ("ignore_case", "multiline", "dotall")
    ):
        raise ConfigurationError("SOAP search flags cannot be combined with --skip-search")
    values: dict[str, Any] = {}
    for key in (
        "test_mrn",
        "profile",
        "login_negative_attempts",
        "output_root",
        "doctor_card",
        "opd_date",
        "range_start",
        "range_end",
        "ca_bundle",
        "include_surgery",
        "include_unsigned",
        "include_earnings",
        "visit_date",
        "order_date",
        "download_assets",
        "all_matching_orders",
        "only_operations",
        "max_cases",
        "max_items",
    ):
        value = getattr(args, key, None)
        if value is not None:
            values[key] = value
    if getattr(args, "only_operations", None):
        if getattr(args, "profile", None) not in {None, "atomic", "comprehensive"}:
            raise ConfigurationError("--only requires atomic or comprehensive profile")
        values["profile"] = getattr(args, "profile", None) or "atomic"
    asset_terms = getattr(args, "asset_term", None)
    if asset_terms is not None:
        values["asset_terms"] = list(asset_terms)

    names = getattr(args, "section_name", None)
    codes = getattr(args, "section_code", None)
    all_sections = getattr(args, "all_sections", None)
    case_start = getattr(args, "case_start", None)
    case_end = getattr(args, "case_end", None)
    if any(value is not None for value in (names, codes, all_sections, case_start, case_end)):
        visit: dict[str, Any] = {}
        if all_sections:
            visit.update({"all_sections": True, "section_name_contains": [], "section_codes": []})
        elif names is not None or codes is not None:
            visit.update(
                {
                    "all_sections": False,
                    "section_name_contains": list(names or ()),
                    "section_codes": list(codes or ()),
                }
            )
        if case_start is not None:
            visit["start_date"] = case_start
        if case_end is not None:
            visit["end_date"] = case_end
        values["visit_filter"] = visit

    if getattr(args, "skip_search", False):
        values["soap_search"] = None
    elif getattr(args, "text", None) is not None:
        values["soap_search"] = {
            "pattern": args.text,
            "mode": "literal",
            "ignore_case": bool(getattr(args, "ignore_case", False)),
        }
    elif getattr(args, "regex", None) is not None:
        values["soap_search"] = {
            "pattern": args.regex,
            "mode": "regex",
            "ignore_case": bool(getattr(args, "ignore_case", False)),
            "multiline": bool(getattr(args, "multiline", False)),
            "dotall": bool(getattr(args, "dotall", False)),
        }
    elif any(bool(getattr(args, key, False)) for key in ("ignore_case", "multiline", "dotall")):
        # A partial object can override a search supplied by JSON. If no lower
        # precedence search exists, typed config construction rejects it.
        values["soap_search"] = {
            key: bool(getattr(args, key, False))
            for key in ("ignore_case", "multiline", "dotall")
            if getattr(args, key, None) is not None
        }

    policy_names = {
        "delay_min": "min_delay_seconds",
        "delay_max": "max_delay_seconds",
        "max_attempts": "max_attempts",
        "connect_timeout": "connect_timeout_seconds",
        "read_timeout": "read_timeout_seconds",
    }
    policy: dict[str, Any] = {}
    for argument, field_name in policy_names.items():
        value = getattr(args, argument, None)
        if value is not None:
            policy[field_name] = value
    if policy:
        values["request_policy"] = policy

    endpoints: dict[str, str] = {}
    for field_name in (
        "portal_base_url",
        "prq_base_url",
        "sectord_base_url",
        "webmaas_base_url",
        "oppl_base_url",
        "audit_base_url",
        "mis_base_url",
        "review_base_url",
        "personnel_base_url",
    ):
        value = getattr(args, field_name, None)
        if value is not None:
            endpoints[field_name] = value
    if endpoints:
        values["endpoint_overrides"] = endpoints
    return values


def _interactive_wizard(config: LiveTestConfig, *, quick: bool = False) -> LiveTestConfig:
    if config.profile == "login":
        print("\n登入專項測試: 正常登入、子系統 SSO、Session 恢復及本人人事查詢。")
        print(f"最後最多提交 {config.login_negative_attempts} 次刻意產生的錯誤密碼; 首次正常登入失敗則略過。")
        print("其餘錯誤情境使用內建離線模擬; 不需病歷號、身分證或薪資密碼。")
        print("結果 ZIP 不加密, 直接存於 EXE 同目錄, 帶回該 ZIP 即可。")
        return config
    if config.profile == "visits":
        print("\n就診搜尋增量測試: 病歷號/身分證比對、到院日/類別/科別/醫師篩選。")
        print("最多抽樣三筆門診驗證 SOAP/醫囑串接; 各項獨立記錄錯誤並繼續。")
        print("不測審查、手術、薪資或附件; 結果 ZIP 不加密, 存於 EXE 同目錄。")
        mrn = _prompt_required("測試病歷號 (Enter 沿用)", config.test_mrn)
        return replace(config, test_mrn=mrn)
    if config.profile == "ophthalmology":
        print("\nOphthalmology orders: DBR / Microsonography, case and historical order lists.")
        print(
            "Text, PDF and JPG branches run independently; explicit no-data results are retained."
        )
        print(
            "Unexecuted orders are recorded and skipped; each exam type has its own sample budget."
        )
        print(
            f"Request delay: {config.request_policy.min_delay_seconds}..{config.request_policy.max_delay_seconds}s, sequential."
        )
    if config.profile == "comprehensive":
        print(
            "\nFirst intranet run: comprehensive tests; independent failures will be recorded and testing continues."
        )
        print("Debug output: unencrypted files and ZIP; captured credentials may be included.")
        if quick:
            print("本輪參數已內建: 審查案件、依手術碼查手術紀錄、病人歷史手術紀錄與既有查詢。")
            if config.surgery_query.get("procedure_code"):
                print(
                    "手術碼: "
                    + config.surgery_query["procedure_code"]
                    + "; 區間: "
                    + ", ".join(config.surgery_query.get("periods", ()))
                )
        if config.weekly_opd_soap:
            print("Weekly OPD: login card, today and previous 6 days (or configured end date).")
            print(
                "Own list: returned physician matches login card (optional F suffix); blank physician: shared."
            )
            print("SOAP search: arrange CATA (ignore case). Atomic sample limits do not apply.")
        if config.doctor_card:
            print(
                f"Doctor queries: OPD {config.opd_date}; surgery/unsigned {config.range_start}..{config.range_end}"
            )
    if config.profile in {"auth", "atomic", "comprehensive", "ophthalmology"}:
        plan = build_test_plan(config)
        print(
            f"Profile: {config.profile}; queries: {len(plan['operations'])}; max cases: {config.max_cases}; max items: {config.max_items}"
        )
        print("Operations: " + ", ".join(row["key"] for row in plan["operations"]))
        return config
    print(f"\nVGHKS Live Test wizard — authorized MRN {config.test_mrn}")
    print("core = latest matching visit; full = visits + four longitudinal histories + assets")
    if quick:
        print(
            "Quick full mode: full profile, default ophthalmology filter, no SOAP keyword prompt."
        )
        profile = config.profile
        visit_filter = config.visit_filter
        search = config.soap_search
    else:
        profile = _prompt_choice("Profile", ("core", "full"), config.profile)
        visit_filter = _prompt_visit_filter(config.visit_filter)
        search = _prompt_search()
    doctor_card = config.doctor_card
    opd_date = config.opd_date
    range_start = config.range_start
    range_end = config.range_end
    if profile == "full" and doctor_card is not None:
        opd_date = opd_date or _prompt_date("OPD date", opd_date)
        default_start = range_start or opd_date
        default_end = range_end or opd_date
        range_start = _prompt_date("Range start", default_start)
        range_end = _prompt_date("Range end", default_end)
    resolved = replace(
        config,
        profile=profile,
        visit_filter=visit_filter,
        soap_search=search,
        doctor_card=doctor_card,
        opd_date=opd_date,
        range_start=range_start,
        range_end=range_end,
    )
    print("\nEffective test:")
    print(f"  Profile: {resolved.profile}")
    print(f"  Visit filter: {_visit_filter_label(resolved.visit_filter)}")
    print(f"  SOAP search: {_search_label(resolved.soap_search)}")
    print(
        "  Assets: " + (", ".join(resolved.asset_terms) if resolved.download_assets else "skipped")
    )
    print(f"  Output root: {resolved.output_root}")
    return resolved


def print_query_catalog() -> None:
    for spec in QUERY_SPECS:
        print(f"{spec.key:30} sdk.{spec.sdk_method}({', '.join(spec.inputs)})")


def analyze_bundle_command(source: Path, output: Path, previous: Path | None = None) -> int:
    report = analyze_bundle(source, output_dir=output, compare_path=previous)
    print(f"Bundle opened; recorded run: {report['bundle_status']}")
    print(f"Analysis status: {report['analysis_status']}")
    root = report["root_cause"]
    if root:
        print(f"Root cause: {root['phase']} / {root['code']}")
    print(f"Analysis: {output.resolve() / 'analysis.md'}")
    print(f"Retest config: {output.resolve() / 'retest-config.json'}")
    return 0 if report["analysis_status"] in {"OK", "COMPLETED_WITH_GAPS"} else 1


def replay_har_command(source: Path, output: Path) -> int:
    report = replay_hars(source, output_path=output / "har-replay.json")
    print(f"HAR replay: {report['status']}; {report['status_counts']}")
    print(f"Report: {output.resolve() / 'har-replay.json'}")
    return 0 if report["status"] == "OK" else 1


def _prompt_visit_filter(current: VisitFilter) -> VisitFilter:
    default_mode = "all" if current.all_sections else "code" if current.section_codes else "name"
    mode = _prompt_choice("Section filter", ("name", "code", "all"), default_mode)
    if mode == "all":
        names: tuple[str, ...] = ()
        codes: tuple[str, ...] = ()
        all_sections = True
    elif mode == "code":
        default = ",".join(current.section_codes)
        raw = _prompt_required("Section code(s), comma separated", default)
        names, codes, all_sections = (), tuple(_split_values(raw)), False
    else:
        default = ",".join(current.section_name_contains) or "眼科"
        raw = _prompt_required("Section name text(s), comma separated", default)
        names, codes, all_sections = tuple(_split_values(raw)), (), False
    start = current.start_date
    end = current.end_date
    raw_range = input(
        "Optional case date range YYYY-MM-DD,YYYY-MM-DD "
        f"[current: {_date_range_label(start, end)}; Enter keeps]: "
    ).strip()
    if raw_range:
        parts = _split_values(raw_range)
        if len(parts) != 2:
            raise ConfigurationError("case date range requires two ISO dates")
        start, end = _iso_date(parts[0]), _iso_date(parts[1])
    return VisitFilter(
        section_name_contains=names,
        section_codes=codes,
        all_sections=all_sections,
        start_date=start,
        end_date=end,
    )


def _prompt_search() -> SoapSearch | None:
    while True:
        raw = input("SOAP search: enter literal text, prefix regex:, or press Enter to skip: ")
        if raw == "":
            return None
        try:
            if raw.startswith("regex:"):
                pattern = raw.removeprefix("regex:")
                ignore = _prompt_yes_no("Ignore case", False)
                multiline = _prompt_yes_no("Regex multiline", False)
                dotall = _prompt_yes_no("Regex dotall", False)
                return SoapSearch(pattern, "regex", ignore, multiline, dotall)
            ignore = _prompt_yes_no("Ignore case", False)
            return SoapSearch(raw, "literal", ignore)
        except ConfigurationError as exc:
            print(f"Invalid search: {exc}")


def _interactive_credentials() -> PortalCredentials:
    default_user = os.getenv("VGHKS_USERNAME") or os.getenv("VGHKS_USER") or ""
    if default_user:
        username = default_user
        print("Portal username: using environment setting")
    else:
        username = _prompt_required("Portal username", "")
    password = os.getenv("VGHKS_PASSWORD") or getpass.getpass("Portal password: ")
    return PortalCredentials(username, password).validate()


def _noninteractive_credentials() -> PortalCredentials:
    username = os.getenv("VGHKS_USERNAME") or os.getenv("VGHKS_USER") or ""
    password = os.getenv("VGHKS_PASSWORD") or ""
    if not username or not password:
        raise ConfigurationError(
            "non-interactive live-test requires VGHKS_USERNAME and VGHKS_PASSWORD"
        )
    return PortalCredentials(username, password).validate()


def _offer_incomplete_packaging(
    output_root: Path, *, archive_directory: Path | None = None
) -> None:
    incomplete = find_incomplete_runs(output_root)
    if not incomplete:
        return
    print(f"Found {len(incomplete)} incomplete live-test run(s).")
    if not _prompt_yes_no("Package them now without network access", False):
        return
    for directory in incomplete:
        archive = pack_incomplete_run(directory, archive_directory=archive_directory)
        print(f"Recovered ZIP: {archive.archive_path}")


def _prompt_choice(label: str, choices: tuple[str, ...], default: str) -> str:
    while True:
        raw = input(f"{label} [{' / '.join(choices)}] [{default}]: ").strip().lower()
        value = raw or default
        if value in choices:
            return value
        print("Choose one of: " + ", ".join(choices))


def _prompt_required(label: str, default: str) -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        value = input(f"{label}{suffix}: ").strip() or default
        if value:
            return value
        print(f"{label} is required.")


def _prompt_date(label: str, default: date | None) -> date:
    while True:
        suffix = f" [{default.isoformat()}]" if default else ""
        raw = input(f"{label}{suffix}: ").strip()
        if not raw and default is not None:
            return default
        try:
            return _iso_date(raw)
        except argparse.ArgumentTypeError:
            print("Enter an ISO date such as 2026-07-01.")


def _prompt_yes_no(label: str, default: bool) -> bool:
    marker = "Y/n" if default else "y/N"
    raw = input(f"{label} [{marker}]: ").strip().casefold()
    if not raw:
        return default
    return raw in {"y", "yes"}


def _split_values(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _visit_filter_label(value: VisitFilter) -> str:
    if value.all_sections:
        section = "all outpatient sections"
    else:
        section = "names=" + ",".join(value.section_name_contains)
        if value.section_codes:
            section += " codes=" + ",".join(value.section_codes)
    return section + " dates=" + _date_range_label(value.start_date, value.end_date)


def _search_label(value: SoapSearch | None) -> str:
    if value is None:
        return "skipped"
    return f"{value.mode} ({'ignore-case' if value.ignore_case else 'case-sensitive'})"


def _date_range_label(start: date | None, end: date | None) -> str:
    return f"{start}..{end}" if start and end else "unbounded"


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected ISO date YYYY-MM-DD") from exc


if __name__ == "__main__":
    raise SystemExit(main())
