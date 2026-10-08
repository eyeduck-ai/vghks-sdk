"""Produce a validated test recipe from recorded scope without running it."""

from __future__ import annotations

from typing import Any

from ..live.config import resolve_live_test_config
from ..queries import QUERY_BY_KEY


def build_retest_config(
    config: dict[str, Any], matrix: list[dict[str, Any]], main: dict[str, str] | None, status: str
) -> dict[str, Any]:
    allowed = {
        "login_negative_attempts",
        "visit_filter",
        "doctor_card",
        "opd_date",
        "range_start",
        "range_end",
        "visit_date",
        "order_date",
        "download_assets",
        "asset_terms",
        "all_matching_orders",
        "request_policy",
        "ca_bundle",
        "allow_unverified_tls",
        "endpoint_overrides",
        "max_cases",
        "max_items",
        "weekly_opd_soap",
        "weekly_opd_end",
        "include_earnings",
        "surgery_query",
        "review_query",
    }
    value = {key: item for key, item in config.items() if key in allowed}
    if config.get("profile") in {"session", "password"}:
        value.update(schema_version=7, profile=config["profile"], only_operations=[],
                     session_pause=config.get("session_pause", False), login_negative_attempts=0,
                     download_assets=False)
        resolved = resolve_live_test_config(json_values=value, environ={})
        resolved.validate_for_execution()
        result = resolved.to_safe_dict()
        result.pop("output_root", None)
        return result
    auth_failed = main and main["phase"] in {"CONNECTIVITY", "AUTHENTICATION"}
    value.update(schema_version=6, profile="auth" if auth_failed else "atomic")
    value["only_operations"] = (
        []
        if auth_failed
        else [
            row["operation"]
            for row in matrix
            if row["live_status"] in {"FAILED", "BLOCKED", "MISSING", "EMPTY"}
        ]
    )
    if config.get("profile") in {"comprehensive", "ophthalmology", "dbr", "visits", "login", "regression", "scans"}:
        # First-run scenarios include category/date variants which an atomic
        # default call would not reproduce. Preserve the complete scenario set.
        value.update(
            profile=config["profile"],
            only_operations=(config.get("only_operations") or [])
            if config["profile"] in {"comprehensive", "regression"}
            else [],
        )
    if value.get("include_earnings") and value["profile"] == "auth":
        value["profile"] = "comprehensive"
    if not auth_failed and not value["only_operations"] and config.get("profile") == "atomic":
        value["only_operations"] = config.get("only_operations") or []
    # An interruption can leave requested operations with no checkpoint yet.
    if (
        value["profile"] == "atomic"
        and not auth_failed
        and status in {"INTERRUPTED", "RECOVERED_INCOMPLETE"}
    ):
        requested = config.get("only_operations") or [row["operation"] for row in matrix]
        for row in matrix:
            if row["operation"] in requested and row["live_status"] == "NOT_TESTED":
                if QUERY_BY_KEY[row["operation"]].scope.startswith("doctor_") and not config.get(
                    "doctor_card"
                ):
                    continue
                if (
                    QUERY_BY_KEY[row["operation"]].scope in {"image", "pdf", "surgery_pdf"}
                    and config.get("download_assets") is False
                ):
                    continue
                value["only_operations"].append(row["operation"])
    # Validate the generated recipe, but do not load credentials, TLS, or SDK.
    resolved = resolve_live_test_config(json_values=value, environ={})
    resolved.validate_for_execution()
    result = resolved.to_safe_dict()
    result.pop("output_root", None)
    return result
