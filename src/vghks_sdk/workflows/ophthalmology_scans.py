"""Reconcile eye-visit scan links with the complete upload history."""

from __future__ import annotations

from dataclasses import dataclass

from ..core.errors import AuthenticationError, ConfigurationError, SDKError
from ..models import PdfAttachmentRef, ScannedRecord, VisitCase, VisitFilter
from ..services.protocols import SDKProtocol


@dataclass(frozen=True, slots=True)
class EyeScanCaseLink:
    """A PDF reference proven to occur on an ophthalmology visit page."""

    case: VisitCase
    pdf_ref: PdfAttachmentRef


@dataclass(frozen=True, slots=True)
class EyeScanCaseIssue:
    case: VisitCase
    error_code: str


@dataclass(frozen=True, slots=True)
class OphthalmologyScansResult:
    """All identifiable eye scans; unresolved RECORD rows remain separate."""

    scans: tuple[ScannedRecord, ...]
    case_links: tuple[EyeScanCaseLink, ...]
    unclassified_history: tuple[ScannedRecord, ...]
    eye_case_count: int
    checked_case_count: int
    case_issues: tuple[EyeScanCaseIssue, ...] = ()
    history_error_code: str = ""
    visits_error_code: str = ""

    @property
    def complete(self) -> bool:
        """Every eye visit and the history endpoint were queried successfully."""

        return (
            not self.history_error_code
            and not self.visits_error_code
            and not self.case_issues
            and self.checked_case_count == self.eye_case_count
        )


def collect_ophthalmology_scans(
    sdk: SDKProtocol,
    mrn: str,
    *,
    max_cases: int | None = None,
) -> OphthalmologyScansResult:
    """Collect OPG history plus every PDF linked by eye outpatient visits.

    A RECORD row is counted as an eye scan only when its PDF also occurs on a
    verified eye visit. The remaining RECORD rows are returned separately and
    may belong to other departments. No PDF bytes are downloaded.
    """

    if not isinstance(mrn, str) or not mrn.strip():
        raise ConfigurationError("ophthalmology scans require a patient MRN")
    mrn = mrn.strip()
    if max_cases is not None and (type(max_cases) is not int or max_cases < 1):
        raise ConfigurationError("max_cases must be a positive integer")

    history_records: tuple[ScannedRecord, ...] = ()
    history_error_code = ""
    try:
        history_records = sdk.records.get_upload_history(mrn).scanned_records
    except SDKError as exc:
        history_error_code = exc.info.code
        if isinstance(exc, AuthenticationError):
            return OphthalmologyScansResult(
                (), (), (), 0, 0,
                history_error_code=history_error_code,
                visits_error_code="AUTH_DEPENDENCY_BLOCKED",
            )

    cases: list[VisitCase] = []
    visits_error_code = ""
    try:
        cases = VisitFilter(section_name_contains=("眼科",)).select(
            sdk.records.get_visit_cases(mrn)
        )
    except SDKError as exc:
        visits_error_code = exc.info.code

    selected = cases[:max_cases] if max_cases is not None else cases
    links: list[EyeScanCaseLink] = []
    seen_links: set[tuple[tuple[str, str, str, str, str], PdfAttachmentRef]] = set()
    issues: list[EyeScanCaseIssue] = []
    checked = 0
    for case in selected:
        try:
            records = sdk.records.get_case_scanned_records(case)
        except SDKError as exc:
            issues.append(EyeScanCaseIssue(case, exc.info.code))
            if isinstance(exc, AuthenticationError):
                break
            continue
        checked += 1
        for record in records:
            if record.pdf_ref.mrn != case.mrn:
                issues.append(EyeScanCaseIssue(case, "SCAN_REF_MRN_MISMATCH"))
                continue
            link = EyeScanCaseLink(case, record.pdf_ref)
            identity = (case.identity, record.pdf_ref)
            if identity not in seen_links:
                links.append(link)
                seen_links.add(identity)

    case_refs = {link.pdf_ref for link in links}
    scans: list[ScannedRecord] = []
    unclassified: list[ScannedRecord] = []
    seen_refs: set[PdfAttachmentRef] = set()
    for record in history_records:
        if record.record_type != "OPG" and record.pdf_ref not in case_refs:
            unclassified.append(record)
            continue
        if record.pdf_ref not in seen_refs:
            scans.append(record)
            seen_refs.add(record.pdf_ref)
    for link in links:
        if link.pdf_ref not in seen_refs:
            scans.append(ScannedRecord(None, link.pdf_ref))
            seen_refs.add(link.pdf_ref)

    return OphthalmologyScansResult(
        tuple(scans),
        tuple(links),
        tuple(unclassified),
        len(cases),
        checked,
        tuple(issues),
        history_error_code,
        visits_error_code,
    )
