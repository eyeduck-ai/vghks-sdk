"""Export longitudinal and visit-scoped patient records with optional assets."""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, TypeVar

from ..core.errors import AuthenticationError, ConfigurationError, error_info
from ..identifiers import normalize_mrns
from ..local_io import write_bytes_atomic, write_json_atomic
from ..models import (
    BinaryAsset,
    ClinicalOrder,
    MedicationHistoryFilter,
    NumericHistoryFilter,
    OrderHistoryFilter,
    OrderReportRef,
    PacsStudyRef,
    SurgeryHistoryFilter,
    VisitFilter,
)
from ..services.protocols import SDKProtocol

PATIENT_RECORDS_SCHEMA_VERSION = 1
DEFAULT_ASSET_TERMS = ("Microsonography", "DBR")
_INCLUDES = {"history", "visits", "all"}
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class PatientRecordsResult:
    patient_count: int
    case_count: int
    asset_count: int
    error_count: int
    manifest_path: Path
    history_path: Path | None
    cases_dir: Path | None
    asset_manifest_path: Path | None


def export_patient_records(
    sdk: SDKProtocol,
    *,
    mrns: Sequence[str],
    output_dir: Path,
    include: str = "history",
    lookback: int | str = "all",
    visit_filter: VisitFilter | None = None,
    visit_date: date | None = None,
    order_date: date | None = None,
    order_category: str = "*",
    order_status: str = "*",
    order_subtype: str = "*",
    medication_status: str = "*",
    numeric_department: str = "*",
    numeric_subtype: str = "*",
    download_assets: bool = False,
    asset_terms: Sequence[str] = DEFAULT_ASSET_TERMS,
    all_matching_orders: bool = False,
    overwrite: bool = False,
) -> PatientRecordsResult:
    normalized_mrns = normalize_mrns(mrns)
    if not normalized_mrns:
        raise ConfigurationError("patient-records input did not contain any records")
    include = unicodedata.normalize("NFKC", include).strip().casefold()
    if include not in _INCLUDES:
        raise ConfigurationError("patient-records include must be history, visits, or all")
    include_history = include in {"history", "all"}
    include_visits = include in {"visits", "all"}
    if include_visits and visit_filter is None:
        raise ConfigurationError("visit scope requires a visit filter")
    normalized_terms = _normalize_terms(asset_terms)
    if download_assets and not normalized_terms:
        raise ConfigurationError("asset download requires at least one search term")

    destination = output_dir.expanduser().resolve()
    if destination.exists() and not overwrite:
        try:
            if any(destination.iterdir()):
                raise ConfigurationError(
                    "patient-records output directory is not empty; pass --overwrite",
                    code="OUTPUT_EXISTS",
                )
        except OSError as exc:
            raise ConfigurationError("unable to inspect patient-records output") from exc
    destination.mkdir(parents=True, exist_ok=True)

    histories: list[dict[str, Any]] = []
    case_documents: list[dict[str, Any]] = []
    candidate_orders: list[ClinicalOrder] = []
    errors: list[dict[str, Any]] = []
    case_count = 0

    order_filter: OrderHistoryFilter | None = None
    medication_filter: MedicationHistoryFilter | None = None
    numeric_filter: NumericHistoryFilter | None = None
    surgery_filter: SurgeryHistoryFilter | None = None
    if include_history:
        order_filter = OrderHistoryFilter(
            lookback_days=lookback,
            category=order_category,
            status=order_status,
            subtype=order_subtype,
            order_date=order_date,
        )
        medication_filter = MedicationHistoryFilter(
            lookback_days=lookback,
            status=medication_status,
        )
        numeric_filter = NumericHistoryFilter(
            lookback_days=lookback,
            department=numeric_department,
            subtype=numeric_subtype,
        )
        surgery_filter = SurgeryHistoryFilter(lookback_days=lookback)

    for mrn in normalized_mrns:
        if include_history:
            if any(
                item is None
                for item in (order_filter, medication_filter, numeric_filter, surgery_filter)
            ):
                raise AssertionError("history filters were not initialized")
            history_errors: list[dict[str, Any]] = []
            orders = _fetch_component(
                lambda mrn=mrn: sdk.orders.get_order_history(mrn, order_filter),
                component="orders",
                errors=history_errors,
            )
            medications = _fetch_component(
                lambda mrn=mrn: sdk.medications.get_medication_history(mrn, medication_filter),
                component="medications",
                errors=history_errors,
            )
            numeric = _fetch_component(
                lambda mrn=mrn: sdk.records.get_numeric_history(mrn, numeric_filter),
                component="numeric",
                errors=history_errors,
            )
            surgeries = _fetch_component(
                lambda mrn=mrn: sdk.records.get_surgery_history(mrn, surgery_filter),
                component="surgeries",
                errors=history_errors,
            )
            if isinstance(orders, list):
                candidate_orders.extend(item for item in orders if isinstance(item, ClinicalOrder))
            histories.append(
                {
                    "mrn": mrn,
                    "orders": orders,
                    "medications": medications,
                    "numeric": numeric,
                    "surgeries": surgeries,
                    "issues": history_errors,
                }
            )
            errors.extend(history_errors)

        if include_visits:
            case_list_errors: list[dict[str, Any]] = []
            raw_cases = _fetch_component(
                lambda mrn=mrn: sdk.records.get_visit_cases(mrn),
                component="visit_cases",
                errors=case_list_errors,
            )
            errors.extend(case_list_errors)
            if not isinstance(raw_cases, list):
                continue
            selected_cases = visit_filter.select(raw_cases) if visit_filter is not None else []
            if visit_date is not None:
                selected_cases = [item for item in selected_cases if item.visit_date == visit_date]
            for case in selected_cases:
                case_count += 1
                case_errors: list[dict[str, Any]] = []
                soap = _fetch_component(
                    lambda case=case: sdk.records.get_soap(case),
                    component="soap",
                    errors=case_errors,
                )
                numeric = _fetch_component(
                    lambda case=case: sdk.records.get_numeric_report(case),
                    component="numeric_report",
                    errors=case_errors,
                )
                orders = _fetch_component(
                    lambda case=case: sdk.orders.get_case_orders(case),
                    component="orders",
                    errors=case_errors,
                )
                medications = _fetch_component(
                    lambda case=case: sdk.medications.get_case_medications(case),
                    component="medications",
                    errors=case_errors,
                )
                consults = _fetch_component(
                    lambda case=case: sdk.records.get_consults(case),
                    component="consults",
                    errors=case_errors,
                )
                treatments = _fetch_component(
                    lambda case=case: sdk.records.get_treatments(case),
                    component="treatments",
                    errors=case_errors,
                )
                if isinstance(orders, list):
                    filtered_orders = [
                        item
                        for item in orders
                        if isinstance(item, ClinicalOrder)
                        and (
                            order_date is None or item.order_date.startswith(order_date.isoformat())
                        )
                    ]
                    candidate_orders.extend(filtered_orders)
                    orders = filtered_orders
                case_document = {
                    "schema_version": PATIENT_RECORDS_SCHEMA_VERSION,
                    "case": case,
                    "soap": soap,
                    "numeric_report": numeric,
                    "orders": orders,
                    "medications": medications,
                    "consults": consults,
                    "treatments": treatments,
                    "issues": case_errors,
                }
                case_documents.append(case_document)
                errors.extend(case_errors)

    history_path: Path | None = None
    if include_history:
        history_path = write_json_atomic(
            destination / "history.json",
            {"schema_version": PATIENT_RECORDS_SCHEMA_VERSION, "patients": histories},
        )

    cases_dir: Path | None = None
    if include_visits:
        cases_dir = destination / "cases"
        cases_dir.mkdir(parents=True, exist_ok=True)
        for index, document in enumerate(case_documents, start=1):
            write_json_atomic(cases_dir / f"case-{index:06d}.json", document)

    asset_manifest_path: Path | None = None
    asset_count = 0
    if download_assets:
        selected_orders = select_asset_orders(
            candidate_orders,
            terms=normalized_terms,
            all_matching=all_matching_orders,
        )
        asset_manifest, asset_count = download_order_assets(
            sdk,
            selected_orders=selected_orders,
            assets_dir=destination / "assets",
        )
        errors.extend(asset_manifest["issues"])
        asset_manifest_path = write_json_atomic(
            destination / "assets" / "asset_manifest.json",
            asset_manifest,
        )

    manifest_path = write_json_atomic(
        destination / "manifest.json",
        {
            "schema_version": PATIENT_RECORDS_SCHEMA_VERSION,
            "include": include,
            "patient_count": len(normalized_mrns),
            "case_count": case_count,
            "asset_count": asset_count,
            "error_count": len(errors),
            "files": {
                "history": history_path.name if history_path is not None else None,
                "cases_directory": cases_dir.name if cases_dir is not None else None,
                "asset_manifest": (
                    str(Path("assets") / asset_manifest_path.name)
                    if asset_manifest_path is not None
                    else None
                ),
            },
            "issues": errors,
        },
    )
    return PatientRecordsResult(
        patient_count=len(normalized_mrns),
        case_count=case_count,
        asset_count=asset_count,
        error_count=len(errors),
        manifest_path=manifest_path,
        history_path=history_path,
        cases_dir=cases_dir,
        asset_manifest_path=asset_manifest_path,
    )


