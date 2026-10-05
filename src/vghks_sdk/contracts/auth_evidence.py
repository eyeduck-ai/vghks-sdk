"""Pure validation of recorded auth-test evidence; never runs a scenario."""

from __future__ import annotations

import re
from typing import Any


def expected_rejection(step: dict) -> bool:
    """Only this completed, bounded live scenario can explain a rejection capture."""
    details = step.get("details") or {}
    return (
        step.get("name") in {"login.negative.1", "login.negative.2", "failures.live.negative_before_login"}
        and step.get("status") == "OK"
        and details.get("evidence") == "LIVE"
        and details.get("expected_failure") is True
        and details.get("observed_code") == "PORTAL_LOGIN_REJECTED"
        and details.get("password_posts") == 1
        and details.get("blocked_posts") == 0
        and (step.get("name") != "failures.live.negative_before_login" or (
            step.get("operation") == "portal.login"
            and details.get("position") == "BEFORE_CORRECT_LOGIN"
        ))
    )


def expected_anonymous_challenge(step: dict) -> bool:
    details = step.get("details") or {}
    return (
        step.get("name") == "failures.live.unauthenticated"
        and step.get("operation") == "prq.upload_types"
        and step.get("status") == "OK"
        and details.get("evidence") == "LIVE_UNAUTHENTICATED"
        and details.get("prior_login") is False
        and details.get("expected_challenge") is True
        and details.get("observed_code") == "AUTH_NOT_AUTHENTICATED"
        and details.get("password_posts") == 0
        and details.get("blocked_posts") == 0
    )


def in_capture_range(row: dict, step: dict) -> bool:
    values = [row.get("capture_id"), step.get("first_capture_id"), step.get("last_capture_id")]
    if not all(isinstance(value, str) and re.fullmatch(r"\d{6}", value) for value in values):
        return False
    return values[1] <= values[0] <= values[2]


def verified_cookie_recovery(step: dict[str, Any]) -> bool:
    if step.get("status") != "OK":
        return False
    details = step.get("details") or {}
    if step.get("name") == "login.cookie_loss":
        return details.get("recovered") is True
    return (
        step.get("name") == "failures.live.cookie_loss"
        and details.get("evidence") == "LIVE_COOKIE_LOSS"
        and details.get("recovery_observed") is True
        and details.get("natural_ttl_verified") is False
        and type(details.get("generation_before")) is int
        and type(details.get("generation_after")) is int
        and details["generation_after"] > details["generation_before"]
    )
