"""Whitelist recorded password-test observations; never execute its probes."""

from __future__ import annotations

import re

from ..models.auth import PasswordStatus
from .bundle import BundleReader

_STATUSES = {"OK", "ERROR", "EMPTY", "BLOCKED", "NO_SAMPLE", "NOT_TESTED", "CAPTURED"}


def password_test_summary(reader: BundleReader) -> dict | None:
    path = "parsed/password/summary.json"
    if path not in reader.names:
        return None
    source = reader.json(path)
    if not isinstance(source, dict):
        source = {}

    def fields(value):
        return value if isinstance(value, dict) else {}

    def status(value):
        return value if isinstance(value, str) and value in _STATUSES else "UNKNOWN"

    def code(value):
        return value if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", value) else ""

    def policy(value):
        try:
            model = PasswordStatus(**value)
        except (TypeError, ValueError):
            return None
        return {"status": model.status, "remaining_days": model.remaining_days, "evidence": model.evidence}

    page = fields(source.get("change_page"))
    catalog = fields(source.get("existing_cookie_catalog"))
    anonymous = fields(source.get("anonymous_catalog"))
    budget = fields(source.get("password_post_budget"))
    return {
        "login_status": status(source.get("login_status")),
        "login_error_code": code(source.get("login_error_code")),
        "credential_validity": source.get("credential_validity") if isinstance(source.get("credential_validity"), str)
                               and source["credential_validity"] in {"ACCEPTED", "REJECTED", "UNKNOWN"} else "UNKNOWN",
        "password_status": policy(source.get("password_status")),
        "change_page": {"status": status(page.get("status")), "captured": page.get("captured") is True,
                        "password_status": policy(page.get("password_status"))},
        "anonymous_catalog_accepted": anonymous.get("query_accepted")
                                      if type(anonymous.get("query_accepted")) is bool else None,
        "existing_cookie_catalog": {"status": status(catalog.get("status")),
                                    "query_accepted": catalog.get("query_accepted") if
                                    type(catalog.get("query_accepted")) is bool else None,
                                    "error_code": code(catalog.get("error_code"))},
        "sdk_catalog_status": status(source.get("sdk_catalog_status")),
        "patient_read_status": status(source.get("patient_read_status")),
        "password_post_budget": {key: budget.get(key) if type(budget.get(key)) is int
                                 and 0 <= budget[key] <= 100 else None
                                 for key in ("password_posts", "blocked_posts")},
        "scope": "Recorded observations only. Catalog access may be anonymous and does not prove credentials or all patient-record access; natural TTL is not tested.",
    }