def select_asset_orders(
    orders: Sequence[ClinicalOrder],
    *,
    terms: Sequence[str] = DEFAULT_ASSET_TERMS,
    all_matching: bool = False,
) -> list[ClinicalOrder]:
    """Select the newest order per normalized term, then deduplicate identities."""

    normalized_terms = _normalize_terms(terms)
    deduplicated: dict[tuple[str, ...], ClinicalOrder] = {}
    for order in orders:
        deduplicated.setdefault(order.identity, order)
    sorted_orders = sorted(
        deduplicated.values(),
        key=lambda item: (item.order_date, item.execution_date, item.case_no),
        reverse=True,
    )
    output: list[ClinicalOrder] = []
    seen: set[tuple[str, ...]] = set()
    for term in normalized_terms:
        matches = [item for item in sorted_orders if term in _normalize_search_text(item.name)]
        for item in matches if all_matching else matches[:1]:
            if item.identity not in seen:
                seen.add(item.identity)
                output.append(item)
    return output


def download_order_assets(
    sdk: SDKProtocol,
    *,
    selected_orders: Sequence[ClinicalOrder],
    assets_dir: Path,
) -> tuple[dict[str, Any], int]:
    assets_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    seen_pdf: set[tuple[str, str]] = set()
    seen_study: set[tuple[str, str]] = set()
    seen_image: set[tuple[str, str, str, str, str]] = set()
    sequence = 0

    for order_index, order in enumerate(selected_orders, start=1):
        report_refs: list[OrderReportRef] = []
        pacs_refs: list[PacsStudyRef] = []
        if order.report_ref is not None:
            report_refs.append(order.report_ref)
        if order.pacs_ref is not None:
            pacs_refs.append(order.pacs_ref)
        if order.detail_ref is not None:
            detail = _fetch_asset_stage(
                lambda order=order: sdk.orders.get_order_detail(order.detail_ref),
                stage="order_detail",
                order_index=order_index,
                issues=issues,
            )
            if detail is not None:
                report_refs.extend(detail.report_refs)
                pacs_refs.extend(detail.pacs_refs)

        unique_reports = dict.fromkeys(report_refs)
        pdf_refs = []
        for report_ref in unique_reports:
            report = _fetch_asset_stage(
                lambda report_ref=report_ref: sdk.orders.get_order_report(report_ref),
                stage="order_report",
                order_index=order_index,
                issues=issues,
            )
            if report is not None:
                pdf_refs.extend(report.pdf_refs)
                pacs_refs.extend(report.pacs_refs)

        for pdf_ref in pdf_refs:
            key = (pdf_ref.mrn, pdf_ref.file_path)
            if key in seen_pdf:
                continue
            seen_pdf.add(key)
            asset = _fetch_asset_stage(
                lambda pdf_ref=pdf_ref: sdk.orders.download_pdf(pdf_ref),
                stage="pdf",
                order_index=order_index,
                issues=issues,
            )
            if isinstance(asset, BinaryAsset):
                sequence += 1
                filename = f"asset-{sequence:06d}.pdf"
                write_bytes_atomic(assets_dir / filename, asset.content)
                entries.append(_asset_entry(filename, asset, "pdf", order_index))

        for pacs_ref in pacs_refs:
            study_key = (pacs_ref.mrn, pacs_ref.request_no)
            if study_key in seen_study:
                continue
            seen_study.add(study_key)
            study = _fetch_asset_stage(
                lambda pacs_ref=pacs_ref: sdk.orders.get_pacs_study(pacs_ref),
                stage="pacs_study",
                order_index=order_index,
                issues=issues,
            )
            if study is None:
                continue
            for image_ref in study.images:
                image_key = (
                    image_ref.mrn,
                    image_ref.request_no,
                    image_ref.study_uid,
                    image_ref.series_uid,
                    image_ref.uid,
                )
                if image_key in seen_image:
                    continue
                seen_image.add(image_key)
                asset = _fetch_asset_stage(
                    lambda image_ref=image_ref: sdk.orders.download_pacs_image(image_ref),
                    stage="pacs_image",
                    order_index=order_index,
                    issues=issues,
                )
                if isinstance(asset, BinaryAsset):
                    sequence += 1
                    filename = f"asset-{sequence:06d}.jpg"
                    write_bytes_atomic(assets_dir / filename, asset.content)
                    entries.append(_asset_entry(filename, asset, "pacs_jpeg", order_index))

    return (
        {
            "schema_version": PATIENT_RECORDS_SCHEMA_VERSION,
            "selected_orders": [
                {"index": index, "identity": list(order.identity), "name": order.name}
                for index, order in enumerate(selected_orders, start=1)
            ],
            "assets": entries,
            "issues": issues,
        },
        sequence,
    )


