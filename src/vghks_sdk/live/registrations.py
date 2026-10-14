"""Sequential registration-only integration checks for an explicit patient list."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from ..core.errors import AuthenticationError, ConfigurationError, ErrorInfo
from ..core.operations import OPERATIONS
from ..local_io import write_json_atomic, write_jsonl_line
from ..models import to_jsonable
from ..parsing.webmaas import registration_counts
from ..queries import query_spec
from .atomic import _write_coverage
from .preflight import independent_readiness, run_network_checks
from .profile import (
    LIVE_TEST_SCHEMA_VERSION,
    LiveTestResult,
    LiveTestStep,
    _overall_status,
    _run_step,
    _target_ready,
)

KEY = "webmaas.registration_query"
TARGETS = ("portal", "sectord", "webmaas")


def build_registration_plan(config: Any) -> dict[str, Any]:
    spec = query_spec(KEY)
    return {
        "schema_version": 1, "profile": "registrations",
        "patient_count": len(config.patient_mrns), "patient_input_required": not config.patient_mrns,
        "selection": "all supplied distinct MRNs in input order; no sampling",
        "execution": "sequential_shared_session", "auth_targets": list(TARGETS),
        "network_checks": ["dns", "tcp", "https"], "negative_password_post_limit": 0,
        "download_assets": False, "no_show": "True for recorded icon; None means unknown",
        "registered_by": "raw code; no source classification",
        "excluded_write_operations": [spec.key for spec in OPERATIONS if spec.mutates],
        "continue_patient_errors": True, "stop_on_authentication_error": True,
        "operations": [{"key": KEY, "sdk_method": spec.sdk_method, "scope": spec.scope,
                        "inputs": list(spec.inputs), "dependencies": list(spec.dependencies),
                        "requested": True}],
    }


def run_registration_test(
    sdk: Any, config: Any, *, output_dir: Path, settings: Any = None,
    raw_capture: Any = None, diagnostics: Any = None, run_id: str = "",
) -> LiveTestResult:
    if not config.patient_mrns:
        raise ConfigurationError("patient list is empty", code="BATCH_MRNS_REQUIRED")
    root = output_dir.resolve()
    parsed = root / "parsed/registrations"
    parsed.mkdir(parents=True, exist_ok=True)
    plan = build_registration_plan(config)
    write_json_atomic(root / "test_plan.json", plan)
    write_json_atomic(parsed / "patients.json", [
        {"index": index, "mrn": mrn, "output": f"patients/{index:06d}.json"}
        for index, mrn in enumerate(config.patient_mrns, 1)
    ])
    steps: list[LiveTestStep] = []
    common = {"root": root, "raw_capture": raw_capture, "diagnostics": diagnostics}
    totals: Counter = Counter()
    codes: Counter = Counter()
    summary_path = parsed / "batch_summary.json"

    def checkpoint(status: str) -> None:
        write_json_atomic(summary_path, {
            "schema_version": 1, "status": status, "requested_patient_count": len(config.patient_mrns),
            "completed_patient_count": totals["OK"] + totals["EMPTY"] + totals["ERROR"],
            "blocked_patient_count": totals["BLOCKED"],
            "pending_patient_count": len(config.patient_mrns) - totals["patients"],
            "patient_status_counts": {key: totals[key] for key in ("OK", "EMPTY", "ERROR", "BLOCKED")},
            "record_count": totals["records"], "no_show_count": totals["no_show"],
            "attendance_unknown_count": totals["unknown"], "registered_by_codes": dict(codes),
            "patients": "patients.json", "patient_results": "patient_results.jsonl",
        })

    checkpoint("RUNNING")
    fatal = False
    try:
        if settings is not None:
            run_network_checks(
                settings, steps, only=TARGETS, allow_unverified_tls=config.allow_unverified_tls,
                configure_connection=getattr(sdk, "configure_connection", None), **common,
            )
        readiness = independent_readiness(sdk, steps, only=TARGETS, **common)
        fatal = not _target_ready(readiness, "portal")
        ready = all(_target_ready(readiness, key) for key in TARGETS)
        with (parsed / "patient_results.jsonl").open("w", encoding="utf-8") as journal:
            for index, mrn in enumerate(config.patient_mrns, 1):
                name = f"registrations.patient_{index:06d}"
                if fatal or not ready:
                    steps.append(LiveTestStep(
                        name, "BLOCKED", operation=KEY,
                        issue=ErrorInfo("READINESS_FAILED" if not ready else "AUTHENTICATION_FAILED",
                                        "DEPENDENCY"),
                    ))
                else:
                    print(f"Patient {index}/{len(config.patient_mrns)}", flush=True)
                    rows, error = _run_step(
                        steps, name=name, operation_key=KEY,
                        operation=lambda mrn=mrn: sdk.patients.get_registration_history(mrn),
                        output_path=parsed / "patients" / f"{index:06d}.json",
                        classify=lambda rows: "OK" if rows else "EMPTY",
                        summarize=lambda rows: {key: value for key, value in registration_counts(rows).items()
                                                if key != "registered_by_codes"}, **common,
                    )
                    fatal = isinstance(error, AuthenticationError)
                    if error is None and rows is not None:
                        counts = registration_counts(rows)
                        totals.update(records=counts["record_count"], no_show=counts["no_show_count"],
                                      unknown=counts["attendance_unknown_count"])
                        codes.update(counts["registered_by_codes"])
                step = steps[-1]
                totals.update({step.status: 1, "patients": 1})
                write_jsonl_line(journal, {"index": index, "mrn": mrn, "step": to_jsonable(step)})
                checkpoint("RUNNING")
    except KeyboardInterrupt:
        checkpoint("INTERRUPTED")
        raise
    if totals["OK"] or totals["EMPTY"]:
        steps.append(LiveTestStep(
            "registrations.coverage.no_show", "OK" if totals["no_show"] else "NO_SAMPLE",
            output=summary_path.relative_to(root).as_posix(),
            details={"no_show_count": totals["no_show"], "attendance_unknown_count": totals["unknown"]},
        ))
    write_json_atomic(root / "step_results.json", to_jsonable(steps))
    status = _overall_status(steps, fatal_auth=fatal)
    checkpoint(status)
    _write_coverage(root, plan, steps, status)
    write_json_atomic(root / "run_summary.json", {
        "schema_version": LIVE_TEST_SCHEMA_VERSION, "run_id": run_id, "profile": "registrations",
        "status": status, "steps": to_jsonable(steps), "test_plan": "test_plan.json",
        "coverage": "coverage.json", "registration_batch": summary_path.relative_to(root).as_posix(),
        "patient_count": len(config.patient_mrns),
    })
    return LiveTestResult(status, tuple(steps), root / "run_summary.json", run_id)
