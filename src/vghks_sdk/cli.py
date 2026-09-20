"""Command-line interface for the SDK, workflows, and closed-network tests."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import TextIO

from .contracts.check import DEFAULT_HAR_REPORT_PATH, run_har_check
from .core.config import PortalCredentials, RequestPolicy, SDKSettings
from .core.diagnostics import DiagnosticRecorder
from .core.errors import ConfigurationError, SDKError, error_code
from .core.readiness import (
    AUTH_CHECK_REGISTRY,
    DEFAULT_AUTH_REPORT_PATH,
    AuthCheckSpec,
    failed_auth_report,
    resolve_auth_targets,
)
from .diagnostics.probe import run_diagnostic_probe
from .live.console import configure_console_output
from .live.profile import LIVE_TEST_MRN
from .live_test_app import add_live_test_arguments, run_live_test_namespace
from .local_io import read_mrns, write_auth_check_report
from .models import AuthCheckReport, VisitFilter
from .sdk import VghksSDK
from .search import DoctorOpdPatientSource, MrnPatientSource, SoapSearch
from .workflows import (
    DEFAULT_ASSET_TERMS,
    export_latest_records,
    export_patient_records,
    export_visit_history,
    scan_soap,
)

_AUTO_DIAGNOSTICS = object()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vghks-sdk",
        description="Authorized internal VGHKS SDK with local-only output",
    )
    parser.add_argument("--delay-min", type=float)
    parser.add_argument("--delay-max", type=float)
    parser.add_argument("--max-attempts", type=int)
    parser.add_argument("--connect-timeout", type=float)
    parser.add_argument("--read-timeout", type=float)
    subparsers = parser.add_subparsers(dest="command", required=True)

    auth = subparsers.add_parser("auth-check", help="verify portal and application SSO")
    auth.add_argument(
        "--only",
        help=(
            "optional comma-separated subset; dependencies are added automatically "
            "(default: all readiness targets)"
        ),
    )
    auth.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_AUTH_REPORT_PATH,
        help=f"safe JSON report path (default: {DEFAULT_AUTH_REPORT_PATH})",
    )
    _add_diagnostic_arguments(auth)

    soap_parser = subparsers.add_parser(
        "scan-soap", help="screen selected patients for literal or safe-regex SOAP text"
    )
    soap_source = soap_parser.add_mutually_exclusive_group(required=True)
    soap_source.add_argument("--doctor-card")
    soap_source.add_argument("--input", type=Path)
    soap_source.add_argument("--mrn")
    soap_parser.add_argument(
        "--start", type=_iso_date, help="first doctor OPD cohort date (inclusive)"
    )
    soap_parser.add_argument(
        "--end", type=_iso_date, help="last doctor OPD cohort date (inclusive)"
    )
    soap_pattern = soap_parser.add_mutually_exclusive_group(required=True)
    soap_pattern.add_argument("--text", help="literal SOAP text to find")
    soap_pattern.add_argument("--regex", help="restricted regular expression to find")
    soap_parser.add_argument("--ignore-case", action="store_true")
    soap_parser.add_argument("--multiline", action="store_true")
    soap_parser.add_argument("--dotall", action="store_true")
    soap_parser.add_argument("--all-matches", action="store_true")
    soap_parser.add_argument("--max-patients", type=int)
    soap_parser.add_argument("--output", required=True, type=Path)
    soap_parser.add_argument("--overwrite", action="store_true")
    _add_visit_filter_arguments(soap_parser)
    _add_diagnostic_arguments(soap_parser)

    latest = subparsers.add_parser(
        "latest-records", help="save the latest filtered SOAP and numeric report for MRNs"
    )
    latest.add_argument("--input", required=True, type=Path)
    latest.add_argument("--output", required=True, type=Path)
    latest.add_argument("--max-records", type=int)
    latest.add_argument("--overwrite", action="store_true")
    _add_visit_filter_arguments(latest)
    _add_diagnostic_arguments(latest)

    history = subparsers.add_parser(
        "visit-history", help="save every filtered outpatient SOAP and numeric report"
    )
    history_input = history.add_mutually_exclusive_group(required=True)
    history_input.add_argument("--mrn")
    history_input.add_argument("--input", type=Path)
    history.add_argument("--output", required=True, type=Path)
    history.add_argument("--overwrite", action="store_true")
    _add_visit_filter_arguments(history)
    _add_diagnostic_arguments(history)

    patient_records = subparsers.add_parser(
        "patient-records",
        help="export longitudinal histories, selected visits, and optional order assets",
    )
    patient_input = patient_records.add_mutually_exclusive_group(required=True)
    patient_input.add_argument("--mrn")
    patient_input.add_argument("--input", type=Path)
    patient_records.add_argument("--output", required=True, type=Path)
    patient_records.add_argument(
        "--include",
        choices=("history", "visits", "all"),
        default="history",
    )
    patient_records.add_argument("--lookback", type=_lookback, default="all")
    patient_records.add_argument("--order-category", default="ALL")
    patient_records.add_argument("--order-status", default="*")
    patient_records.add_argument("--order-subtype", default="*")
    patient_records.add_argument("--medication-status", default="*")
    patient_records.add_argument("--numeric-department", default="*")
    patient_records.add_argument("--numeric-subtype", default="*")
    patient_records.add_argument("--visit-date", type=_iso_date)
    patient_records.add_argument("--order-date", type=_iso_date)
    patient_records.add_argument("--download-assets", action="store_true")
    patient_records.add_argument(
        "--asset-term",
        action="append",
        default=[],
        metavar="TEXT",
        help="normalized order-name search term; repeatable",
    )
    patient_records.add_argument("--all-matching-orders", action="store_true")
    patient_records.add_argument("--overwrite", action="store_true")
    _add_visit_filter_arguments(patient_records)
    _add_diagnostic_arguments(patient_records)

    diagnose = subparsers.add_parser(
        "diagnose", help="run progressive checks and save a redacted diagnostic bundle"
    )
    diagnose.add_argument("--doctor-card")
    diagnose.add_argument("--date", type=_iso_date)
    diagnose.add_argument("--mrn-input", type=Path)
    diagnose.add_argument("--skip-registration", action="store_true")
    diagnose.add_argument("--include-surgery", action="store_true")
    diagnose.add_argument("--include-unsigned", action="store_true")
    diagnose.add_argument("--start", type=_iso_date)
    diagnose.add_argument("--end", type=_iso_date)
    _add_visit_filter_arguments(diagnose)
    _add_diagnostic_arguments(diagnose, default_auto=True)

    live = subparsers.add_parser(
        "live-test",
        help=f"run an unredacted capture using fixed test MRN {LIVE_TEST_MRN}",
    )
    add_live_test_arguments(live, include_maintenance=True)

    subparsers.add_parser("list-operations", help="list atomic query Service methods offline")
    analyze = subparsers.add_parser(
        "analyze-bundle", help="verify and replay a return ZIP/directory offline"
    )
    analyze.add_argument("--input", required=True, type=Path)
    analyze.add_argument("--output", type=Path, default=Path("output/bundle-analysis"))
    analyze.add_argument("--compare", type=Path)
    replay = subparsers.add_parser(
        "replay-har", help="replay recorded HAR query responses with local parsers"
    )
    replay.add_argument("--input", type=Path, default=Path.cwd())
    replay.add_argument("--output", type=Path, default=Path("output/har-replay"))

    har = subparsers.add_parser(
        "har-check", help="validate HAR endpoint contracts without network or credentials"
    )
    har.add_argument(
        "--input",
        type=Path,
        help=(
            "one HAR file or a directory containing baseline HARs and optional "
            "data/har fixtures (default: current project)"
        ),
    )
    har.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_HAR_REPORT_PATH,
        help=f"safe JSON report path (default: {DEFAULT_HAR_REPORT_PATH})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    configure_console_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "auth-check": _handle_auth_check,
        "scan-soap": _handle_scan_soap,
        "latest-records": _handle_latest_records,
        "visit-history": _handle_visit_history,
        "patient-records": _handle_patient_records,
        "diagnose": _handle_diagnose,
        "live-test": _handle_live_test,
        "har-check": _handle_har_check,
        "analyze-bundle": _handle_analyze_bundle,
        "list-operations": _handle_list_operations,
        "replay-har": _handle_replay_har,
    }
    return handlers[args.command](args, parser)


def _handle_list_operations(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    from .live_test_app import print_query_catalog

    print_query_catalog()
    return 0


def _handle_analyze_bundle(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    from .live_test_app import analyze_bundle_command

    try:
        return analyze_bundle_command(args.input, args.output, args.compare)
    except (SDKError, ValueError, OSError) as exc:
        print(f"Analysis failed: {error_code(exc)}", file=sys.stderr)
        return 2


def _handle_replay_har(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    from .live_test_app import replay_har_command

    try:
        return replay_har_command(args.input, args.output)
    except (SDKError, ValueError, OSError) as exc:
        print(f"Replay failed: {error_code(exc)}", file=sys.stderr)
        return 2


def _handle_live_test(
    args: argparse.Namespace,
    _parser: argparse.ArgumentParser,
) -> int:
    try:
        return run_live_test_namespace(args)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except (SDKError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


def _handle_har_check(
    args: argparse.Namespace,
    _parser: argparse.ArgumentParser,
) -> int:
    try:
        report, report_path = run_har_check(
            input_path=args.input,
            output_path=args.output,
        )
    except (SDKError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(
        "HAR contracts: "
        f"status={report.status}, archives={report.archive_count}, "
        f"entries={report.entry_count}, contracts={len(report.contracts)}."
    )
    print(f"Safe report: {report_path}")
    return 0 if report.ok else 1


def _handle_auth_check(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    return _handle_online_command(args, parser)


def _handle_scan_soap(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    return _handle_online_command(args, parser)


def _handle_latest_records(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    return _handle_online_command(args, parser)


def _handle_visit_history(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    return _handle_online_command(args, parser)


def _handle_patient_records(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    return _handle_online_command(args, parser)


def _handle_diagnose(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    return _handle_online_command(args, parser)


def _handle_online_command(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    recorder: DiagnosticRecorder | None = None
    final_error: BaseException | None = None
    diagnostic_checks: tuple[dict[str, object], ...] = ()
    auth_specs: tuple[AuthCheckSpec, ...] | None = None
    auth_only: tuple[str, ...] | None = None
    auth_report_written = False
    auth_report_write_attempted = False
    exit_code = 2
    run_status = "ERROR"
    visit_filter: VisitFilter | None = None
    soap_source: DoctorOpdPatientSource | MrnPatientSource | None = None
    soap_search: SoapSearch | None = None
    try:
        _validate_cli_arguments(args)
        if args.command in {"scan-soap", "latest-records", "visit-history"}:
            visit_filter = _visit_filter_from_args(args, default_eye=False)
        elif args.command == "patient-records" and args.include in {"visits", "all"}:
            visit_filter = _patient_records_visit_filter(args)
        elif args.command == "diagnose":
            visit_filter = _visit_filter_from_args(args, default_eye=True)
        if args.command == "scan-soap":
            soap_search = _soap_search_from_args(args)
            soap_source = _soap_source_from_args(args)
        if args.command == "auth-check":
            auth_only = _parse_auth_only(args.only)
            auth_specs = resolve_auth_targets(auth_only)
        diagnostic_dir = _resolve_diagnostic_directory(args)
        if diagnostic_dir is not None:
            recorder = DiagnosticRecorder(
                diagnostic_dir,
                overwrite=bool(getattr(args, "diagnostics_overwrite", False)),
            )
        policy = RequestPolicy(
            min_delay_seconds=args.delay_min if args.delay_min is not None else 0.45,
            max_delay_seconds=args.delay_max if args.delay_max is not None else 1.10,
            max_attempts=args.max_attempts if args.max_attempts is not None else 3,
            connect_timeout_seconds=(
                args.connect_timeout if args.connect_timeout is not None else 10.0
            ),
            read_timeout_seconds=(args.read_timeout if args.read_timeout is not None else 45.0),
        ).validate()
        settings = SDKSettings.from_env(policy=policy)
        credentials = _credentials_from_env()
        with VghksSDK(
            settings=settings,
            credentials=credentials,
            diagnostics=recorder,
        ) as sdk:
            if args.command == "auth-check":
                report = sdk.auth.check(only=auth_only)
                auth_report_write_attempted = True
                report_path = write_auth_check_report(args.output, report)
                auth_report_written = True
                _print_auth_check_matrix(report, report_path)
                diagnostic_checks = tuple(
                    {
                        "check": f"auth_{target.target}",
                        "status": target.status,
                        "error_code": target.error_code,
                    }
                    for target in report.targets
                )
                exit_code = 0 if report.ok else 1
                run_status = report.status
            elif args.command == "scan-soap":
                if visit_filter is None or soap_source is None or soap_search is None:
                    raise ConfigurationError("scan-soap was not completely configured")
                result = scan_soap(
                    sdk,
                    source=soap_source,
                    search=soap_search,
                    output_dir=args.output,
                    visit_filter=visit_filter,
                    all_matches=args.all_matches,
                    max_patients=args.max_patients,
                    overwrite=args.overwrite,
                )
                print(
                    "Scan complete: "
                    f"patients={result.patient_count}, matches={result.match_count}, "
                    f"errors={result.error_count}."
                )
                print(
                    "Local outputs: "
                    f"{result.matches_path.name}, {result.index_path.name}, "
                    f"{result.status_path.name}, and {result.manifest_path.name}"
                )
                exit_code = 0 if result.error_count == 0 else 1
                run_status = "OK" if exit_code == 0 else "COMPLETED_WITH_ERRORS"
            elif args.command == "latest-records":
                if visit_filter is None:
                    raise ConfigurationError("latest-records visit filter was not configured")
                result = export_latest_records(
                    sdk,
                    input_path=args.input,
                    visit_filter=visit_filter,
                    output_dir=args.output,
                    max_records=args.max_records,
                    overwrite=args.overwrite,
                )
                print(
                    "Export complete: "
                    f"records={result.record_count}, ok={result.ok_count}, "
                    f"errors={result.error_count}."
                )
                print(f"Local outputs: {result.records_path.name} and {result.index_path.name}")
                exit_code = 0 if result.error_count == 0 else 1
                run_status = "OK" if exit_code == 0 else "COMPLETED_WITH_ERRORS"
            elif args.command == "visit-history":
                if visit_filter is None:
                    raise ConfigurationError("visit-history visit filter was not configured")
                mrns = [args.mrn] if args.mrn is not None else read_mrns(args.input)
                result = export_visit_history(
                    sdk,
                    mrns=mrns,
                    visit_filter=visit_filter,
                    output_dir=args.output,
                    overwrite=args.overwrite,
                )
                print(
                    "Visit history export complete: "
                    f"patients={result.patient_count}, cases={result.case_count}, "
                    f"ok={result.ok_count}, errors={result.error_count}."
                )
                print(f"Local outputs: {result.records_path.name} and {result.index_path.name}")
                exit_code = 0 if result.error_count == 0 else 1
                run_status = "OK" if exit_code == 0 else "COMPLETED_WITH_ERRORS"
            elif args.command == "patient-records":
                mrns = [args.mrn] if args.mrn is not None else read_mrns(args.input)
                result = export_patient_records(
                    sdk,
                    mrns=mrns,
                    output_dir=args.output,
                    include=args.include,
                    lookback=args.lookback,
                    visit_filter=visit_filter,
                    visit_date=args.visit_date,
                    order_date=args.order_date,
                    order_category=args.order_category,
                    order_status=args.order_status,
                    order_subtype=args.order_subtype,
                    medication_status=args.medication_status,
                    numeric_department=args.numeric_department,
                    numeric_subtype=args.numeric_subtype,
                    download_assets=args.download_assets,
                    asset_terms=tuple(args.asset_term) or DEFAULT_ASSET_TERMS,
                    all_matching_orders=args.all_matching_orders,
                    overwrite=args.overwrite,
                )
                print(
                    "Patient records export complete: "
                    f"patients={result.patient_count}, cases={result.case_count}, "
                    f"assets={result.asset_count}, errors={result.error_count}."
                )
                print(f"Manifest: {result.manifest_path}")
                exit_code = 0 if result.error_count == 0 else 1
                run_status = "OK" if exit_code == 0 else "COMPLETED_WITH_ERRORS"
            elif args.command == "diagnose":
                if recorder is None:
                    raise ConfigurationError("diagnose requires a diagnostic output directory")
                result = run_diagnostic_probe(
                    sdk,
                    recorder,
                    doctor_card=args.doctor_card,
                    probe_date=args.date,
                    mrn_input=args.mrn_input,
                    include_registration=not args.skip_registration,
                    include_surgery=args.include_surgery,
                    include_unsigned=args.include_unsigned,
                    range_start=args.start,
                    range_end=args.end,
                    visit_filter=visit_filter,
                )
                diagnostic_checks = tuple(dict(check.as_mapping()) for check in result.checks)
                exit_code = 0 if result.failed_count == 0 else 1
                run_status = result.status
                print(
                    "Diagnostic checks complete: "
                    f"checks={len(result.checks)}, errors={result.failed_count}."
                )
            else:
                parser.error("unknown command")
        return exit_code
    except KeyboardInterrupt as exc:
        final_error = exc
        exit_code = 130
        run_status = "INTERRUPTED"
        print("Interrupted.", file=sys.stderr)
        return exit_code
    except (SDKError, ValueError) as exc:
        final_error = exc
        exit_code = 2
        run_status = "ERROR"
        if (
            args.command == "auth-check"
            and not auth_report_written
            and not auth_report_write_attempted
        ):
            report = failed_auth_report(auth_specs or AUTH_CHECK_REGISTRY, exc)
            auth_report_write_attempted = True
            try:
                report_path = write_auth_check_report(args.output, report)
            except SDKError as report_exc:
                final_error = report_exc
                print(f"Error: {report_exc}", file=sys.stderr)
            else:
                auth_report_written = True
                _print_auth_check_matrix(report, report_path, stream=sys.stderr)
        print(f"Error: {exc}", file=sys.stderr)
        return exit_code
    except Exception as exc:
        final_error = exc
        exit_code = 3
        run_status = "UNEXPECTED_ERROR"
        if (
            args.command == "auth-check"
            and not auth_report_written
            and not auth_report_write_attempted
        ):
            report = failed_auth_report(auth_specs or AUTH_CHECK_REGISTRY, exc)
            auth_report_write_attempted = True
            try:
                report_path = write_auth_check_report(args.output, report)
            except SDKError as report_exc:
                final_error = report_exc
                print(f"Error: {report_exc}", file=sys.stderr)
            else:
                auth_report_written = True
                _print_auth_check_matrix(report, report_path, stream=sys.stderr)
        print(
            f"Unexpected error: {exc.__class__.__name__}. "
            "Enable the redacted diagnostic bundle for investigation.",
            file=sys.stderr,
        )
        return exit_code
    finally:
        if recorder is not None:
            recorder.finalize(
                command=args.command,
                status=run_status,
                exit_code=exit_code,
                error=final_error,
                checks=diagnostic_checks,
            )
            stream = sys.stdout if exit_code == 0 else sys.stderr
            print(
                f"Redacted diagnostic bundle saved: {recorder.directory.resolve()}",
                file=stream,
            )


def _credentials_from_env() -> PortalCredentials:
    username = os.getenv("VGHKS_USERNAME") or os.getenv("VGHKS_USER") or ""
    if not username.strip():
        raise ConfigurationError("set VGHKS_USERNAME before running a live command")
    password = os.getenv("VGHKS_PASSWORD")
    if not password:
        try:
            password = getpass.getpass("VGHKS password: ")
        except EOFError as exc:
            raise ConfigurationError(
                "portal password was not available from the environment or prompt"
            ) from exc
    return PortalCredentials(username=username.strip(), password=password).validate()


def _parse_auth_only(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _print_auth_check_matrix(
    report: AuthCheckReport, report_path: Path, *, stream: TextIO | None = None
) -> None:
    stream = stream or sys.stdout
    rows = [
        (
            item.target,
            item.status,
            "yes" if item.hid_present else "no",
            str(item.cookie_count),
            f"{item.duration_ms:.1f}",
            ", ".join(item.capability),
            item.landing_path or "-",
            item.error_code or "-",
        )
        for item in report.targets
    ]
    headers = (
        "Target",
        "Status",
        "HID",
        "Cookies",
        "ms",
        "Capability",
        "Landing path",
        "Error code",
    )
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    line = "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))
    print(f"Auth readiness: {report.status}", file=stream)
    print(line, file=stream)
    print("  ".join("-" * width for width in widths), file=stream)
    for row in rows:
        print(
            "  ".join(value.ljust(widths[index]) for index, value in enumerate(row)),
            file=stream,
        )
    print(f"Safe report: {report_path}", file=stream)


def _validate_cli_arguments(args: argparse.Namespace) -> None:
    if args.command == "patient-records":
        if args.all_matching_orders and not args.download_assets:
            raise ValueError("--all-matching-orders requires --download-assets")
        if args.asset_term and not args.download_assets:
            raise ValueError("--asset-term requires --download-assets")
        if args.include == "history" and args.visit_date is not None:
            raise ValueError("--visit-date requires --include visits or all")
        if args.visit_date is not None and (
            args.case_start is not None or args.case_end is not None
        ):
            raise ValueError("--visit-date cannot be combined with --case-start/--case-end")
        return
    if args.command == "scan-soap":
        start = args.start
        end = args.end
        if args.doctor_card is not None:
            if start is None or end is None:
                raise ValueError("scan-soap doctor source requires --start and --end together")
            if end < start:
                raise ValueError("scan-soap end date must not be before start date")
        elif start is not None or end is not None:
            raise ValueError("scan-soap --start and --end are available only with --doctor-card")
        if args.max_patients is not None and args.max_patients < 1:
            raise ValueError("scan-soap --max-patients must be positive")
        if args.regex is None and (args.multiline or args.dotall):
            raise ValueError("scan-soap --multiline and --dotall require --regex")
        return
    if args.command != "diagnose":
        return
    doctor_card = getattr(args, "doctor_card", None)
    probe_date = getattr(args, "date", None)
    if (doctor_card is None) != (probe_date is None):
        raise ValueError(f"{args.command} requires --doctor-card and --date together")
    start = getattr(args, "start", None)
    end = getattr(args, "end", None)
    if (start is None) != (end is None):
        raise ValueError(f"{args.command} requires --start and --end together")
    if start is not None and end is not None and end < start:
        raise ValueError(f"{args.command} end date must not be before start date")
    if (
        getattr(args, "include_surgery", False) or getattr(args, "include_unsigned", False)
    ) and doctor_card is None:
        raise ValueError(
            f"{args.command} surgery and unsigned checks require --doctor-card and --date"
        )


def _resolve_diagnostic_directory(args: argparse.Namespace) -> Path | None:
    configured = getattr(args, "diagnostics", None)
    if configured is None:
        return None
    if configured is not _AUTO_DIAGNOSTICS:
        return configured
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return Path.cwd() / "diagnostics" / f"{args.command}-{timestamp}"


def _add_diagnostic_arguments(
    parser: argparse.ArgumentParser, *, default_auto: bool = False
) -> None:
    parser.add_argument(
        "--diagnostics",
        nargs="?",
        type=Path,
        const=_AUTO_DIAGNOSTICS,
        default=_AUTO_DIAGNOSTICS if default_auto else None,
        metavar="DIRECTORY",
        help=(
            "write a redacted diagnostic bundle; omit DIRECTORY for an automatic "
            "timestamped path under ./diagnostics"
        ),
    )
    parser.add_argument(
        "--diagnostics-overwrite",
        action="store_true",
        help="replace an existing redacted diagnostic bundle",
    )


def _add_visit_filter_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--section-name",
        action="append",
        default=[],
        metavar="TEXT",
        help="include an outpatient section whose normalized name contains TEXT; repeatable",
    )
    parser.add_argument(
        "--section-code",
        action="append",
        default=[],
        metavar="CODE",
        help="include an outpatient section with this exact normalized code; repeatable",
    )
    parser.add_argument(
        "--all-sections",
        action="store_true",
        help="include every outpatient section; cannot be combined with section selectors",
    )
    parser.add_argument(
        "--case-start",
        type=_iso_date,
        help="inclusive lower bound for matching historical case dates",
    )
    parser.add_argument(
        "--case-end",
        type=_iso_date,
        help="inclusive upper bound for matching historical case dates",
    )


def _visit_filter_from_args(args: argparse.Namespace, *, default_eye: bool) -> VisitFilter:
    names = tuple(getattr(args, "section_name", ()) or ())
    codes = tuple(getattr(args, "section_code", ()) or ())
    all_sections = bool(getattr(args, "all_sections", False))
    if default_eye and not names and not codes and not all_sections:
        names = ("眼科",)
    return VisitFilter(
        section_name_contains=names,
        section_codes=codes,
        all_sections=all_sections,
        start_date=getattr(args, "case_start", None),
        end_date=getattr(args, "case_end", None),
    )


def _patient_records_visit_filter(args: argparse.Namespace) -> VisitFilter:
    names = tuple(args.section_name or ())
    codes = tuple(args.section_code or ())
    all_sections = bool(args.all_sections)
    if args.visit_date is not None and not names and not codes and not all_sections:
        all_sections = True
    return VisitFilter(
        section_name_contains=names,
        section_codes=codes,
        all_sections=all_sections,
        start_date=args.visit_date or args.case_start,
        end_date=args.visit_date or args.case_end,
    )


def _soap_search_from_args(args: argparse.Namespace) -> SoapSearch:
    if args.regex is not None:
        return SoapSearch(
            pattern=args.regex,
            mode="regex",
            ignore_case=args.ignore_case,
            multiline=args.multiline,
            dotall=args.dotall,
        )
    return SoapSearch(
        pattern=args.text,
        mode="literal",
        ignore_case=args.ignore_case,
    )


def _soap_source_from_args(
    args: argparse.Namespace,
) -> DoctorOpdPatientSource | MrnPatientSource:
    if args.doctor_card is not None:
        return DoctorOpdPatientSource(args.doctor_card, args.start, args.end)
    if args.input is not None:
        return MrnPatientSource(read_mrns(args.input))
    return MrnPatientSource((args.mrn,))


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _lookback(value: str) -> int | str:
    candidate = value.strip().casefold()
    if candidate == "all":
        return "all"
    try:
        days = int(candidate)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("lookback must be a positive day count or all") from exc
    if days < 1:
        raise argparse.ArgumentTypeError("lookback must be positive")
    return days