def _fetch_component(
    callback: Callable[[], T],
    *,
    component: str,
    errors: list[dict[str, Any]],
) -> T | None:
    try:
        return callback()
    except AuthenticationError:
        raise
    except Exception as exc:
        errors.append(_issue(component, exc))
        return None


def _fetch_asset_stage(
    callback: Callable[[], T],
    *,
    stage: str,
    order_index: int,
    issues: list[dict[str, Any]],
) -> T | None:
    try:
        return callback()
    except AuthenticationError:
        raise
    except Exception as exc:
        issue = _issue(stage, exc)
        issue["order_index"] = order_index
        issues.append(issue)
        return None


def _issue(component: str, exc: BaseException) -> dict[str, Any]:
    info = error_info(exc)
    return {
        "component": component,
        "code": info.code,
        "category": info.category,
    }


def _asset_entry(
    filename: str,
    asset: BinaryAsset,
    kind: str,
    order_index: int,
) -> dict[str, Any]:
    return {
        "filename": filename,
        "kind": kind,
        "order_index": order_index,
        "media_type": asset.media_type,
        "size": asset.size,
        "sha256": asset.sha256,
    }


def _normalize_terms(values: Sequence[str]) -> tuple[str, ...]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _normalize_search_text(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            output.append(normalized)
    return tuple(output)


def _normalize_search_text(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value)).casefold().strip()
