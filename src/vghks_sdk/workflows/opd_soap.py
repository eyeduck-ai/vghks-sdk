"""Compose OPD registration, same-day visits and SOAP queries with checkpoints."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from ..core.errors import ConfigurationError, ParseError, error_info
from ..local_io import write_json_atomic
from ..models import OutpatientPatient, SoapRecord, VisitCase, to_jsonable
from ..queries import run_query
from ..search import DoctorOpdPatientSource, SoapSearch, evaluate_soap_search

# A live-test runner supplies this boundary to associate raw HTTP captures and
# diagnostic events with each atomic call. Standalone use needs only the SDK.
QueryRunner = Callable[[str, str, dict[str, Any], Path], tuple[Any, BaseException | None]]
DEFAULT_OPD_SEARCH = SoapSearch("arrange CATA", ignore_case=True)


@dataclass(frozen=True, slots=True)
class OpdSoapResult:
    status: str
    manifest_path: Path
    matches_path: Path
    counts: dict[str, int]


def classify_opd_registration(patient: OutpatientPatient, *, doctor_card: str) -> str:
    """Use the returned room physician, including the observed F suffix.

    The exact account or account + F is accepted; arbitrary prefixes and other
    physicians are not. Section codes never establish list ownership.
    """
    account = doctor_card.strip().upper()
    physician = patient.doctor_card.strip().upper()
    if account and physician in {account, account + "F"}:
        return "DEDICATED"
    if patient.doctor_label_present and not physician:
        return "SHARED"
    return "UNCLASSIFIED"


def select_registration_visits(
    registrations: list[OutpatientPatient], cases: list[VisitCase], *, doctor_card: str
) -> list[VisitCase]:
    """Require the same patient, registration date, section and outpatient type."""
    keys = {
        (row.mrn, row.visit_date, row.section_code.strip())
        for row in registrations
        if classify_opd_registration(row, doctor_card=doctor_card) == "DEDICATED"
        and row.section_code.strip()
    }
    unique = {
        case.identity: case
        for case in cases
        if case.case_type.strip().upper() == "O"
        and (case.patient_mrn, case.visit_date, case.section_code.strip()) in keys
    }
    return sorted(unique.values(), key=lambda case: case.identity)


def scan_opd_soap(
    sdk: Any,
    *,
    source: DoctorOpdPatientSource,
    output_dir: Path,
    search: SoapSearch = DEFAULT_OPD_SEARCH,
    query_runner: QueryRunner | None = None,
    blocked_reason: str = "",
) -> OpdSoapResult:
    """Query every date/patient without sample limits; preserve all three stages.

    Visit and SOAP queries are grouped by patient so PRQ's patient context is
    not switched between discovering a case and fetching its SOAP. Individual
    query failures remain unknown, never negative attendance/search evidence.
    """
    root = output_dir.resolve()
    if (root / "manifest.json").exists():
        raise ConfigurationError("OPD SOAP output already exists; choose a new directory")
    runner = _OpdSoapRun(sdk, source, search, root, query_runner)
    runner.checkpoint()
    if blocked_reason:
        runner.stages = {name: "BLOCKED" for name in runner.stages}
        runner.blocked_reason = blocked_reason
        return runner.finish("BLOCKED")
    try:
        runner.collect_registrations()
        runner.check_visits_and_soap()
    except (KeyboardInterrupt, SystemExit):
        runner.finish("INTERRUPTED")
        raise
    except Exception:
        runner.finish("ERROR")
        raise
    incomplete = any(
        runner.counts[key]
        for key in (
            "days_failed",
            "unclassified_registrations",
            "unqueryable_registrations",
            "visit_query_errors",
            "registrations_unknown",
            "soap_errors",
            "soap_missing",
        )
    )
    return runner.finish("INCOMPLETE" if incomplete else "OK")


class _OpdSoapRun:
    def __init__(self, sdk, source, search, root, query_runner):
        self.sdk = sdk
        self.source = source
        self.search = search
        self.root = root
        self.query_runner = query_runner
        self.stages = {
            "stage1_opd": "PENDING",
            "stage2_visits": "PENDING",
            "stage3_soap": "PENDING",
        }
        self.counts = dict.fromkeys(
            (
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
            ),
            0,
        )
        self.counts["days_total"] = (source.end - source.start).days + 1
        self.registrations: dict[str, list[tuple[str, OutpatientPatient]]] = defaultdict(list)
        self.matches: list[dict[str, Any]] = []
        self.matched_patients: set[str] = set()
        self.blocked_reason = ""

    def checkpoint(self, status="RUNNING"):
        for stage, state in self.stages.items():
            write_json_atomic(
                self.root / stage / "status.json", {"status": state, "counts": self.counts}
            )
        write_json_atomic(self.root / "matches.json", self.matches)
        write_json_atomic(
            self.root / "manifest.json",
            {
                "schema_version": 2,
                "workflow": "opd_soap_week",
                "status": status,
                "source": to_jsonable(self.source),
                "search": to_jsonable(self.search),
                "stages": self.stages,
                "counts": self.counts,
                "blocked_reason": self.blocked_reason,
                "policy": {
                    "dedicated_doctor_codes": [self.source.card_no, self.source.card_no + "F"],
                    "shared_lists": "explicit_empty_room_doctor",
                    "unknown_doctors": "retain_for_review_without_querying_patient",
                    "section_role": "match_visit_to_registration; never_determine_ownership",
                    "same_registration_date_required": True,
                    "case_type": "O",
                    "doctor_identity": "exact_returned_room_doctor; no_query_account_fallback",
                    "patient_limit": None,
                    "case_limit": None,
                    "absence_means": "no_matching_record_returned_at_query_time",
                    "raw_evidence": "live_step links to capture_manifest.jsonl in the live-test ZIP",
                },
            },
        )

    def finish(self, status):
        if status in {"ERROR", "INTERRUPTED"}:
            self.stages = {
                name: status if state in {"PENDING", "RUNNING"} else state
                for name, state in self.stages.items()
            }
        self.checkpoint(status)
        return OpdSoapResult(
            status, self.root / "manifest.json", self.root / "matches.json", dict(self.counts)
        )

    def fetch(self, stage, label, operation, arguments, expected_type):
        output = self.root / stage / f"{label}.returned.json"
        step = f"weekly_opd.{stage}.{label}"
        audit = {
            "live_step": step,
            "operation": operation,
            "inputs": to_jsonable(arguments),
            "status": "RUNNING",
            "returned_file": "",
            "issue": None,
        }
        audit_path = self.root / stage / f"{label}.query.json"
        write_json_atomic(audit_path, audit)
        try:
            if self.query_runner is None:
                value = run_query(self.sdk, operation, **arguments)
                error = None
            else:
                value, error = self.query_runner(step, operation, arguments, output)
            if error is not None:
                raise error
            # Store the result before validating its shape, for parser debugging.
            write_json_atomic(output, to_jsonable(value))
            audit["returned_file"] = output.relative_to(self.root).as_posix()
            valid = (
                isinstance(value, expected_type)
                if expected_type is SoapRecord
                else (
                    isinstance(value, list) and all(isinstance(row, expected_type) for row in value)
                )
            )
            if not valid:
                raise ParseError(
                    "unexpected workflow query result", code="OPD_WORKFLOW_RESULT_INVALID"
                )
            audit["status"] = "OK" if value else "EMPTY"
        except Exception as exc:
            value = None
            audit.update(status="ERROR", issue=to_jsonable(error_info(exc)))
        write_json_atomic(audit_path, audit)
        return value, audit

    def collect_registrations(self):
        self.stages["stage1_opd"] = "RUNNING"
        day = self.source.start
        while day <= self.source.end:
            label = day.isoformat()
            patients, audit = self.fetch(
                "stage1_opd",
                label,
                "prq.opd_patients",
                {"doctor_card": self.source.card_no, "visit_date": day},
                OutpatientPatient,
            )
            rows = []
            section_counts: Counter[str] = Counter()
            if patients is None:
                self.counts["days_failed"] += 1
            else:
                self.counts["days_completed"] += 1
                for index, patient in enumerate(patients, 1):
                    scope = classify_opd_registration(patient, doctor_card=self.source.card_no)
                    if patient.visit_date != day:
                        scope = "UNCLASSIFIED"
                    registration_id = f"{label}-{index:04d}"
                    rows.append(
                        {
                            "registration_id": registration_id,
                            "scope": scope,
                            "queryable": scope == "DEDICATED" and bool(patient.mrn.strip()),
                            "issue": "MISSING_MRN" if not patient.mrn.strip() else "",
                            "patient": to_jsonable(patient),
                        }
                    )
                    section_counts[patient.section_code or "UNKNOWN"] += 1
                    self.counts["registrations"] += 1
                    self.counts[f"{scope.lower()}_registrations"] += 1
                    if scope == "DEDICATED":
                        if patient.mrn.strip():
                            self.registrations[patient.mrn].append((registration_id, patient))
                        else:
                            self.counts["unqueryable_registrations"] += 1
            write_json_atomic(
                self.root / "stage1_opd" / f"{label}.json",
                {
                    "query": audit,
                    "date": label,
                    "section_counts": dict(section_counts),
                    "registrations": rows,
                },
            )
            self.counts["patients_total"] = len(self.registrations)
            self.checkpoint()
            day += timedelta(days=1)
        self.stages["stage1_opd"] = (
            "INCOMPLETE"
            if (
                self.counts["days_failed"]
                or self.counts["unclassified_registrations"]
                or self.counts["unqueryable_registrations"]
            )
            else "OK"
        )
        self.checkpoint()

    def check_visits_and_soap(self):
        self.stages["stage2_visits"] = "RUNNING"
        self.stages["stage3_soap"] = "RUNNING"
        for index, (mrn, registrations) in enumerate(self.registrations.items(), 1):
            label = f"patient-{index:04d}"
            cases, audit = self.fetch(
                "stage2_visits", label, "prq.visit_cases", {"mrn": mrn}, VisitCase
            )
            self.counts["patients_checked"] += 1
            if cases is None:
                self.counts["visit_query_errors"] += 1
            selected = select_registration_visits(
                [row for _, row in registrations], cases or [], doctor_card=self.source.card_no
            )
            decisions = []
            for registration_id, patient in registrations:
                matching = select_registration_visits(
                    [patient], selected, doctor_card=self.source.card_no
                )
                ambiguous = not patient.section_code.strip() or any(
                    case.patient_mrn == mrn
                    and case.case_type.strip().upper() == "O"
                    and case.section_code.strip() in {patient.section_code.strip(), ""}
                    and (
                        case.visit_date is None
                        or (case.visit_date == patient.visit_date and not case.section_code.strip())
                    )
                    for case in cases or []
                )
                status = (
                    "UNKNOWN"
                    if cases is None or ambiguous
                    else "HAS_VISIT"
                    if matching
                    else "NO_SAME_DATE_VISIT"
                )
                counter = {
                    "UNKNOWN": "registrations_unknown",
                    "HAS_VISIT": "registrations_with_visit",
                    "NO_SAME_DATE_VISIT": "registrations_without_visit",
                }[status]
                self.counts[counter] += 1
                decisions.append(
                    {
                        "registration_id": registration_id,
                        "patient": to_jsonable(patient),
                        "status": status,
                        "matching_cases": to_jsonable(matching),
                    }
                )
            write_json_atomic(
                self.root / "stage2_visits" / f"{label}.json",
                {
                    "query": audit,
                    "registrations": decisions,
                    "selected_cases": to_jsonable(selected),
                },
            )
            self.counts["soap_cases"] += len(selected)
            self.checkpoint()
            for case_index, case in enumerate(selected, 1):
                self.check_soap(f"{label}-case-{case_index:04d}", case, registrations)
        self.stages["stage2_visits"] = (
            "INCOMPLETE"
            if (self.counts["visit_query_errors"] or self.counts["registrations_unknown"])
            else "OK"
            if self.registrations
            else "NO_INPUT"
        )
        self.stages["stage3_soap"] = (
            "INCOMPLETE"
            if (self.counts["soap_errors"] or self.counts["soap_missing"])
            else "OK"
            if self.counts["soap_checked"]
            else "NO_INPUT"
        )

    def check_soap(self, label, case, registrations):
        soap, audit = self.fetch("stage3_soap", label, "prq.soap", {"case": case}, SoapRecord)
        self.counts["soap_checked"] += 1
        evaluation = None
        if soap is None:
            status = "ERROR"
        elif soap.case.identity != case.identity:
            status = "ERROR"
            audit["issue"] = {"code": "SOAP_CASE_MISMATCH"}
        else:
            evaluation = evaluate_soap_search(soap, self.search)
            status = evaluation.status
        if status == "ERROR":
            self.counts["soap_errors"] += 1
        elif status == "MISSING":
            self.counts["soap_missing"] += 1
        sources = [
            {"registration_id": key, "patient": to_jsonable(row)}
            for key, row in registrations
            if select_registration_visits([row], [case], doctor_card=self.source.card_no)
        ]
        result = {
            "query": audit,
            "status": status,
            "case": to_jsonable(case),
            "registrations": sources,
            "soap": to_jsonable(soap),
            "evaluation": to_jsonable(evaluation),
        }
        result_path = self.root / "stage3_soap" / f"{label}.json"
        write_json_atomic(result_path, result)
        if status == "MATCHED":
            self.matches.append(
                {**result, "evidence_file": result_path.relative_to(self.root).as_posix()}
            )
            self.matched_patients.add(case.patient_mrn)
            self.counts["matches"] = len(self.matches)
            self.counts["matched_patients"] = len(self.matched_patients)
        self.checkpoint()
