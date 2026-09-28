"""Read the recorded PRQ patient-access review form without running JavaScript."""

from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from ..core.errors import ConfigurationError, ParseError

_FORM_FIELDS = frozenset({
    "reqCode", "value(status)", "value(smr_hid)", "value(smr_source)",
    "value(smr_caseInfo)", "value(smr_hhisnum)", "value(smr_caseNo)",
    "value(smr_caseType)", "value(smr_Flg)", "value(smr_nextForward)",
    "value(inCaseFlg)", "value(bgnDt)", "value(endDt)",
    "value(causeOther1)", "value(causeOther2)", "value(causeOther7)",
    "value(causeOther8)", "value(causeOther9)", "value(causeOther99)",
})
_REQUIRED_FIELDS = frozenset({
    "reqCode", "value(status)", "value(smr_hid)", "value(smr_hhisnum)",
    "value(smr_Flg)", "value(inCaseFlg)", "value(bgnDt)", "value(endDt)",
})
_CAUSE_NAME = "valueA(cause)"
_CAUSE_CODE = re.compile(r"[A-Za-z0-9]{1,4}\Z")


@dataclass(frozen=True, slots=True, repr=False)
class PatientAccessReviewForm:
    """Session-bound form fields stay internal and must not enter diagnostics."""

    inputs: tuple[tuple[str, str, str], ...]
    offered_reasons: frozenset[str]

    def payload_for(self, reason: str) -> tuple[tuple[str, str], ...]:
        if reason not in self.offered_reasons:
            raise ConfigurationError(
                "access-review reason was not offered by the current page",
                code="PRQ_ACCESS_REVIEW_REASON_UNAVAILABLE",
            )
        return tuple(
            (name, value)
            for name, value, kind in self.inputs
            if name in _FORM_FIELDS or (name == _CAUSE_NAME and kind == "checkbox" and value == reason)
        )


def parse_patient_access_review_form(
    html_text: str,
    *,
    expected_mrn: str,
    expected_hid: str,
) -> PatientAccessReviewForm | None:
    """Return a validated challenge, or None for a page without its form."""

    form = BeautifulSoup(html_text, "html.parser").find("form", id="addForm")
    if form is None:
        return None
    if str(form.get("method", "")).lower() != "post" or str(form.get("action", "")).strip() not in {
        "../../../EMRProcess.do", "/PRQWeb/EMRProcess.do"
    }:
        raise ParseError("access-review form target changed", code="PRQ_ACCESS_REVIEW_FORM_INVALID")

    inputs: list[tuple[str, str, str]] = []
    fields: dict[str, str] = {}
    offered: set[str] = set()
    for node in form.select("input[name]"):
        name = str(node.get("name", ""))
        value = str(node.get("value", ""))
        kind = str(node.get("type", "text")).lower()
        if name == _CAUSE_NAME:
            if kind != "checkbox" or not _CAUSE_CODE.fullmatch(value) or value in offered:
                raise ParseError("access-review reason changed", code="PRQ_ACCESS_REVIEW_FORM_INVALID")
            offered.add(value)
            inputs.append((name, value, kind))
        elif name in _FORM_FIELDS:
            if name in fields or kind not in {"hidden", "text"}:
                raise ParseError("access-review field changed", code="PRQ_ACCESS_REVIEW_FORM_INVALID")
            fields[name] = value
            inputs.append((name, value, kind))
        elif kind == "hidden":
            raise ParseError("access-review hidden field changed", code="PRQ_ACCESS_REVIEW_FORM_INVALID")

    if not _REQUIRED_FIELDS.issubset(fields) or not offered:
        raise ParseError("access-review fields were missing", code="PRQ_ACCESS_REVIEW_FORM_INVALID")
    if fields["reqCode"] != "saveAccessCause" or not fields["value(status)"] or not fields["value(smr_Flg)"]:
        raise ParseError("access-review state changed", code="PRQ_ACCESS_REVIEW_FORM_INVALID")
    if fields["value(smr_hhisnum)"] != expected_mrn:
        raise ParseError("access-review patient did not match", code="PRQ_ACCESS_REVIEW_PATIENT_MISMATCH")
    if fields["value(smr_hid)"] != expected_hid:
        raise ParseError("access-review login context did not match", code="PRQ_ACCESS_REVIEW_HID_MISMATCH")
    if any(value for name, value in fields.items() if name.startswith("value(causeOther")):
        raise ParseError("access-review free-text reason was already set", code="PRQ_ACCESS_REVIEW_FORM_INVALID")
    return PatientAccessReviewForm(tuple(inputs), frozenset(offered))
