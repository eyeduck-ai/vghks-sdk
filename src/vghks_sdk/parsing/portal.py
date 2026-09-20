"""Interpret Portal login responses without executing server JavaScript."""

from __future__ import annotations

import re

from ..core.errors import AuthenticationError, AuthExpiredError


def parse_login_target(text: str) -> str:
    context = {"operation": "portal.login", "app": "portal", "endpoint_path": "/login.do"}
    if not text.strip():
        raise AuthenticationError(
            "portal returned an empty login response; credential validity is unknown",
            code="PORTAL_LOGIN_RESPONSE_EMPTY",
            **context,
        )
    for pattern in (
        r"\btargetUrl\s*=\s*(['\"])(?P<url>.*?)\1",
        r"(?:window\.)?location(?:\.href)?\s*=\s*(['\"])(?P<url>.*?)\1",
    ):
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if match and match.group("url").strip():
            return match.group("url").strip()
    if re.search(r"name\s*=\s*['\"]muid['\"]", text, re.I) and re.search(
        r"name\s*=\s*['\"]mpassword['\"]", text, re.I
    ):
        raise AuthExpiredError(
            "portal login remained on the login page", code="PORTAL_LOGIN_REJECTED", **context
        )
    raise AuthenticationError(
        "portal login response did not provide a target page",
        code="PORTAL_LOGIN_TARGET_MISSING",
        **context,
    )
