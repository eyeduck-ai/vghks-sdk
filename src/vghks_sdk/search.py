"""Public patient-source, SOAP-search, and match-evidence models."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from re import Pattern

from .core.errors import ConfigurationError
from .core.safe_regex import compile_safe_regex
from .identifiers import normalize_mrns
from .models import SoapRecord, VisitCase

MAX_REGEX_SOAP_TEXT_LENGTH = 250_000


@dataclass(frozen=True, slots=True)
class DoctorOpdPatientSource:
    card_no: str
    start: date
    end: date

    def __post_init__(self) -> None:
        if not isinstance(self.card_no, str):
            raise ConfigurationError("doctor OPD source requires a doctor card number")
        card_no = unicodedata.normalize("NFKC", self.card_no).strip()
        object.__setattr__(self, "card_no", card_no)
        if not card_no:
            raise ConfigurationError("doctor OPD source requires a doctor card number")
        if not isinstance(self.start, date) or not isinstance(self.end, date):
            raise ConfigurationError("doctor OPD source dates must be date values")
        if self.end < self.start:
            raise ConfigurationError("doctor OPD source end date precedes its start date")


@dataclass(frozen=True, slots=True)
class MrnPatientSource:
    mrns: tuple[str, ...]

    def __init__(self, mrns: Sequence[str]) -> None:
        if isinstance(mrns, (str, bytes)):
            raise ConfigurationError("MRN source requires a sequence of records")
        object.__setattr__(self, "mrns", tuple(normalize_mrns(mrns)))


@dataclass(frozen=True, slots=True)
class SoapSearch:
    pattern: str
    mode: str = "literal"
    ignore_case: bool = False
    multiline: bool = False
    dotall: bool = False

    def __post_init__(self) -> None:
        if self.mode not in {"literal", "regex"}:
            raise ConfigurationError("SOAP search mode must be literal or regex")
        if not isinstance(self.pattern, str) or not self.pattern:
            raise ConfigurationError("SOAP search pattern must not be empty")
        if self.mode == "literal":
            if self.multiline or self.dotall:
                raise ConfigurationError(
                    "multiline and dotall flags are available only for regex searches"
                )
        else:
            # Validate eagerly so callers can construct this value before they
            # create an SDK, read credentials, or make network requests.
            self.compile()

    def compile(self) -> re.Pattern[str]:
        if self.mode == "regex":
            return compile_safe_regex(
                self.pattern,
                ignore_case=self.ignore_case,
                multiline=self.multiline,
                dotall=self.dotall,
            )
        flags = re.IGNORECASE if self.ignore_case else 0
        return re.compile(re.escape(self.pattern), flags)


@dataclass(frozen=True, slots=True)
class SoapMatchEvidence:
    """The first occurrence in one matching case and its complete evidence."""

    matched_text: str
    start: int
    end: int
    block_indices: tuple[int, ...]
    evidence_scope: str
    evidence_block_indices: tuple[int, ...]
    evidence_blocks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SoapSearchEvaluation:
    """Local-only result of applying one search to one already fetched SOAP."""

    status: str
    text_length: int
    evidence: SoapMatchEvidence | None = None
    error_code: str = ""


def evaluate_soap_search(
    soap: SoapRecord,
    search: SoapSearch,
    *,
    compiled: Pattern[str] | None = None,
) -> SoapSearchEvaluation:
    """Search fetched SOAP blocks without issuing or enabling an HTTP request."""

    selected = [
        (index, block) for index, block in enumerate(soap.blocks) if block and block.strip()
    ]
    if not selected:
        return SoapSearchEvaluation("MISSING", 0, error_code="SOAP_MISSING")
    parts: list[str] = []
    ranges: list[tuple[int, int, int]] = []
    cursor = 0
    for position, (block_index, block) in enumerate(selected):
        if position:
            parts.append("\n\n")
            cursor += 2
        start = cursor
        parts.append(block)
        cursor += len(block)
        ranges.append((block_index, start, cursor))
    full_text = "".join(parts)
    if search.mode == "regex" and len(full_text) > MAX_REGEX_SOAP_TEXT_LENGTH:
        return SoapSearchEvaluation(
            "ERROR",
            len(full_text),
            error_code="SOAP_TEXT_TOO_LARGE",
        )
    matcher = compiled or search.compile()
    occurrence = matcher.search(full_text)
    if occurrence is None:
        return SoapSearchEvaluation("NO_MATCH", len(full_text))
    evidence = _build_evidence(
        soap=soap,
        full_text=full_text,
        ranges=tuple(ranges),
        start=occurrence.start(),
        end=occurrence.end(),
    )
    return SoapSearchEvaluation("MATCHED", len(full_text), evidence=evidence)


def _build_evidence(
    *,
    soap: SoapRecord,
    full_text: str,
    ranges: tuple[tuple[int, int, int], ...],
    start: int,
    end: int,
) -> SoapMatchEvidence:
    intersecting = [
        block_index
        for block_index, block_start, block_end in ranges
        if start < block_end and end > block_start
    ]
    contained = [
        block_index
        for block_index, block_start, block_end in ranges
        if start >= block_start and end <= block_end
    ]
    if not intersecting:
        previous = [item for item in ranges if item[2] <= start]
        following = [item for item in ranges if item[1] >= end]
        if previous:
            intersecting.append(previous[-1][0])
        if following:
            intersecting.append(following[0][0])
    block_indices = tuple(dict.fromkeys(intersecting))
    if len(contained) == 1:
        evidence_scope = "MATCHED_BLOCK"
        evidence_indices = (contained[0],)
    else:
        evidence_scope = "FULL_SOAP"
        evidence_indices = tuple(range(len(soap.blocks)))
    return SoapMatchEvidence(
        matched_text=full_text[start:end],
        start=start,
        end=end,
        block_indices=block_indices,
        evidence_scope=evidence_scope,
        evidence_block_indices=evidence_indices,
        evidence_blocks=tuple(soap.blocks[index] for index in evidence_indices),
    )


@dataclass(frozen=True, slots=True)
class SoapScanMatch:
    schema_version: int
    mrn: str
    name: str
    telephone: str
    source_type: str
    source_opd_dates: tuple[date, ...]
    case: VisitCase
    search: SoapSearch
    evidence: SoapMatchEvidence
