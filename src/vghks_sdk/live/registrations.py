"""Sequential registration-only integration checks for an explicit patient list."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Any

from ..contracts.registration_journal import (
    REGISTRATION_QUERY,
    REGISTRATION_ROOT,
    patient_index,
    registration_batch_snapshot,
)
from ..core.errors import AuthenticationError, ConfigurationError, ErrorInfo
from ..core.operations import OPERATIONS
from ..local_io import restrict_permissions, write_json_atomic, write_jsonl_line
from ..models import to_jsonable
from ..parsing.webmaas import registration_counts
from ..queries import query_spec
from .atomic import _write_coverage
from .preflight import independent_readiness, run_network_checks
from .registration_recovery import restore_registration_checkpoint
from .steps import (
    LIVE_TEST_SCHEMA_VERSION,
    LiveTestResult,
    LiveTestStep,
    _overall_status,
    _run_step,
    _target_ready,
)

TARGETS = ("portal", "sectord", "webmaas")
CHECKPOINT_INTERVAL = 100


def build_registration_plan(config: Any) -> dict[str, Any]:
    spec = query_spec(REGISTRATION_QUERY)
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
        "operations": [{"key": REGISTRATION_QUERY, "sdk_method": spec.sdk_method, "scope": spec.scope,
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
    parsed = root / REGISTRATION_ROOT
    parsed.mkdir(parents=True, exist_ok=True)
    plan = build_registration_plan(config)
    write_json_atomic(root / "test_plan.json", plan)
    write_json_atomic(parsed / "patients.json", patient_index(config.patient_mrns))
    patient_count = len(config.patient_mrns)
    steps: list[LiveTestStep] = []
    entries: list[dict] = []
    common = {"root": root, "raw_capture": raw_capture, "diagnostics": diagnostics}
    summary_path = parsed / "batch_summary.json"
    journal_path = parsed / "patient_results.jsonl"
    step_path = root / "step_results.json"

    def checkpoint(status: str) -> None:
        write_json_atomic(step_path, steps)
        write_json_atomic(summary_path, registration_batch_snapshot(entries, patient_count=patient_count, status=status))

    fatal = False
    try:
        with journal_path.open("w", encoding="utf-8", newline="\n") as journal:
            restrict_permissions(journal_path)
            write_json_atomic(summary_path, registration_batch_snapshot(entries, patient_count=patient_count, status="RUNNING"))
            if settings is not None:
                run_network_checks(
                    settings, steps, only=TARGETS, allow_unverified_tls=config.allow_unverified_tls,
                    configure_connection=getattr(sdk, "configure_connection", None), **common,
                )
            readiness = independent_readiness(sdk, steps, only=TARGETS, **common)
            fatal = not _target_ready(readiness, "portal")
            ready = all(_target_ready(readiness, key) for key in TARGETS)
            write_json_atomic(step_path, steps)

            def commit(step: LiveTestStep, *, index: int, mrn: str, codes: dict[str, int]) -> None:
                entry = {
                    "index": index, "mrn": mrn, "step": to_jsonable(step),
                    "registered_by_codes": codes if step.status in ("OK", "EMPTY") else {},
                }
                write_jsonl_line(journal, entry, durable=True)
                entries.append(entry)
                if index % CHECKPOINT_INTERVAL == 0:
                    checkpoint("RUNNING")

            for index, mrn in enumerate(config.patient_mrns, 1):
                name = f"registrations.patient_{index:06d}"
                codes: dict[str, int] = {}
                persist = partial(commit, index=index, mrn=mrn, codes=codes)
                if fatal or not ready:
                    steps.append(LiveTestStep(
                        name, "BLOCKED", operation=REGISTRATION_QUERY,
                        issue=ErrorInfo("READINESS_FAILED" if not ready else "AUTHENTICATION_FAILED",
                                        "DEPENDENCY"),
                    ))
                    persist(steps[-1])
                else:
                    def summarize(rows, codes=codes):
                        counts = registration_counts(rows)
                        codes.update(counts.pop("registered_by_codes"))
                        return counts

                    print(f"Patient {index}/{patient_count}", flush=True)
                    _, error = _run_step(
                        steps, name=name, operation_key=REGISTRATION_QUERY,
                        operation=lambda mrn=mrn: sdk.patients.get_registration_history(mrn),
                        output_path=parsed / "patients" / f"{index:06d}.json",
                        classify=lambda rows: "OK" if rows else "EMPTY",
                        summarize=summarize, persist_step=persist, **common,
                    )
                    fatal = isinstance(error, AuthenticationError)
    except KeyboardInterrupt:
        restore_registration_checkpoint(root)
        raise
    totals = registration_batch_snapshot(entries, patient_count=patient_count, status="RUNNING")
    if totals["patient_status_counts"]["OK"] or totals["patient_status_counts"]["EMPTY"]:
        steps.append(LiveTestStep(
            "registrations.coverage.no_show", "OK" if totals["no_show_count"] else "NO_SAMPLE",
            output=summary_path.relative_to(root).as_posix(),
            details={key: totals[key] for key in ("no_show_count", "attendance_unknown_count")},
        ))
    status = _overall_status(steps, fatal_auth=fatal)
    write_json_atomic(step_path, steps)
    write_json_atomic(summary_path, {**totals, "status": status})
    _write_coverage(root, plan, steps, status)
    write_json_atomic(root / "run_summary.json", {
        "schema_version": LIVE_TEST_SCHEMA_VERSION, "run_id": run_id, "profile": "registrations",
        "status": status, "steps": to_jsonable(steps), "test_plan": "test_plan.json",
        "coverage": "coverage.json", "registration_batch": summary_path.relative_to(root).as_posix(),
        "patient_count": patient_count,
    })
    return LiveTestResult(status, tuple(steps), root / "run_summary.json", run_id)
