"""Interpret Portal login responses without executing server JavaScript."""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..core.errors import AuthenticationError, LoginRejectedError
from ..core.jsliteral import decode_js_string, split_top_level, strip_js_comments

_LOGIN_PATHS = {"/index.do", "/login.do", "/logout.do"}
_ASSIGNMENT = re.compile(
    r"^(?:(?:var|let|const)\s+)?"
    r"(?P<name>targetUrl|(?:(?:window|top|self|document)\.)?location(?:\.href)?)\s*=\s*"
    r"""(?P<literal>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')\s*$""",
    re.DOTALL,
)


def _navigation_literals(text: str) -> list[tuple[str, str]]:
    """Read top-level literal assignments, never buttons, comments or callbacks."""
    values = []
    for script in BeautifulSoup(text, "html.parser").find_all("script"):
        if script.get("src") or str(script.get("type", "")).lower() not in {
            "",
            "text/javascript",
            "application/javascript",
            "module",
        }:
            continue
        source = strip_js_comments(script.get_text())
        # Template literals and nonliteral expressions are outside this grammar.
        if "`" in source:
            continue
        for statement in split_top_level(source, ";"):
            match = _ASSIGNMENT.fullmatch(statement.strip())
            if match:
                value = decode_js_string(match.group("literal"))
                if value and value.strip():
                    values.append((match.group("name"), value.strip()))
    return values


def has_portal_login_rejection(text: str) -> bool:
    if has_portal_login_form(text):
        return True
    if "登入失敗" not in text and "帳號或密碼錯誤" not in text:
        return False
    soup = BeautifulSoup(text, "html.parser")
    for node in soup.select("script, style, template"):
        node.decompose()
    visible = re.sub(r"\s+", "", soup.get_text())
    return "帳號或密碼錯誤" in visible or any(
        "登入失敗" in heading.get_text() for heading in soup.find_all(re.compile(r"^h[1-6]$"))
    )


def is_portal_login_destination(target: str, source_url: str, portal_base_url: str) -> bool:
    """Recognize the recorded Portal/legacy entrance; this never follows a URL."""
    destination = urlsplit(urljoin(source_url, target))
    portal = urlsplit(portal_base_url)
    hosts = {portal.hostname}
    if portal.hostname == "portal.vghks.gov.tw":
        hosts.add("intranet.vghks.gov.tw")
    return (
        destination.scheme in {"http", "https"}
        and destination.hostname in hosts
        and (destination.path.lower() in _LOGIN_PATHS or destination.path in {"", "/"})
    )


def has_portal_login_redirect(text: str, response_url: str, portal_base_url: str) -> bool:
    if "location" not in text:
        return False
    return any(
        name != "targetUrl" and is_portal_login_destination(target, response_url, portal_base_url)
        for name, target in _navigation_literals(text)
    )


def has_portal_login_form(text: str) -> bool:
    """Recognize actual login inputs, not JavaScript examples or error prose."""
    lowered = text.lower()
    if "muid" not in lowered or "mpassword" not in lowered:
        return False
    soup = BeautifulSoup(text, "html.parser")
    names = {str(node.get("name", "")).lower() for node in soup.find_all("input")}
    return {"muid", "mpassword"}.issubset(names)


def parse_login_target(text: str) -> str:
    context = {"operation": "portal.login", "app": "portal", "endpoint_path": "/login.do"}
    if not text.strip():
        raise AuthenticationError(
            "portal returned an empty login response; credential validity is unknown",
            code="PORTAL_LOGIN_RESPONSE_EMPTY",
            **context,
        )
    if has_portal_login_rejection(text):
        raise LoginRejectedError(
            "portal login remained on the login page; rejection reason is unknown",
            code="PORTAL_LOGIN_REJECTED",
            **context,
        )
    assignments = _navigation_literals(text)
    targets = {value for name, value in assignments if name == "targetUrl"}
    if not targets:
        targets = {value for _, value in assignments}
    if len(targets) == 1:
        return targets.pop()
    if targets:
        raise AuthenticationError(
            "portal returned ambiguous navigation targets",
            code="PORTAL_LOGIN_TARGET_AMBIGUOUS",
            **context,
        )
    raise AuthenticationError(
        "portal login response did not provide a target page",
        code="PORTAL_LOGIN_TARGET_MISSING",
        **context,
    )
