"""Synthetic public defaults; optional private defaults belong only in local EXEs."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ..core.errors import ConfigurationError

SYNTHETIC_MRN = "0000000"


def default_test_mrn() -> str:
    path = Path(__file__).with_name("_live_defaults.json")
    if not getattr(sys, "frozen", False) or not path.is_file():
        return SYNTHETIC_MRN
    values = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(values, dict) or set(values) != {"test_mrn"}:
        raise ConfigurationError("invalid embedded test defaults", code="LIVE_DEFAULTS_INVALID")
    mrn = values["test_mrn"]
    if not isinstance(mrn, str) or not mrn.strip():
        raise ConfigurationError("embedded test MRN is empty", code="TEST_MRN_REQUIRED")
    return mrn.strip()
