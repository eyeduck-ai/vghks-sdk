"""Typed assets data for the public SDK."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from hashlib import sha256 as calculate_sha256
from urllib.parse import urlsplit

from ..core.errors import ConfigurationError
from ._validation import (
    _fully_unquote,
    _require_navigation_values,
    _validate_navigation_ref,
    re_split_path,
)


@dataclass(frozen=True, slots=True)
class PacsStudyRef:
    mrn: str
    request_no: str

    def __post_init__(self) -> None:
        _validate_navigation_ref(self.mrn, self.request_no)
        _require_navigation_values(self.request_no)


@dataclass(frozen=True, slots=True)
class PacsImageRef:
    mrn: str
    request_no: str
    series_uid: str
    study_uid: str
    uid: str = ""

    def __post_init__(self) -> None:
        _validate_navigation_ref(
            self.mrn,
            self.request_no,
            self.series_uid,
            self.study_uid,
            self.uid,
        )
        _require_navigation_values(self.request_no, self.uid)


@dataclass(frozen=True, slots=True)
class PdfAttachmentRef:
    mrn: str
    file_path: str

    def __post_init__(self) -> None:
        _validate_navigation_ref(self.mrn)
        decoded = _fully_unquote(self.file_path)
        parsed = urlsplit(decoded)
        is_recorded_unc = bool(
            re.match(
                r"^(?://|\\\\)(?:hfs\d+_[A-Za-z0-9]+[/\\]+REPORT|"
                r"(?:HFS01_1A0(?:\.vghks\.gov\.tw)?|nfs01p(?:\.vghks\.gov\.tw)?)[/\\]+EMRU|"
                r"HFS01_1A0(?:\.vghks\.gov\.tw)?[/\\]+OPG)[/\\]+",
                decoded,
                re.IGNORECASE,
            )
        )
        if (
            ((parsed.scheme or parsed.netloc) and not is_recorded_unc)
            or parsed.query
            or parsed.fragment
            or "\x00" in decoded
        ):
            raise ConfigurationError(
                "external PDF references are not allowed", code="PDF_REF_EXTERNAL"
            )
        parts = tuple(part for part in re_split_path(decoded) if part not in {"", "."})
        if not parts or ".." in parts or not decoded.casefold().endswith(".pdf"):
            raise ConfigurationError("unsafe PDF attachment reference", code="PDF_REF_INVALID")
        if not is_recorded_unc:
            raise ConfigurationError("unknown PDF attachment path", code="PDF_REF_PATH_UNKNOWN")
        if self.mrn not in parts:
            raise ConfigurationError(
                "PDF attachment MRN did not match", code="PDF_REF_MRN_MISMATCH"
            )
        object.__setattr__(self, "file_path", decoded)


@dataclass(frozen=True, slots=True)
class PacsStudy:
    reference: PacsStudyRef
    images: tuple[PacsImageRef, ...]
    data_status: str = "UNASSESSED"
    empty_reason: str = ""


@dataclass(frozen=True, slots=True)
class BinaryAsset:
    """Validated in-memory asset; JSON output intentionally omits content."""

    content: bytes = field(repr=False)
    media_type: str
    size: int = 0
    sha256: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.content, bytes):
            raise ConfigurationError(
                "binary asset content must be bytes", code="BINARY_CONTENT_INVALID"
            )
        actual_size = len(self.content)
        actual_hash = calculate_sha256(self.content).hexdigest()
        if self.size not in {0, actual_size}:
            raise ConfigurationError(
                "binary asset size does not match content", code="BINARY_SIZE_MISMATCH"
            )
        if self.sha256 and self.sha256.casefold() != actual_hash:
            raise ConfigurationError(
                "binary asset hash does not match content", code="BINARY_HASH_MISMATCH"
            )
        object.__setattr__(self, "size", actual_size)
        object.__setattr__(self, "sha256", actual_hash)

    @property
    def bytes(self) -> bytes:
        return self.content
