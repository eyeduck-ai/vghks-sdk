"""Compose order/report/PDF/PACS atoms without coupling independent branches."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.errors import ConfigurationError, ParseError, error_info
from ..local_io import write_json_atomic
from ..models import BinaryAsset, ClinicalOrder, OrderDetail, OrderReport, PacsStudy, to_jsonable
from ..order_status import classify_order_execution
from ..queries import run_query

QueryRunner = Callable[[str, str, dict[str, Any], Path], tuple[Any, BaseException | None]]


@dataclass(frozen=True, slots=True)
class OrderReportsResult:
    status: str
    manifest_path: Path
    counts: dict[str, int]
    coverage: dict[str, dict[str, int]]


def matching_order_terms(order: ClinicalOrder, terms: Sequence[str]) -> tuple[str, ...]:
    name = unicodedata.normalize("NFKC", order.name).casefold()
    return tuple(
        term
        for term in terms
        if re.search(r"(?<![a-z0-9])" + re.escape(term.casefold()) + r"(?![a-z0-9])", name)
    )


def collect_order_reports(
    sdk: Any,
    *,
    order_sources: Mapping[str, Sequence[ClinicalOrder]],
    output_dir: Path,
    terms: Sequence[str] = ("DBR", "Microsonography"),
    max_orders_per_term: int | None = None,
    download_assets: bool = True,
    query_runner: QueryRunner | None = None,
    blocked_reason: str = "",
) -> OrderReportsResult:
    """Keep source rows, branch results and original assets after each query.

    NOT_EXECUTED is skipped, never called a confirmed empty report. Each term
    has its own eligible-order budget; skipped rows do not consume that budget.
    All images of selected studies are visited, with no first-image shortcut.
    The default standalone composition has no order sample limit.
    """
    root = output_dir.resolve()
    if (root / "manifest.json").exists():
        raise ConfigurationError("order-report output already exists; choose a new directory")
    terms = tuple(dict.fromkeys(unicodedata.normalize("NFKC", t).strip() for t in terms))
    if not terms or not all(terms):
        raise ConfigurationError("order-report terms must not be empty")
    if max_orders_per_term is not None and (
        type(max_orders_per_term) is not int or max_orders_per_term < 1
    ):
        raise ConfigurationError("order-report sample limit must be a positive integer")
    run = _OrderReportRun(sdk, root, query_runner, download_assets)
    coverage = {term: Counter() for term in terms}
    manifest = {
        "schema_version": 1,
        "status": "RUNNING",
        "entry_path": "order_list",
        "terms": list(terms),
        "max_orders_per_term": max_orders_per_term,
        "image_limit_per_study": None,
        "download_assets": download_assets,
        "text_source": "HTML report content; PDF/JPG retained as original files, without OCR",
        "blocked_reason": blocked_reason,
        "orders": [],
    }
    counts: Counter[str] = Counter()

    def checkpoint(status="RUNNING"):
        manifest.update(
            status=status, counts=dict(counts), coverage={k: dict(v) for k, v in coverage.items()}
        )
        write_json_atomic(root / "manifest.json", manifest)

    checkpoint()
    if blocked_reason:
        checkpoint("BLOCKED")
        return OrderReportsResult("BLOCKED", root / "manifest.json", {}, {})

    buckets: dict[tuple[str, ...], list[tuple[str, ClinicalOrder]]] = {}
    for source, orders in order_sources.items():
        for order in orders:
            counts["discovered_rows"] += 1
            if matching_order_terms(order, terms):
                buckets.setdefault(order.identity, []).append((source, order))
    # Preserve source variants. If views disagree, a completed/queryable row
    # takes priority, and every original state remains in the result.
    groups = list(buckets.values())
    groups.sort(key=lambda group: max(order.order_date for _, order in group), reverse=True)
    groups.sort(
        key=lambda group: min(
            {"COMPLETED": 0, "UNKNOWN": 1, "NOT_EXECUTED": 2}[
                classify_order_execution(order.status)
            ]
            for _, order in group
        )
    )
    for number, group in enumerate(groups, 1):
        orders = [order for _, order in group]
        representative = min(
            orders,
            key=lambda order: {"COMPLETED": 0, "UNKNOWN": 1, "NOT_EXECUTED": 2}[
                classify_order_execution(order.status)
            ],
        )
        matched = matching_order_terms(representative, terms)
        execution = classify_order_execution(representative.status)
        row = {
            "order_id": f"{number:04d}",
            "order": to_jsonable(representative),
            "sources": [{"source": source, "order": to_jsonable(order)} for source, order in group],
            "terms": list(matched),
            "execution_status": execution,
            "status": "PENDING",
            "branches": [],
        }
        for term in matched:
            coverage[term]["matched"] += 1
        counts["matched_orders"] += 1
        path = root / "stage2_orders" / f"{number:04d}.json"
        manifest["orders"].append(
            {"order_id": row["order_id"], "result": path.relative_to(root).as_posix()}
        )

        def save_row(row=row, path=path):
            write_json_atomic(path, row)

        save_row()
        checkpoint()
        if execution == "NOT_EXECUTED":
            row["status"] = "SKIPPED_NOT_EXECUTED"
            counts["skipped_not_executed"] += 1
            for term in matched:
                coverage[term]["skipped_not_executed"] += 1
        elif max_orders_per_term is not None and all(
            coverage[term]["selected"] >= max_orders_per_term for term in matched
        ):
            row["status"] = "SKIPPED_SAMPLE_LIMIT"
            counts["skipped_sample_limit"] += 1
            for term in matched:
                coverage[term]["skipped_sample_limit"] += 1
        else:
            for term in matched:
                coverage[term]["selected"] += 1
            counts["selected_orders"] += 1
            run.collect(orders, row, save_row)
            counts[row["status"].lower()] += 1
            for key, value in row["availability"].items():
                counts[key] += value
                for term in matched:
                    coverage[term][key] += value
        save_row()
        checkpoint()
    counts["unique_queries"] = len(run.cache)
    counts["query_errors"] = sum(audit["status"] == "ERROR" for _, audit in run.cache.values())
    status = "INCOMPLETE" if counts["query_errors"] else "OK" if groups else "NO_MATCHING_ORDERS"
    checkpoint(status)
    return OrderReportsResult(
        status, root / "manifest.json", dict(counts), {k: dict(v) for k, v in coverage.items()}
    )


class _OrderReportRun:
    def __init__(self, sdk, root, query_runner, download_assets):
        self.sdk, self.root, self.query_runner = sdk, root, query_runner
        self.download_assets = download_assets
        self.cache = {}

    def fetch(self, operation, ref):
        key = (operation, ref)
        if key in self.cache:
            value, audit = self.cache[key]
            return value, {**audit, "reused": True}
        number = len(self.cache) + 1
        name = f"{number:04d}-{operation.removeprefix('prq.')}"
        binary = operation in {"prq.pdf_attachment", "prq.pacs_image"}
        stage = "stage3_assets" if binary else "stage2_queries"
        path = self.root / stage / f"{name}.json"
        audit = {
            "operation": operation,
            "ref": to_jsonable(ref),
            "query": path.relative_to(self.root).as_posix(),
            "status": "PENDING",
            "reused": False,
        }
        write_json_atomic(
            path.with_suffix(".input.json"), {"operation": operation, "ref": to_jsonable(ref)}
        )
        value = None
        try:
            if self.query_runner is not None:
                value, error = self.query_runner(
                    f"ophthalmology.{name}", operation, {"ref": ref}, path
                )
                if error is not None:
                    raise error
            else:
                value = run_query(self.sdk, operation, ref=ref)
            expected = {
                "prq.order_detail": OrderDetail,
                "prq.order_report": OrderReport,
                "prq.pacs_study": PacsStudy,
                "prq.pdf_attachment": BinaryAsset,
                "prq.pacs_image": BinaryAsset,
            }[operation]
            if not isinstance(value, expected):
                raise ParseError("unexpected order query result", code="ORDER_QUERY_RESULT_INVALID")
            write_json_atomic(path, value)
            audit["status"] = "OK"
            if isinstance(value, BinaryAsset):
                asset_path = path.with_suffix(
                    ".pdf" if operation == "prq.pdf_attachment" else ".jpg"
                )
                asset_path.write_bytes(value.content)
                audit.update(
                    asset=asset_path.relative_to(self.root).as_posix(), byte_count=value.size
                )
            elif isinstance(value, OrderReport):
                audit.update(
                    data_status=value.report_data_status, text_characters=len(value.report_text)
                )
                if value.report_data_status == "EMPTY":
                    audit["status"] = "EMPTY"
            elif isinstance(value, PacsStudy):
                audit.update(
                    data_status=value.data_status,
                    image_count=len(value.images),
                    empty_reason=value.empty_reason,
                )
                if not value.images:
                    audit["status"] = "EMPTY"
        except Exception as exc:
            value = None
            audit.update(status="ERROR", issue=to_jsonable(error_info(exc)))
        write_json_atomic(path.with_suffix(".outcome.json"), audit)
        # Keep the file and metadata, not every downloaded binary in memory.
        # Collectors never need the bytes again after saving the asset.
        self.cache[key] = (None if binary else value), audit
        return value, audit

    def collect(self, orders, row, checkpoint):
        reports, pdfs, studies = [], [], []
        details = []
        for order in orders:
            details.extend([order.detail_ref] if order.detail_ref else [])
            reports.extend([order.report_ref] if order.report_ref else [])
            studies.extend([order.pacs_ref] if order.pacs_ref else [])
            pdfs.extend(order.pdf_refs)

        def query(operation, ref):
            value, audit = self.fetch(operation, ref)
            row["branches"].append(audit)
            checkpoint()
            return value

        for ref in dict.fromkeys(details):
            detail = query("prq.order_detail", ref)
            if detail:
                reports.extend(detail.report_refs)
                studies.extend(detail.pacs_refs)
                pdfs.extend(detail.pdf_refs)
        texts = []
        for ref in dict.fromkeys(reports):
            report = query("prq.order_report", ref)
            if report:
                if report.report_text:
                    texts.append({"reference": to_jsonable(ref), "text": report.report_text})
                pdfs.extend(report.pdf_refs)
                studies.extend(report.pacs_refs)
        row["texts"] = texts
        row["pdf_refs"] = to_jsonable(tuple(dict.fromkeys(pdfs)))
        checkpoint()
        # Always inspect JPG viewers, independently of PDF success and text.
        images = []
        for ref in dict.fromkeys(studies):
            study = query("prq.pacs_study", ref)
            if study:
                images.extend(study.images)
        row["image_refs"] = to_jsonable(tuple(dict.fromkeys(images)))
        checkpoint()
        if self.download_assets:
            for ref in dict.fromkeys(pdfs):
                query("prq.pdf_attachment", ref)
            for ref in dict.fromkeys(images):
                query("prq.pacs_image", ref)
        branches = row["branches"]
        available = {
            "reports_with_text": len(texts),
            "pdf_files": sum(
                b["operation"] == "prq.pdf_attachment" and b["status"] == "OK" for b in branches
            ),
            "jpg_files": sum(
                b["operation"] == "prq.pacs_image" and b["status"] == "OK" for b in branches
            ),
            "empty_studies": sum(
                b["operation"] == "prq.pacs_study" and b["status"] == "EMPTY" for b in branches
            ),
        }
        row["availability"] = available
        useful = any(available[key] for key in ("reports_with_text", "pdf_files", "jpg_files"))
        failed = any(b["status"] == "ERROR" for b in branches)
        row["status"] = (
            "PARTIAL_ERROR"
            if failed and useful
            else "ERROR"
            if failed
            else "COLLECTED"
            if useful
            else "NO_LINKS"
            if not branches
            else "ATTACHMENTS_NOT_DOWNLOADED"
            if (pdfs or images) and not self.download_assets
            else "NO_DATA"
            if any(b["status"] == "EMPTY" for b in branches)
            else "METADATA_ONLY"
        )
        row["content_status"] = (
            "TEXT_AVAILABLE" if texts else "BINARY_AVAILABLE" if useful else "NOT_EXTRACTED"
        )
