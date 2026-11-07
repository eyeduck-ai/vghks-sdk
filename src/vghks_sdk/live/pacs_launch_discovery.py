"""Incremental, patient-bound native PACS discovery for the desktop test runner."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any

from ..core.errors import AuthenticationError, ConfigurationError, ParseError, error_info
from ..identifiers import normalize_mrn
from ..local_io import write_json_atomic
from ..models import to_jsonable
from ..models.orders import (
    ClinicalOrder,
    OrderDetail,
    OrderDetailRef,
    OrderHistoryFilter,
    OrderReport,
    OrderReportRef,
)
from ..models.pacs_launch import PacsLaunchRef
from ..order_status import classify_order_execution


@dataclass(frozen=True, slots=True)
class PacsLaunchSource:
    order_index: int
    category: str
    stage: str
    source_file: str
    order: ClinicalOrder = field(repr=False)
    source_ref: OrderDetailRef | OrderReportRef | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class PacsLaunchCandidate:
    reference: PacsLaunchRef = field(repr=False)
    order_date: str = field(repr=False)
    execution_date: str = field(repr=False)
    name: str = field(repr=False)
    department: str = field(repr=False)
    sources: tuple[PacsLaunchSource, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class PacsLaunchDiscoveryResult:
    candidates: tuple[PacsLaunchCandidate, ...] = field(repr=False)
    summary: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _Variant:
    index: int
    category: str
    order: ClinicalOrder = field(repr=False)


@dataclass(frozen=True, slots=True)
class _Fetched:
    source_file: str
    value: OrderDetail | OrderReport | None = field(default=None, repr=False)
    error: Exception | None = field(default=None, repr=False)


def _binding(reference: OrderDetailRef | OrderReportRef) -> tuple[str, str, str, str]:
    return reference.mrn, reference.case_no, reference.case_type, reference.sequence_no


def _date_key(value: str) -> int:
    match = re.fullmatch(r"\s*(\d{3,4})[-/.](\d{1,2})[-/.](\d{1,2})(?:\s+.*)?", value)
    if match is None:
        return 0
    year, month, day = map(int, match.groups())
    if year < 1911:
        year += 1911
    try:
        return date(year, month, day).toordinal()
    except ValueError:
        return 0


class PacsLaunchDiscovery:
    """Use one existing SDK sequentially; never log in, prepare, or open a viewer.

    Private source models and per-order outcomes are saved after each stage.
    A stopped session cannot resume implicitly; callers create a new run instead.
    """

    def __init__(self, sdk: Any, mrn: str, *, output_dir: Path,
                 on_progress: Callable[[dict[str, Any]], None] | None = None,
                 raw_capture: Any = None) -> None:
        self._sdk = sdk
        self._mrn = normalize_mrn(mrn)
        self._root = Path(output_dir) / "private" / "pacs_discovery"
        self._progress = on_progress
        self._capture = raw_capture
        self._groups: list[list[_Variant]] = []
        self._candidates: dict[PacsLaunchRef, PacsLaunchCandidate] = {}
        self._cache: dict[OrderDetailRef | OrderReportRef, _Fetched] = {}
        self._outcomes: list[dict[str, Any]] = []
        self._issues: list[dict[str, Any]] = []
        self._loaded = False
        self._terminal = ""
        self._cursor = 0
        self._source_count = 0
        self._request_count = 0
        self._history_count = 0
        self._gaps = 0
        self._no_followup = 0
        self._not_executed = 0

    def _write(self, path: Path, value: Any) -> None:
        try:
            write_json_atomic(path, value)
        except Exception:
            # Losing local evidence stops discovery and blocks candidate selection.
            self._terminal = "ERROR"
            raise

    def _step(self, stage: str) -> None:
        if self._capture is not None:
            self._capture.set_live_step("pacs_launch.discovery." + stage)

    def _issue(self, exc: Exception, *, stage: str, outcome: dict | None = None) -> None:
        issue = {"stage": stage, "issue": to_jsonable(error_info(exc))}
        self._issues.append(issue)
        if outcome is not None:
            outcome["issues"].append(issue)

    def _result(self) -> PacsLaunchDiscoveryResult:
        pending = len(self._groups) - self._cursor
        complete = self._loaded and not (pending or self._issues or self._gaps or self._terminal)
        summary = {
            "schema_version": 1,
            "status": self._terminal or ("COMPLETE" if complete else "PARTIAL"),
            "complete": complete,
            "loaded": self._loaded,
            "lookback_days": 4000,
            "categories": ["*", "OR"],
            "history_queries": self._history_count,
            "source_rows": self._source_count,
            "total": len(self._groups),
            "scanned": self._cursor,
            "pending": pending,
            "candidate_count": len(self._candidates),
            "error_count": len(self._issues),
            "errors": len(self._issues),
            "gap_count": self._gaps,
            "no_followup_count": self._no_followup,
            "not_executed_count": self._not_executed,
            "detail_report_queries": self._request_count,
            "unknown_date_count": sum(not _date_key(group[0].order.order_date)
                                      for group in self._groups),
            "strategy": "STOP_VARIANT_ON_NATIVE_BUTTON",
        }
        return PacsLaunchDiscoveryResult(tuple(self._candidates.values()), summary)

    def _save(self) -> PacsLaunchDiscoveryResult:
        result = self._result()
        self._write(self._root / "candidates.json", result.candidates)
        self._write(self._root / "summary.json", result.summary)
        self._write(self._root / "issues.json", self._issues)
        if self._progress is not None:
            try:
                self._progress(dict(result.summary))
            except KeyboardInterrupt:
                self._terminal = "INTERRUPTED"
                result = self._result()
                self._write(self._root / "summary.json", result.summary)
        return result

    def load(self) -> PacsLaunchDiscoveryResult:
        if self._loaded or self._terminal:
            return self._result()
        self._write(self._root / "request.json", {
            "mrn": self._mrn, "lookback_days": 4000, "categories": ["*", "OR"],
        })
        groups: dict[tuple[str, ...], list[_Variant]] = {}
        try:
            for category in ("*", "OR"):
                self._step("history")
                self._history_count += 1
                try:
                    rows = self._sdk.orders.get_order_history(
                        self._mrn, OrderHistoryFilter(lookback_days="all", category=category))
                    self._write(self._root / f"history_{self._history_count}.json", rows)
                    if not isinstance(rows, (list, tuple)) or any(
                            not isinstance(row, ClinicalOrder) for row in rows):
                        raise ParseError("unexpected order history", code="PACS_DISCOVERY_HISTORY_INVALID")
                    for row in rows:
                        self._source_count += 1
                        groups.setdefault(row.identity, []).append(
                            _Variant(self._source_count, category, row))
                except AuthenticationError as exc:
                    self._issue(exc, stage="HISTORY")
                    self._terminal = "AUTHENTICATION_FAILED"
                    break
                except Exception as exc:
                    self._issue(exc, stage="HISTORY")
                    if self._terminal:
                        break
            self._loaded = True
            if self._history_count and not groups and self._issues and not self._terminal:
                self._terminal = "ERROR"
        except KeyboardInterrupt:
            self._terminal = "INTERRUPTED"
        finally:
            self._groups = sorted(groups.values(), key=lambda group: max(
                _date_key(variant.order.order_date) for variant in group), reverse=True)
        return self._save()

    def _fetch(self, reference: OrderDetailRef | OrderReportRef) -> _Fetched:
        if reference in self._cache:
            return self._cache[reference]
        detail = isinstance(reference, OrderDetailRef)
        self._step("detail" if detail else "report")
        self._request_count += 1
        source_file = f"pages/{self._request_count:06d}.json"
        try:
            value = (self._sdk.orders.get_order_detail(reference) if detail
                     else self._sdk.orders.get_order_report(reference))
            self._write(self._root / source_file, {"reference": reference, "value": value})
            expected = OrderDetail if detail else OrderReport
            if not isinstance(value, expected) or value.reference != reference:
                raise ParseError("order response binding mismatch", code="PACS_DISCOVERY_RESPONSE_MISMATCH")
            fetched = _Fetched(source_file, value=value)
        except KeyboardInterrupt:
            self._write(self._root / source_file, {"reference": reference, "status": "INTERRUPTED"})
            raise
        except Exception as exc:
            self._write(self._root / (source_file + ".issue.json"), {
                "reference": reference, "issue": error_info(exc),
            })
            fetched = _Fetched(source_file, error=exc)
        self._cache[reference] = fetched
        return fetched

    def _check_ref(self, reference: OrderDetailRef | OrderReportRef, order: ClinicalOrder,
                   expected: OrderDetailRef | OrderReportRef | None = None) -> None:
        if not isinstance(reference, (OrderDetailRef, OrderReportRef)):
            raise ParseError("unexpected order reference", code="PACS_DISCOVERY_REFERENCE_INVALID")
        reference.__post_init__()
        if (reference.mrn, reference.case_no, reference.case_type) != (
                self._mrn, order.case_no, order.case_type) or (
                expected is not None and _binding(reference) != _binding(expected)):
            raise ParseError("cross-order reference", code="PACS_DISCOVERY_REFERENCE_MISMATCH")

    def _add(self, references: tuple[PacsLaunchRef, ...], variant: _Variant, stage: str,
             source_file: str, source_ref: OrderDetailRef | OrderReportRef | None,
             outcome: dict, fields: Mapping[str, str] | None = None) -> bool:
        found = False
        for reference in references:
            try:
                if type(reference) is not PacsLaunchRef:
                    raise ParseError("unexpected native reference", code="PACS_DISCOVERY_NATIVE_REF_INVALID")
                reference.__post_init__()
                if reference.mrn != self._mrn:
                    raise ParseError("native patient mismatch", code="PACS_DISCOVERY_NATIVE_REF_MISMATCH")
                source = PacsLaunchSource(variant.index, variant.category, stage, source_file,
                                          variant.order, source_ref)
                existing = self._candidates.get(reference)
                if existing is None:
                    department = source_ref.department if isinstance(source_ref, OrderReportRef) else ""
                    for label in ("科別", "開單科別", "檢查科別", "申請科別"):
                        if not department and fields and isinstance(fields.get(label), str):
                            department = fields[label]
                    self._candidates[reference] = PacsLaunchCandidate(
                        reference, variant.order.order_date, variant.order.execution_date,
                        variant.order.name, department, (source,))
                elif source not in existing.sources:
                    self._candidates[reference] = replace(existing, sources=(*existing.sources, source))
                if reference not in outcome["references"]:
                    outcome["references"].append(reference)
                found = True
            except Exception as exc:
                self._issue(exc, stage=stage, outcome=outcome)
        return found

    def _page(self, reference: OrderDetailRef | OrderReportRef, outcome: dict) -> _Fetched:
        fetched = self._fetch(reference)
        outcome["stages"].append({"reference": reference, "source_file": fetched.source_file,
                                   "status": "ERROR" if fetched.error else "OK"})
        if fetched.error is not None:
            self._issue(fetched.error, stage="DETAIL" if isinstance(reference, OrderDetailRef)
                        else "REPORT", outcome=outcome)
            if isinstance(fetched.error, AuthenticationError):
                raise fetched.error
        return fetched

    def _variant(self, variant: _Variant, outcome: dict) -> None:
        order = variant.order
        if order.mrn != self._mrn:
            raise ParseError("order patient mismatch", code="PACS_DISCOVERY_ORDER_MISMATCH")
        direct = (order.pacs_launch_ref,) if order.pacs_launch_ref is not None else ()
        if self._add(direct, variant, "ORDER", "history_1.json" if variant.category == "*"
                     else "history_2.json", None, outcome):
            return
        if classify_order_execution(order.status) == "NOT_EXECUTED":
            self._not_executed += 1
            outcome["notes"].append("SKIPPED_NOT_EXECUTED")
            return
        reports = []
        if order.report_ref is not None:
            try:
                self._check_ref(order.report_ref, order, order.detail_ref)
                reports.append(order.report_ref)
            except Exception as exc:
                self._issue(exc, stage="REPORT_REFERENCE", outcome=outcome)
        if order.detail_ref is not None:
            try:
                self._check_ref(order.detail_ref, order)
                fetched = self._page(order.detail_ref, outcome)
                if fetched.value is not None:
                    detail = fetched.value
                    if self._add(detail.pacs_launch_refs, variant, "DETAIL", fetched.source_file,
                                 order.detail_ref, outcome, detail.fields):
                        return
                    for reference in detail.report_refs:
                        try:
                            self._check_ref(reference, order, order.detail_ref)
                            if reference not in reports:
                                reports.append(reference)
                        except Exception as exc:
                            self._issue(exc, stage="REPORT_REFERENCE", outcome=outcome)
            except AuthenticationError:
                raise
            except Exception as exc:
                self._issue(exc, stage="DETAIL", outcome=outcome)
        if self._terminal:
            return
        for reference in reports:
            fetched = self._page(reference, outcome)
            if fetched.value is not None:
                report = fetched.value
                found = self._add(report.pacs_launch_refs, variant, "REPORT", fetched.source_file,
                                  reference, outcome, report.fields)
                if report.report_data_status == "PARTIAL" or report.text_extraction_notes:
                    self._gaps += 1
                    outcome["notes"].append("REPORT_EXTRACTION_INCOMPLETE")
                if found:
                    return
        if order.detail_ref is None and not reports:
            self._no_followup += 1
            outcome["notes"].append("NO_FOLLOWUP_REFERENCE")

    def scan_next(self, batch_size: int = 30) -> PacsLaunchDiscoveryResult:
        if type(batch_size) is not int or batch_size < 1:
            raise ConfigurationError("batch size must be positive", code="PACS_DISCOVERY_BATCH_INVALID")
        if not self._loaded and not self._terminal:
            self.load()
        if self._terminal:
            return self._result()
        stop = min(len(self._groups), self._cursor + batch_size)
        try:
            while self._cursor < stop:
                group = self._groups[self._cursor]
                self._cursor += 1
                outcome = {"scan_index": self._cursor, "source_rows": group, "status": "RUNNING",
                           "stages": [], "references": [], "issues": [], "notes": []}
                self._outcomes.append(outcome)
                path = self._root / f"orders/{self._cursor:06d}.json"
                self._write(path, outcome)
                try:
                    for variant in group:
                        try:
                            self._variant(variant, outcome)
                        except AuthenticationError:
                            raise
                        except Exception as exc:
                            self._issue(exc, stage="ORDER", outcome=outcome)
                        finally:
                            self._write(path, outcome)
                        if self._terminal:
                            break
                    outcome["status"] = self._terminal or (
                        "PARTIAL" if outcome["issues"] else "SCANNED")
                except AuthenticationError:
                    outcome["status"] = "AUTHENTICATION_FAILED"
                    self._terminal = "AUTHENTICATION_FAILED"
                except KeyboardInterrupt:
                    outcome["status"] = "INTERRUPTED"
                    self._terminal = "INTERRUPTED"
                finally:
                    self._write(path, outcome)
                    self._save()
                if self._terminal:
                    break
        except KeyboardInterrupt:
            self._terminal = "INTERRUPTED"
        return self._save()


def select_pacs_launch_candidate(result: PacsLaunchDiscoveryResult,
                                 one_based_index: int) -> PacsLaunchCandidate:
    """Return the exact displayed candidate; interrupted or failed sessions cannot launch."""
    if result.summary.get("status") in {"AUTHENTICATION_FAILED", "INTERRUPTED", "ERROR"}:
        raise ConfigurationError("PACS discovery is stopped", code="PACS_DISCOVERY_SELECTION_BLOCKED")
    if type(one_based_index) is not int or not 1 <= one_based_index <= len(result.candidates):
        raise ConfigurationError("select a displayed PACS candidate", code="PACS_DISCOVERY_SELECTION_INVALID")
    return result.candidates[one_based_index - 1]
