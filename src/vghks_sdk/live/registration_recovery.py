"""Rebuild derived registration checkpoints from an interrupted run's journal."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..contracts.registration_journal import (
    JOURNAL_VERSION,
    REGISTRATION_ROOT,
    patient_index,
    read_patient_journal,
    registration_batch_snapshot,
)
from ..core.errors import ConfigurationError
from ..identifiers import normalize_mrns
from ..local_io import write_json_atomic


def restore_registration_checkpoint(root: Path) -> dict[str, Any]:
    parsed = root / REGISTRATION_ROOT
    summary = parsed / "batch_summary.json"
    recorded = _read_json(root, summary)
    if not isinstance(recorded, dict) or recorded.get("patient_journal_version") != JOURNAL_VERSION:
        return {}

    index = _read_json(root, parsed / "patients.json")
    if not isinstance(index, list) or not all(isinstance(item, dict) for item in index):
        return {}
    try:
        mrns = normalize_mrns(item.get("mrn") for item in index)
    except ConfigurationError:
        return {}
    if index != patient_index(mrns):
        return {}

    journal = parsed / "patient_results.jsonl"
    _require_run_path(root, journal)
    try:
        with journal.open("rb") as handle:
            entries, journal_issue = read_patient_journal(handle, mrns)
    except FileNotFoundError:
        return {}

    checkpoint = root / "step_results.json"
    previous = _read_json(root, checkpoint)
    previous = previous if isinstance(previous, list) else []
    preflight = [
        step for step in previous
        if isinstance(step, dict) and isinstance(step.get("name"), str)
        and step["name"].startswith(("auth_check.", "network."))
    ]
    steps = preflight + [entry["step"] for entry in entries]
    coverage = [step for step in previous if isinstance(step, dict) and step.get("name") == "registrations.coverage.no_show"]
    finalized = (
        recorded.get("status") not in ("RUNNING", "INTERRUPTED")
        and len(entries) == len(mrns) and not journal_issue
        and previous == steps + coverage
    )
    if finalized:
        steps = previous
    else:
        snapshot = registration_batch_snapshot(entries, patient_count=len(mrns), status="INTERRUPTED")
        if journal_issue:
            snapshot["journal_issue"] = journal_issue
        if previous != steps:
            write_json_atomic(checkpoint, steps)
        if recorded != snapshot:
            write_json_atomic(summary, snapshot)
    return {
        "profile": "registrations", "steps": steps, "patient_count": len(mrns),
        "test_plan": "test_plan.json", "registration_batch": REGISTRATION_ROOT + "batch_summary.json",
    }


def _read_json(root: Path, path: Path) -> Any:
    _require_run_path(root, path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, RecursionError):
        return None


def _require_run_path(root: Path, path: Path) -> None:
    if not path.resolve().is_relative_to(root):
        raise ConfigurationError("registration checkpoint path is outside the run directory")
