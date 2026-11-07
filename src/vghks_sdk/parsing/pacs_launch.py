"""Parse the recorded SmartWonder launch pages without executing JavaScript."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl

from bs4 import BeautifulSoup

from ..core.errors import ConfigurationError, NotAuthenticatedError, ParseError
from ..core.jsliteral import decode_js_string, strip_js_comments
from ..models.pacs_launch import (
    SMARTWONDER_BASE_URL,
    PacsLaunchBootstrap,
    PacsLaunchContext,
    PacsLaunchRef,
)

_LITERAL = r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')'''
_STRINGS = re.compile(_LITERAL + r"|`(?:\\.|[^`\\])*`", re.DOTALL)


def _failure(code: str = "PACS_LAUNCH_STRUCTURE_UNRECOGNIZED") -> ParseError:
    return ParseError("PACS launch page did not match the recorded contract", code=code)


def _scripts(html_text: str) -> list[str]:
    soup = BeautifulSoup(html_text, "html.parser")
    if soup.select_one('input[type="password" i]') is not None:
        raise NotAuthenticatedError(
            "PACS launch page requires authentication", code="PACS_LAUNCH_NOT_AUTHENTICATED"
        )
    sources = []
    for script in soup.find_all("script"):
        if script.get("src") or str(script.get("type", "")).lower() not in {
            "", "text/javascript", "application/javascript",
        }:
            continue
        source = strip_js_comments(script.get_text())
        # Legacy script wrappers are standalone tokens, never part of a literal.
        for match in re.finditer(r"(?m)^[\t ]*(?:<!--|-->)[\t ]*\r?$", _masked(source)):
            source = source[:match.start()] + " " * len(match.group()) + source[match.end():]
        sources.append(source)
    return sources


def _masked(source: str) -> str:
    return _STRINGS.sub(lambda m: " " * len(m.group()), source)


def _depth(source: str, end: int) -> int:
    depth = 0
    for char in source[:end]:
        if char in "({[":
            depth += 1
        elif char in ")}]":
            depth -= 1
    return depth


def _end(source: str, start: int, closing: str) -> int:
    depth = 0
    for index in range(start, len(source)):
        char = source[index]
        if char == closing and depth == 0:
            return index
        if char in "({[":
            depth += 1
        elif char in ")}]":
            depth -= 1
    raise _failure()


def _assignments(source: str, names: set[str]) -> dict[str, str]:
    masked = _masked(source)
    marker = re.compile(r"(?<![\w$.])(?:var\s+|let\s+|const\s+)?(" +
                        "|".join(re.escape(name) for name in names) + r")\s*=(?!=)")
    result: dict[str, str] = {}
    for match in marker.finditer(masked):
        if _depth(masked, match.start()) != 0:
            continue
        prefix = masked[:match.start()].rstrip()
        if prefix and prefix[-1] not in ";}":
            raise _failure("PACS_LAUNCH_EXPRESSION_UNSUPPORTED")
        name = match.group(1)
        expression = source[match.end():_end(masked, match.end(), ";")].strip()
        if name in result:
            raise _failure("PACS_LAUNCH_ASSIGNMENT_AMBIGUOUS")
        if name == "m_szUID":
            trim = re.fullmatch(rf"Trim\(\s*({_LITERAL})\s*,\s*(['\"]),\2\s*\)", expression)
            if trim is None:
                raise _failure("PACS_LAUNCH_UID_EXPRESSION_UNSUPPORTED")
            value = decode_js_string(trim.group(1))
            value = value.strip(",") if value is not None else None
        else:
            value = decode_js_string(expression) if re.fullmatch(_LITERAL, expression) else None
        if value is None:
            raise _failure("PACS_LAUNCH_EXPRESSION_UNSUPPORTED")
        result[name] = value
    return result


def _function(sources: list[str], name: str, parameters: str) -> str:
    found = []
    for source in sources:
        masked = _masked(source)
        pattern = r"\bfunction\s+" + re.escape(name) + r"\s*\(([^()]*)\)\s*\{"
        for match in re.finditer(pattern, masked):
            if _depth(masked, match.start()) != 0:
                continue
            if re.sub(r"\s+", "", match.group(1)) != parameters:
                raise _failure("PACS_LAUNCH_VIEWER_UNSUPPORTED")
            found.append(source[match.end():_end(masked, match.end(), "}")])
    if len(found) != 1:
        raise _failure("PACS_LAUNCH_VIEWER_UNSUPPORTED")
    return found[0]


def parse_pacs_launch_bootstrap(
    html_text: str, *, reference: PacsLaunchRef, expected_login_id: str,
) -> PacsLaunchBootstrap:
    names = {"m_szUserID", "m_szUserIP", "m_szProgID", "m_szHospID", "m_szPatID",
             "m_szAccNo", "m_szDicomServer", "m_szLevel", "m_szUID", "m_szGoCFind",
             "m_szViewerModule", "m_szDownloadApp", "m_szGEZFP"}
    values: dict[str, str] = {}
    for source in _scripts(html_text):
        parsed = _assignments(source, names)
        if values.keys() & parsed.keys():
            raise _failure("PACS_LAUNCH_ASSIGNMENT_AMBIGUOUS")
        values.update(parsed)
    if values.keys() != names:
        raise _failure()
    if values["m_szUserID"] != expected_login_id:
        raise _failure("PACS_LAUNCH_LOGIN_MISMATCH")
    if values["m_szPatID"] != reference.mrn or values["m_szAccNo"] != reference.request_no:
        raise _failure("PACS_LAUNCH_REFERENCE_MISMATCH")
    if not values["m_szUID"]:
        raise _failure("PACS_LAUNCH_UID_MISSING")
    if re.search(r"[,;]", values["m_szUID"]):
        raise _failure("PACS_LAUNCH_MULTIPLE_UIDS_UNSUPPORTED")
    if any(values[key] != expected for key, expected in {
        "m_szProgID": "qryExternalAP", "m_szGoCFind": "true",
        "m_szViewerModule": "../Viewer/vwrIris.jsp",
        "m_szDownloadApp": "false", "m_szGEZFP": "false",
    }.items()):
        raise _failure("PACS_LAUNCH_MODE_UNSUPPORTED")
    try:
        return PacsLaunchBootstrap(reference, values["m_szUserID"], values["m_szUserIP"],
                                   values["m_szUID"], values["m_szDicomServer"],
                                   values["m_szLevel"], values["m_szHospID"])
    except ConfigurationError as exc:
        raise _failure(exc.info.code) from exc


def parse_pacs_launch_context(
    bootstrap: PacsLaunchBootstrap, viewer_html: str,
) -> PacsLaunchContext:
    sources = _scripts(viewer_html)
    show = _function(sources, "showImage", "p_szDicomServer,p_szLevel,p_szUID,p_szRecycle,"
                     "p_szHospID,p_szPatID,p_szAccNo")
    init = _function(sources, "initOcx", "")
    login = _assignments(init, {"szLoginID"})
    if login.get("szLoginID") != bootstrap.login_id:
        raise _failure("PACS_LAUNCH_LOGIN_MISMATCH")
    # Only the SK map entry directly in the known showImage function is read.
    masked = _masked(show)
    declarations = list(re.finditer(r"\baWadoURL\s*=(?!=)", masked))
    if len(declarations) != 1:
        raise _failure("PACS_LAUNCH_WADO_UNSUPPORTED")
    declaration = declarations[0]
    expression = show[declaration.end():_end(masked, declaration.end(), ";")].strip()
    if _depth(masked, declaration.start()) != 0 or not re.fullmatch(r"new\s+Object\(\s*\)", expression):
        raise _failure("PACS_LAUNCH_WADO_UNSUPPORTED")
    candidates = []
    pattern = r"\baWadoURL\s*\[\s*([^\]]+)\s*\]\s*(\+=|=(?!=))"
    for match in re.finditer(pattern, show):
        if not masked[match.start():].startswith("aWadoURL"):
            continue
        key_expression = match.group(1).strip()
        key = decode_js_string(key_expression) if re.fullmatch(_LITERAL, key_expression) else None
        if key is None:
            raise _failure("PACS_LAUNCH_WADO_UNSUPPORTED")
        if key != "SK":
            continue
        prefix = masked[:match.start()].rstrip()
        if _depth(masked, match.start()) != 0 or (prefix and prefix[-1] not in ";}"):
            raise _failure("PACS_LAUNCH_WADO_UNSUPPORTED")
        expression = show[match.end():_end(masked, match.end(), ";")].strip()
        if match.group(2) != "=" or not re.fullmatch(_LITERAL, expression):
            raise _failure("PACS_LAUNCH_WADO_UNSUPPORTED")
        candidates.append(decode_js_string(expression))
    if len(candidates) != 1 or not candidates[0]:
        raise _failure("PACS_LAUNCH_WADO_MISSING")
    blocks = list(re.finditer(r"\bif\s*\(\s*aUIDList\s*!=\s*null\s*\)\s*\{", masked))
    blocks = [match for match in blocks if _depth(masked, match.start()) == 0]
    if len(blocks) != 1:
        raise _failure("PACS_LAUNCH_VIEWER_UNSUPPORTED")
    block = blocks[0]
    user_url = _assignments(show[block.end():_end(masked, block.end(), "}")], {"szUserUrl"})
    try:
        pairs = parse_qsl(user_url.get("szUserUrl", "").removeprefix("&"),
                          keep_blank_values=True, strict_parsing=True, max_num_fields=4)
    except ValueError:
        raise _failure("PACS_LAUNCH_VIEWER_BINDING_MISMATCH") from None
    if len(pairs) != 4 or dict(pairs) != {
        "PID": "vwrIris", "UserID": bootstrap.login_id,
        "ClientIP": bootstrap.client_ip, "NotifyResult": "false",
    }:
        raise _failure("PACS_LAUNCH_VIEWER_BINDING_MISMATCH")
    try:
        return PacsLaunchContext(
            bootstrap.reference, bootstrap.login_id, bootstrap.client_ip, bootstrap.study_uid,
            bootstrap.dicom_server, bootstrap.level, bootstrap.hospital_id,
            SMARTWONDER_BASE_URL, candidates[0] + "&",
        )
    except ConfigurationError as exc:
        raise _failure(exc.info.code) from exc
