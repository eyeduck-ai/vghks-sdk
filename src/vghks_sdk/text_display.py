"""Opt-in display of confirmed source-specific patient-name characters."""

from __future__ import annotations

from typing import TypedDict

_PROFILE_VERSION = "1"
# Only mappings explicitly confirmed for this source belong here. Decoding,
# stored source names and identity comparisons must retain the original text.
_PATIENT_NAME_MAPPINGS = {
    "vghks": {"\ue3a4": "\u51c3", "\uea81": "\u771e", "\ue03e": "\u7d89", "\ue897": "\u6052"},
}


class PatientNameProfile(TypedDict):
    source: str
    version: str
    mapping: dict[str, str]


def patient_name_profile(source: str = "vghks") -> PatientNameProfile:
    """Return a fresh display profile; unknown sources have no substitutions."""
    return {
        "source": source,
        "version": _PROFILE_VERSION,
        "mapping": dict(_PATIENT_NAME_MAPPINGS.get(source, {})),
    }


def display_patient_name(name: str, source: str = "vghks") -> str:
    """Return display text without changing the source name or its identity."""
    mapping = _PATIENT_NAME_MAPPINGS.get(source, {})
    return "".join(mapping.get(character, character) for character in name)
