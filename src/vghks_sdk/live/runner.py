"""End-to-end live-test orchestration shared by the SDK CLI and Windows EXE."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.config import EarningsCredentials, PortalCredentials
from ..core.diagnostics import DiagnosticRecorder
from ..core.errors import (
    AuthenticationError,
    ConfigurationError,
    error_code,
    error_info,
)
from ..local_io import write_json_atomic
from ..models import to_jsonable
from ..sdk import VghksSDK
from .atomic import run_atomic_test
from .bundle import LiveTestArchive, LiveTestBundleManager
from .capture import RawCaptureRecorder
from .config import LiveTestConfig
from .environment import environment_report
from .profile import (
    LIVE_TEST_MRN,
    LIVE_TEST_SCHEMA_VERSION,
    LiveTestResult,
    LiveTestStep,
    run_live_test,
)


@dataclass(frozen=True, slots=True)
class LiveTestExecution:
    status: str
    exit_code: int
    run_directory: Path
    archive: LiveTestArchive | None
    result: LiveTestResult | None
    error_code: str = ""


def package_bootstrap_failure(
    error: BaseException,
    *,
    bundle_manager: LiveTestBundleManager | None = None,
    output_dir: Path | None = None,
    output_root: Path | None = None,
    overwrite: bool = False,
    executable_directory: Path | None = None,
) -> LiveTestExecution:
    """Create a portable bundle for failures before typed config/credentials exist."""

    base = executable_directory or Path.cwd()
    root = output_root or (base / "live-test-results")
    manager = bundle_manager or _create_manager(
        output_dir=output_dir,
        output_root=root,
        overwrite=overwrite,
        archive_directory=executable_directory,
    )
    status = "INTERRUPTED" if isinstance(error, KeyboardInterrupt) else "BOOTSTRAP_FAILED"
    exit_code = 130 if status == "INTERRUPTED" else 2
    archive: LiveTestArchive | None = None
    raw_capture: RawCaptureRecorder | None = None
    diagnostics: DiagnosticRecorder | None = None
    error_id = ""
    try:
        manager.write_config(
            {
                "schema_version": 3,
                "bootstrap_incomplete": True,
                "credentials_included": False,
            }
        )
        raw_capture = RawCaptureRecorder(
            manager.run_directory,
            prepare_root=False,
            run_id=manager.run_id,
        )
        diagnostics = DiagnosticRecorder(
            manager.diagnostics_directory,
            run_id=manager.run_id,
        )
        write_json_atomic(
            manager.run_directory / "environment.json",
            environment_report(manager.run_directory),
        )
        error_id = raw_capture.record_error(error=error, step="bootstrap")
        manager.log(
            "Live-test setup failed; packaging a portable diagnostic bundle.",
            error=True,
        )
    finally:
        if raw_capture is not None:
            raw_capture.close()
        if diagnostics is not None:
            diagnostics.finalize(
                command="live-test",
                status=status,
                exit_code=exit_code,
                error=error,
                checks=(),
            )
    summary = {
        "schema_version": LIVE_TEST_SCHEMA_VERSION,
        "run_id": manager.run_id,
        "status": status,
        "exit_code": exit_code,
        "profile": "unknown",
        "test_mrn": LIVE_TEST_MRN,
        "issue": to_jsonable(error_info(error)),
        "error_id": error_id,
        "warning": (
            "UNREDACTED: a bootstrap bundle may contain credentials or session "
            "state if failure occurred after input; ZIP is not encrypted."
        ),
        "steps": [],
        "bundle": _bundle_map(),
    }
    try:
        archive = manager.finalize(status=status, summary=summary)
    except Exception as archive_error:
        status = "PACKAGING_FAILED"
        exit_code = 2
        error = archive_error
        _print_packaging_recovery(manager.run_directory)
    else:
        _print_archive(archive)
    return LiveTestExecution(
        status=status,
        exit_code=exit_code,
        run_directory=manager.run_directory,
        archive=archive,
        result=None,
        error_code=error_code(error),
    )


def execute_live_test(
    config: LiveTestConfig,
    credentials: PortalCredentials,
    *,
    earnings_credentials: EarningsCredentials | None = None,
    output_dir: Path | None = None,
    overwrite: bool = False,
    executable_directory: Path | None = None,
    bundle_manager: LiveTestBundleManager | None = None,
) -> LiveTestExecution:
    """Create the incomplete marker before SDK bootstrap and always try to package."""

    output_root = config.output_root
    if output_root is None:
        base = executable_directory or Path.cwd()
        output_root = base / "live-test-results"
    manager = bundle_manager or _create_manager(
        output_dir=output_dir,
        output_root=output_root,
        overwrite=overwrite,
        archive_directory=executable_directory,
    )
    raw_capture: RawCaptureRecorder | None = None
    diagnostics: DiagnosticRecorder | None = None
    result: LiveTestResult | None = None
    archive: LiveTestArchive | None = None
    failure: BaseException | None = None
    status = "BOOTSTRAP_FAILED"
    exit_code = 2
    failure_error_id = ""
    try:
        raw_capture = RawCaptureRecorder(
            manager.run_directory,
            prepare_root=False,
            run_id=manager.run_id,
        )
        diagnostics = DiagnosticRecorder(manager.diagnostics_directory, run_id=manager.run_id)
        config = config.with_default_doctor(credentials.username)
        manager.write_config(config.to_safe_dict())
        write_json_atomic(
            manager.run_directory / "environment.json",
            environment_report(
                manager.run_directory,
                ca_bundle=config.ca_bundle,
            ),
        )
        manager.log(f"Live test run ID: {manager.run_id}")
        manager.log(f"Profile: {config.profile}")
        manager.log(f"Atomic test MRN: {config.test_mrn}")
        if config.profile == "comprehensive" and config.weekly_opd_soap:
            manager.log(
                "Weekly OPD workflow: login card, seven days; physician labels identify owned lists, section codes are retained as metadata."
            )
        manager.log("Debug output: unencrypted JSON/HTML/binary files and ZIP; captured credentials may be included.")
        if config.profile in {"comprehensive", "ophthalmology"}:
            manager.log(
                "Unverified HTTPS fallback for login/query tests: "
                + (
                    "enabled after verified modes fail"
                    if config.allow_unverified_tls
                    else "disabled"
                )
            )
        # These can fail, but the marker, capture/error manifests, environment,
        # and redacted diagnostic trace already exist and can still be returned.
        config.validate_for_execution()
        credentials = credentials.validate()
        settings = config.build_settings()
        manager.log("Starting authenticated readiness and data checks...")
        with VghksSDK(
            settings=settings,
            credentials=credentials,
            diagnostics=diagnostics,
            raw_capture=raw_capture,
        ) as sdk:
            if config.profile in {"auth", "atomic", "comprehensive", "ophthalmology"}:
                result = run_atomic_test(
                    sdk,
                    config,
                    output_dir=manager.run_directory,
                    raw_capture=raw_capture,
                    diagnostics=diagnostics,
                    run_id=manager.run_id,
                    settings=settings,
                    login_card=credentials.username,
                    earnings_credentials=earnings_credentials,
                )
            else:
                result = run_live_test(
                    sdk,
                    test_mrn=config.test_mrn,
                    output_dir=manager.run_directory,
                    profile=config.profile,
                    doctor_card=config.doctor_card,
                    probe_date=config.opd_date,
                    include_surgery=config.include_surgery,
                    include_unsigned=config.include_unsigned,
                    range_start=config.range_start,
                    range_end=config.range_end,
                    visit_filter=config.visit_filter,
                    soap_search=config.soap_search,
                    visit_date=config.visit_date,
                    order_date=config.order_date,
                    download_assets=config.download_assets,
                    asset_terms=config.asset_terms,
                    all_matching_orders=config.all_matching_orders,
                    raw_capture=raw_capture,
                    diagnostics=diagnostics,
                    run_id=manager.run_id,
                )
        status = result.status
        exit_code = 0 if status in {"OK", "COMPLETED_WITH_GAPS"} else 1
    except KeyboardInterrupt as exc:
        failure = exc
        status = "INTERRUPTED"
        exit_code = 130
        manager.log("Live test interrupted; packaging all completed material.", error=True)
    except AuthenticationError as exc:
        failure = exc
        status = "AUTHENTICATION_FAILED"
        exit_code = 1
        manager.log("Authentication failed; packaging the diagnostic material.", error=True)
    except (ConfigurationError, ValueError) as exc:
        failure = exc
        status = "BOOTSTRAP_FAILED"
        exit_code = 2
        manager.log(f"Live test bootstrap failed: {exc}", error=True)
    except Exception as exc:  # preserve unexpected failures for offline analysis
        failure = exc
        status = "COMPLETED_WITH_ERRORS" if raw_capture is not None else "BOOTSTRAP_FAILED"
        exit_code = 1 if raw_capture is not None else 2
        manager.log(
            f"Unexpected live-test failure: {exc.__class__.__name__}: {exc}",
            error=True,
        )
    finally:
        if failure is not None and raw_capture is not None:
            failure_error_id = raw_capture.record_error(
                error=failure,
                step="live-test-runner",
            )
        if raw_capture is not None:
            raw_capture.close()
        steps = tuple(result.steps) if result is not None else ()
        if diagnostics is not None:
            diagnostics.finalize(
                command="live-test",
                status=status,
                exit_code=exit_code,
                error=failure,
                checks=(
                    {
                        "check": step.name,
                        "status": step.status,
                        "error_code": step.error_code,
                    }
                    for step in steps
                ),
            )
        summary = _summary_payload(
            manager=manager,
            config=config,
            status=status,
            exit_code=exit_code,
            steps=steps,
            failure=failure,
            failure_error_id=failure_error_id,
        )
        try:
            archive = manager.finalize(status=status, summary=summary)
        except Exception as archive_error:
            manager.close_console()
            failure = archive_error
            exit_code = 2
            status = "PACKAGING_FAILED"
            archive = None
            _print_packaging_recovery(manager.run_directory)

    if archive is not None:
        _print_archive(archive)
    return LiveTestExecution(
        status=status,
        exit_code=exit_code,
        run_directory=manager.run_directory,
        archive=archive,
        result=result,
        error_code=error_code(failure) if failure is not None else "",
    )


def _summary_payload(
    *,
    manager: LiveTestBundleManager,
    config: LiveTestConfig,
    status: str,
    exit_code: int,
    steps: tuple[LiveTestStep, ...],
    failure: BaseException | None,
    failure_error_id: str = "",
) -> dict[str, Any]:
    existing = _read_existing_summary(manager.run_directory / "run_summary.json")
    completed_steps = to_jsonable(steps)
    if not completed_steps:
        checkpoint = manager.run_directory / "step_results.json"
        if checkpoint.is_file():
            try:
                value = json.loads(checkpoint.read_text(encoding="utf-8"))
                if isinstance(value, list):
                    completed_steps = value
            except (OSError, UnicodeError, json.JSONDecodeError):
                pass
    existing.update(
        {
            "schema_version": LIVE_TEST_SCHEMA_VERSION,
            "run_id": manager.run_id,
            "status": status,
            "exit_code": exit_code,
            "profile": config.profile,
            "test_mrn": config.test_mrn,
            "warning": (
                "UNREDACTED: contains credentials, replayable session state, raw "
                "HTTP bodies, patient data, and SOAP; ZIP is not encrypted."
            ),
            "steps": completed_steps,
            "issue": to_jsonable(error_info(failure)) if failure is not None else None,
            "error_id": failure_error_id,
            "bundle": _bundle_map(),
        }
    )
    return existing


def _read_existing_summary(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _create_manager(
    *,
    output_dir: Path | None,
    output_root: Path,
    overwrite: bool,
    archive_directory: Path | None = None,
) -> LiveTestBundleManager:
    if output_dir is not None:
        return LiveTestBundleManager(
            output_dir, overwrite=overwrite, archive_directory=archive_directory
        )
    return LiveTestBundleManager.create_auto(output_root, archive_directory=archive_directory)


def create_live_test_bundle(
    *,
    output_dir: Path | None,
    output_root: Path,
    overwrite: bool,
    archive_directory: Path | None = None,
) -> LiveTestBundleManager:
    """Create the owner and incomplete markers before interactive bootstrap."""

    return _create_manager(
        output_dir=output_dir,
        output_root=output_root,
        overwrite=overwrite,
        archive_directory=archive_directory,
    )


def _bundle_map() -> dict[str, str]:
    return {
        "capture_manifest": "capture_manifest.jsonl",
        "errors": "errors.jsonl",
        "environment": "environment.json",
        "requests": "requests/",
        "responses": "responses/",
        "parsed": "parsed/",
        "diagnostics": "diagnostics/",
        "return_readme": "README_RETURN.txt",
        "step_checkpoint": "step_results.json",
        "progress": "progress.log",
        "coverage": "coverage.json",
        "results": "RESULTS.txt",
    }


def _print_archive(archive: LiveTestArchive) -> None:
    print(f"Run directory: {archive.run_directory}")
    print(f"Return ZIP: {archive.archive_path}")
    print("Bring back this ZIP only.")
    print("Debug ZIP: no encryption or password. Open RESULTS.txt for the coverage summary.")


def _print_packaging_recovery(run_directory: Path) -> None:
    print(
        f"Packaging failed. Run directory preserved: {run_directory}",
        file=sys.stderr,
    )
    print(
        "Recover without network access with: "
        f'vghks-live-test.exe --pack-incomplete "{run_directory}"',
        file=sys.stderr,
    )
