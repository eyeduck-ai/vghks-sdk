"""Interpret Portal login responses without executing server JavaScript."""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..core.errors import AuthenticationError, LoginRejectedError, PasswordChangeRequiredError
from ..core.jsliteral import decode_js_string, split_top_level, strip_js_comments
from ..models.auth import PasswordStatus

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


def is_password_change_destination(target: str, source_url: str, portal_base_url: str) -> bool:
    destination = urlsplit(urljoin(source_url, target))
    portal = urlsplit(portal_base_url)
    return (
        destination.scheme == portal.scheme
        and destination.netloc.lower() == portal.netloc.lower()
        and destination.path.lower()
        in {
            "/changepassword.do",
            "/changepwd.do",
            "/modifypassword.do",
        }
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
    if parse_password_status(text, login_stage=True).status == "CHANGE_REQUIRED":
        raise PasswordChangeRequiredError("portal requires a password change", **context)
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


def parse_password_status(text: str, *, login_stage: bool = False) -> PasswordStatus:
    """Read visible policy prose or top-level literal alerts, never run JavaScript.

    Unknown layouts stay NO_NOTICE. The full original response is a raw-capture
    concern; this result carries neither message text nor account/form values.
    """

    soup = BeautifulSoup(text, "html.parser")
    messages = []
    alert = re.compile(
        r"(?:window\.)?(?:alert|confirm)\s*\(\s*(?P<literal>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')\s*\)\s*",
        re.DOTALL,
    )
    for script in soup.find_all("script"):
        if script.get("src") or str(script.get("type", "")).lower() not in {
            "",
            "text/javascript",
            "application/javascript",
            "module",
        }:
            continue
        source = strip_js_comments(script.get_text())
        if "`" in source:
            continue
        for statement in split_top_level(source, ";"):
            match = alert.fullmatch(statement.strip())
            if match:
                message = decode_js_string(match.group("literal"))
                if message:
                    messages.append((message, "SCRIPT_LITERAL"))
    for node in soup.select("script, style, template, noscript, [hidden], [aria-hidden='true']"):
        node.decompose()
    for node in list(soup.find_all(style=True)):
        if node.parent is not None and re.search(
            r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", str(node.get("style", "")), re.I
        ):
            node.decompose()
    # Hidden optional password dialogs are not a required login step.
    change_form = False
    authenticated_navigation = any(
        urlsplit(target).path.lower().lstrip("/") == "myportal.do"
        for _, target in _navigation_literals(text)
    )
    if login_stage and not authenticated_navigation:
        for form in soup.find_all("form"):
            names = {
                re.sub(r"[^a-z]", "", str(node.get("name", "")).lower())
                for node in form.find_all("input", attrs={"type": re.compile("^password$", re.I)})
            }
            change_form |= bool(
                names & {"oldpassword", "oldpwd", "oldpass", "currentpassword"}
            ) and bool(names & {"newpassword", "newpwd", "newpass"})
    messages.insert(0, (soup.get_text(" ", strip=True), "VISIBLE_TEXT"))
    warnings = []
    for message, evidence in messages:
        compact = re.sub(r"\s+", "", message)
        if "密碼" not in compact and "密码" not in compact and "password" not in message.casefold():
            continue
        if re.search(r"(?:若|如果|假如|當|当).{0,8}(?:密碼|密码)", compact) or re.search(
            r"\b(?:if|when)\s+(?:your\s+)?password\b", message, re.I,
        ):
            continue
        # The recorded Portal alert wraps the countdown in full-width brackets.
        # Normalize only numeric bracket pairs; other message content stays intact.
        countdown = re.sub(
            r"【\s*(\d{1,5})\s*】|\[\s*(\d{1,5})\s*\]",
            lambda match: next(value for value in match.groups() if value is not None),
            message,
        )
        count = re.search(
            r"(?:剩餘|剩余|尚餘|尚有|還有|还有|倒數|倒数|再|於|于|將在|将在)\s*(\d{1,5})\s*(?:天|日)|(\d{1,5})\s*(?:天|日)(?:後|后|內|内)|(?:\b(?:in|remaining)\s+(\d{1,5})\s+days?\b)",
            countdown,
            re.I,
        )
        days = int(next(value for value in count.groups() if value is not None)) if count else None
        expired = re.search(
            r"(?:密碼|密码)(?:(?:使用)?期限|效期|有效期)?已(?:經)?(?:到期|過期|逾期|失效)", compact
        ) or re.search(r"password\s+(?:(?:has|is)\s+)?expired", message, re.I)
        required = re.search(
            r"(?:必須|必需|強制|需先|需要先|請先|請立即).{0,12}(?:變更|更改|修改|重設).{0,4}(?:密碼|密码)|(?:變更|更改|修改)(?:密碼|密码)(?:後|后)(?:才能|才可).{0,5}(?:登入|登录)",
            compact,
        ) or re.search(r"must\s+(?:change|reset|update)\s+(?:your\s+)?password", message, re.I)
        if re.search(r"(?:無|不)(?:須|需|需要|必須).{0,8}(?:密碼|密码)", compact):
            required = None
        if expired or (required and not (days is not None and days > 0)):
            return PasswordStatus("CHANGE_REQUIRED", evidence=evidence)
        if (
            count
            and days <= 36500
            and re.search(r"到期|過期|有效|變更|更改|修改|失效|expire|chang|remain", message, re.I)
        ):
            warnings.append((days, evidence))
    if change_form:
        return PasswordStatus("CHANGE_REQUIRED", evidence="LOGIN_CHANGE_FORM")
    if warnings:
        days = {item[0] for item in warnings}
        return PasswordStatus("EXPIRING", days.pop() if len(days) == 1 else None, warnings[0][1])
    return PasswordStatus()
