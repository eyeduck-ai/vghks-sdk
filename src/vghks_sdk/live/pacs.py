"""Bounded PACS sampling and evidence summaries for the hospital tester."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..local_io import write_json_atomic
from ..models import ClinicalOrder, to_jsonable
from ..order_status import classify_order_execution
from ..workflows.order_reports import matching_order_terms


def select_pacs_orders(
    sources: Mapping[str, Sequence[ClinicalOrder]], *, terms: Sequence[str], limit: int
) -> tuple[dict[str, list[ClinicalOrder]], dict[str, Any]]:
    """Prefer explicit image evidence, retaining every source variant of a row."""
    groups: dict[tuple[str, ...], list[tuple[str, ClinicalOrder]]] = {}
    for source, orders in sources.items():
        for order in orders:
            groups.setdefault(order.identity, []).append((source, order))

    def priority(group):
        eligible = [order for _, order in group
                    if classify_order_execution(order.status) != "NOT_EXECUTED"]
        if not eligible:
            return 4, "NOT_EXECUTED"
        if any(order.pacs_ref for order in eligible):
            return 0, "PACS_REFERENCE"
        if not any(order.detail_ref or order.report_ref for order in eligible):
            return 4, "NO_NAVIGATION_REFERENCE"
        if any("jpg" in order.attachment.casefold() or "pacs" in order.attachment.casefold()
               for order in eligible):
            return 1, "IMAGE_LABEL"
        if any(matching_order_terms(order, terms) for order in eligible):
            return 2, "PREFERRED_TERM"
        return 3, "REPORT_OR_DETAIL_REFERENCE"

    candidates = sorted(groups.values(), key=lambda group: max(order.order_date for _, order in group),
                        reverse=True)
    candidates.sort(key=lambda group: priority(group)[0])
    selected: dict[str, list[ClinicalOrder]] = {}
    rows = []
    count = 0
    for group in candidates:
        rank, reason = priority(group)
        take = rank < 4 and count < limit
        if take:
            count += 1
            for source, order in group:
                selected.setdefault(source, []).append(order)
        rows.append({
            "status": "SELECTED" if take else "SKIPPED_SAMPLE_LIMIT" if rank < 4 else "SKIPPED",
            "reason": reason,
            "sources": [{"source": source, "order": to_jsonable(order)} for source, order in group],
        })
    return selected, {"schema_version": 1, "order_limit_total": limit,
                      "selected_orders": count, "discovered_orders": len(groups), "orders": rows}


def summarize_pacs_downloads(root: Path, *, blocked: bool, download_assets: bool) -> dict[str, Any]:
    """Count unique references and stored downloads without merging two modes."""
    references: dict[str, dict[str, Any]] = {}
    for path in sorted((root / "stage2_orders").glob("*.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        for ref in row.get("image_refs", ()):
            references[json.dumps(ref, sort_keys=True)] = ref
    outcomes = {}
    for path in sorted((root / "stage3_assets").glob("*.outcome.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        if row.get("operation") == "prq.pacs_image":
            outcomes[json.dumps(row["ref"], sort_keys=True)] = row
    modes = {}
    for mode in ("imgUrl", "uid"):
        refs = {key: ref for key, ref in references.items()
                if ("imgUrl" if ref.get("img_url") else "uid") == mode}
        audits = [outcomes[key] for key in refs if key in outcomes]
        files = [{key: audit[key] for key in ("asset", "byte_count", "sha256")}
                 for audit in audits if audit.get("status") == "OK" and audit.get("asset")]
        errors = sum(audit.get("status") == "ERROR" for audit in audits)
        status = (
            "BLOCKED" if not refs and blocked else
            "NO_SAMPLE" if not refs else
            "SKIPPED" if not download_assets else
            "ERROR" if errors or len(files) != len(refs) else "OK"
        )
        modes[mode] = {"status": status, "references": len(refs), "downloaded_files": len(files),
                       "download_errors": errors, "downloaded_bytes": sum(f["byte_count"] for f in files),
                       "files": files}
    summary = {"schema_version": 1, "download_assets": download_assets, "modes": modes,
               "text_source": "HTML report; stored images are original bytes, without OCR"}
    write_json_atomic(root / "pacs_verification.json", summary)
    return summary
