"""Shared live-test results, step recording and incremental local persistence."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any, TypeVar

from ..core.capture import RawCaptureSink
from ..core.diagnostics import DiagnosticRecorder
from ..core.errors import ErrorInfo, error_info
from ..local_io import restrict_permissions, write_json_atomic
from ..models import AuthCheckReport, to_jsonable

LIVE_TEST_MRN = "0000000"


LIVE_TEST_SCHEMA_VERSION = 6


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class LiveTestStep:
    name: str
    status: str
    output: str = ""
    issue: ErrorInfo | None = None
    error_id: str = ""
    details: Mapping[str, Any] | None = None
    operation: str = ""
    duration_ms: float = 0.0
    first_capture_id: str = ""
    last_capture_id: str = ""

    @property
    def error_code(self) -> str:
        return self.issue.code if self.issue is not None else ""


@dataclass(frozen=True, slots=True)
class LiveTestResult:
    status: str
    steps: tuple[LiveTestStep, ...]
    summary_path: Path
    run_id: str = ""

    @property
    def error_count(self) -> int:
        return sum(step.status in {"ERROR", "BLOCKED", "MISSING"} for step in self.steps)


def _run_step(
    steps: list[LiveTestStep],
    *,
    name: str,
    operation: Callable[[], T],
    output_path: Path | None,
    root: Path,
    raw_capture: RawCaptureSink | None = None,
    diagnostics: DiagnosticRecorder | None = None,
    aliases: tuple[Path, ...] = (),
    summarize: Callable[[T], Mapping[str, Any]] | None = None,
    classify: Callable[[T], str] | None = None,
    missing_code: str = "MISSING",
    classified_error_code: str = "",
    operation_key: str = "",
) -> tuple[T | None, BaseException | None]:
    started = monotonic()
    first = getattr(raw_capture, "capture_count", 0) + 1
    print(f"START {name}", flush=True)
    if raw_capture is not None:
        raw_capture.set_live_step(name)
    try:
        value = operation()
        if output_path is not None:
            _write_json(output_path, value)
            for alias in aliases:
                _write_json(alias, value)
        details = dict(summarize(value)) if summarize is not None else {}
        status = classify(value) if classify is not None else "OK"
        code = (
            missing_code
            if status == "MISSING"
            else classified_error_code
            if status == "ERROR"
            else ""
        )
        step = LiveTestStep(
            name=name,
            status=status,
            output=(output_path.relative_to(root).as_posix() if output_path is not None else ""),
            issue=(
                next((target.issue for target in value.targets if target.status == "ERROR"), None)
                if isinstance(value, AuthCheckReport) and not value.ok
                else ErrorInfo(code, "DATA")
                if code
                else None
            ),
            details=details,
            operation=operation_key,
            duration_ms=round((monotonic() - started) * 1000, 3),
            **_capture_range(raw_capture, first),
        )
        steps.append(step)
        if diagnostics is not None:
            diagnostics.record_probe_step(
                name=name,
                status=status,
                error_code_value=code,
                details=details,
            )
        return value, None
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        issue = error_info(exc)
        error_id = raw_capture.record_error(error=exc, step=name) if raw_capture is not None else ""
        step = LiveTestStep(
            name=name,
            status="ERROR",
            issue=issue,
            error_id=error_id,
            operation=operation_key,
            duration_ms=round((monotonic() - started) * 1000, 3),
            **_capture_range(raw_capture, first),
        )
        steps.append(step)
        if diagnostics is not None:
            diagnostics.record_probe_step(name=name, status="ERROR", error=exc)
        return None, exc
    finally:
        if raw_capture is not None:
            raw_capture.set_live_step(None)
        # Persist completed work after every unit, including when the next
        # operation is interrupted. No response values enter this journal.
        write_json_atomic(root / "step_results.json", to_jsonable(steps))
        if steps and steps[-1].name == name:
            last = steps[-1]
            line = f"{last.status} {name} ({last.duration_ms:.0f} ms) {last.error_code}".rstrip()
            print(line, flush=True)
            with (root / "progress.log").open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")


def _capture_range(capture: RawCaptureSink | None, first: int) -> dict[str, str]:
    last = getattr(capture, "capture_count", 0)
    if last < first:
        return {}
    return {"first_capture_id": f"{first:06d}", "last_capture_id": f"{last:06d}"}


def _target_ready(report: AuthCheckReport | None, key: str) -> bool:
    if report is None:
        return False
    target = next((item for item in report.targets if item.target == key), None)
    # Small offline SDK fakes historically returned only Portal while reporting
    # overall OK. A real readiness report always includes requested targets.
    return report.ok if target is None else target.status == "OK"


def _blocked_step(steps: list[LiveTestStep], name: str, code: str) -> None:
    steps.append(
        LiveTestStep(
            name=name,
            status="BLOCKED",
            issue=ErrorInfo(code, "DEPENDENCY"),
        )
    )


def _overall_status(steps: list[LiveTestStep], *, fatal_auth: bool) -> str:
    if fatal_auth:
        authentication = [
            step
            for step in steps
            if step.name == "auth_check" or step.name.startswith("auth_check.")
        ]
        failures = authentication or steps
        if any(step.issue and step.issue.category == "NETWORK" for step in failures):
            return "CONNECTIVITY_FAILED"
        if any(step.issue and step.issue.category == "HTTP" for step in failures):
            return "HTTP_FAILED"
        return "AUTHENTICATION_FAILED"
    ready = {
        step.name.removeprefix("auth_check.")
        for step in steps
        if step.name.startswith("auth_check.") and step.status == "OK"
    }
    if any(
        step.status in {"ERROR", "BLOCKED", "MISSING"}
        and not (
            step.name.startswith("network.")
            and step.name.split(".")[1] in ready
            and not step.name.endswith(".configure")
        )
        for step in steps
    ):
        return "COMPLETED_WITH_ERRORS"
    if any(step.status == "NO_SAMPLE" or (step.details or {}).get("acquisition_status") == "PARTIAL"
           for step in steps):
        return "COMPLETED_WITH_GAPS"
    return "OK"


def _write_json(path: Path, value: Any) -> None:
    write_json_atomic(path, to_jsonable(value))
    restrict_permissions(path)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _warning() -> str:
    return (
        "UNREDACTED: this bundle contains credentials, cookies, tokens, patient "
        "data, SOAP, and replayable authenticated session state. ZIP is not encrypted."
    )
