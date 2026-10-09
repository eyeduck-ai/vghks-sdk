"""Static PSPDPortal parsing. No JavaScript execution or network access."""

from __future__ import annotations

import re
from datetime import date, datetime
from urllib.parse import parse_qsl, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from ..core.errors import ParseError
from ..models.attendance import (
    ATTENDANCE_MODE_VALUES,
    AttendanceHistory,
    AttendanceMode,
    AttendancePunchReceipt,
    AttendanceQuery,
    AttendanceRecord,
    AttendanceState,
)

_PATH = "/PSPDPortal/oFSchedule.do"
_STAMP = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}"
_CODE = r"[A-Za-z0-9_-]{1,64}"
_MODE_BY_VALUE: dict[str, AttendanceMode] = {
    value: key for key, value in ATTENDANCE_MODE_VALUES.items()
}


def _fail(code: str) -> None:
    raise ParseError("attendance response did not match its recorded structure", code=code)


def _text(node: Tag) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def _soup(text: str) -> BeautifulSoup:
    soup = BeautifulSoup(text, "html.parser")
    for node in list(soup.select("script,style,[hidden]")):
        node.decompose()
    for node in list(
        soup.find_all(style=re.compile(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", re.I))
    ):
        if node.parent is not None:
            node.decompose()
    return soup


def _form(soup: BeautifulSoup, action: str) -> Tag:
    forms = soup.find_all("form", attrs={"name": "pSPDForm"})
    if len(forms) != 1:
        _fail("ATTENDANCE_FORM_MISSING")
    form = forms[0]
    req = form.find_all("input", attrs={"name": "reqCode"})
    if (
        str(form.get("method", "")).lower() != "post"
        or form.get("action") != _PATH
        or len(req) != 1
        or req[0].get("value") != action
    ):
        _fail("ATTENDANCE_FORM_INVALID")
    return form


def _datetime(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise ParseError(
            "attendance timestamp was invalid", code="ATTENDANCE_TIMESTAMP_INVALID"
        ) from exc


def parse_attendance_state(
    text: str, *, expected_employee_id: str | None = None
) -> AttendanceState:
    soup = _soup(text)
    form = _form(soup, "setPCClockInLog")
    if [x.get("name") for x in form.find_all("input") if x.get("name")] != ["reqCode"]:
        _fail("ATTENDANCE_PUNCH_FIELDS_UNRECOGNIZED")
    buttons = form.find_all("input", attrs={"type": "submit"})
    if len(buttons) != 1 or buttons[0].has_attr("disabled"):
        _fail("ATTENDANCE_PUNCH_FORM_UNAVAILABLE")
    legends = form.find_all("legend")
    if len(legends) != 1:
        _fail("ATTENDANCE_IDENTITY_MISSING")
    subject = re.fullmatch(r"\[\s*(\d+-\S+)\s+(.+?)\((" + _CODE + r")\)\s*\]", _text(legends[0]))
    if subject is None:
        _fail("ATTENDANCE_IDENTITY_UNRECOGNIZED")
    unit, name, employee_id = subject.groups()
    if (
        expected_employee_id is not None
        and employee_id.casefold() != expected_employee_id.strip().casefold()
    ):
        _fail("ATTENDANCE_ACCOUNT_MISMATCH")
    visible = _text(form)
    last = re.search(r"上次電腦簽到退記錄\s*:\s*(.*?)\s*電腦序號\s*:\s*(" + _CODE + r")\b", visible)
    if last is None:
        _fail("ATTENDANCE_STATUS_UNRECOGNIZED")
    previous, serial = last.groups()
    event = re.fullmatch(r"(" + _STAMP + r")\s+地點\s*:\s*(" + _CODE + r")", previous)
    if event is None and previous not in {"", "地點:", "尚無紀錄", "尚無紀錄 地點:"}:
        _fail("ATTENDANCE_STATUS_UNRECOGNIZED")
    return AttendanceState(
        employee_id,
        name,
        unit,
        serial,
        _datetime(event.group(1)) if event else None,
        event.group(2) if event else None,
    )


def _date_input(form: Tag, name: str) -> date | None:
    nodes = form.find_all("input", attrs={"name": name})
    if len(nodes) != 1:
        _fail("ATTENDANCE_QUERY_FORM_INVALID")
    value = str(nodes[0].get("value", ""))
    if not value:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        _fail("ATTENDANCE_QUERY_DATE_INVALID")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ParseError(
            "attendance query date was invalid", code="ATTENDANCE_QUERY_DATE_INVALID"
        ) from exc


def parse_attendance_history(
    text: str,
    *,
    query: AttendanceQuery | None = None,
    expected_state: AttendanceState | None = None,
) -> AttendanceHistory:
    soup = _soup(text)
    form = _form(soup, "getProcessedFingerLog")
    names = [x.get("name") for x in form.find_all("input") if x.get("name")]
    if sorted(names) != sorted(
        ["reqCode", "value(begDate)", "value(endDate)", "b1", "value(qryType)", "value(qryType)"]
    ):
        _fail("ATTENDANCE_QUERY_FORM_INVALID")
    radios = form.find_all("input", attrs={"name": "value(qryType)"})
    if {x.get("value") for x in radios} != set(_MODE_BY_VALUE):
        _fail("ATTENDANCE_QUERY_FORM_INVALID")
    selected = [x for x in radios if x.has_attr("checked")]
    if len(selected) > 1:
        _fail("ATTENDANCE_QUERY_FORM_INVALID")
    mode = _MODE_BY_VALUE[str(selected[0]["value"])] if selected else None
    start, end = _date_input(form, "value(begDate)"), _date_input(form, "value(endDate)")
    if (start is None) != (end is None) or (start is not None and end is not None and start > end):
        _fail("ATTENDANCE_QUERY_DATE_INVALID")
    if query is not None:
        if (start, end) != (query.start, query.end):
            _fail("ATTENDANCE_QUERY_PERIOD_MISMATCH")
        if mode is not None and mode != query.mode:
            _fail("ATTENDANCE_QUERY_MODE_MISMATCH")
    subjects = soup.find_all("span", id="spanUsrName")
    if len(subjects) != 1 or not _text(subjects[0]):
        _fail("ATTENDANCE_IDENTITY_MISSING")
    name = _text(subjects[0])
    printed = soup.find_all("span", id="empname")
    if any(_text(node) != name for node in printed):
        _fail("ATTENDANCE_IDENTITY_CONFLICT")
    if expected_state is not None and name != expected_state.employee_name:
        _fail("ATTENDANCE_ACCOUNT_MISMATCH")
    tables = soup.find_all("table", id="drlistTb")
    if len(tables) != 1:
        _fail("ATTENDANCE_RECORDS_TABLE_MISSING")
    table = tables[0]
    rows = [row for row in table.find_all("tr") if row.find_parent("table") is table]
    headers = [_text(x) for row in rows for x in row.find_all("th", recursive=False)]
    if headers != ["日期時間", "說明"]:
        _fail("ATTENDANCE_RECORDS_HEADERS_UNRECOGNIZED")
    result: list[AttendanceRecord] = []
    total: int | None = None
    for row in rows:
        cells = row.find_all("td", recursive=False)
        if not cells:
            continue
        if row.get("id") == "tfoot":
            count = re.fullmatch(
                r"查詢結果\s*[:\uff1a]\s*共有\s*~?\s*(\d+)\s*~?\s*筆資料", _text(row)
            )
            if len(cells) != 1 or count is None or total is not None:
                _fail("ATTENDANCE_RECORD_COUNT_INVALID")
            total = int(count.group(1))
            continue
        if len(cells) != 2 or not re.fullmatch(_STAMP, _text(cells[0])):
            _fail("ATTENDANCE_RECORD_ROW_UNRECOGNIZED")
        description = _text(cells[1])
        location = re.fullmatch(r"電腦\s*:\s*(" + _CODE + r")", description)
        result.append(
            AttendanceRecord(
                _datetime(_text(cells[0])), description, location.group(1) if location else None
            )
        )
    if total is None or total != len(result):
        _fail("ATTENDANCE_RECORD_COUNT_MISMATCH")
    return AttendanceHistory(
        name,
        tuple(result),
        total,
        query,
        start,
        end,
        mode,
        expected_state.employee_id if expected_state else None,
    )


def parse_attendance_punch(
    text: str, *, expected_state: AttendanceState | None = None
) -> AttendancePunchReceipt:
    soup = _soup(text)
    acknowledgments = [
        m
        for node in soup.find_all("li")
        if (
            m := re.fullmatch(
                r"簽到退成功\s*:\s*(" + _STAMP + r")\s+地點\s*:\s*(" + _CODE + r")", _text(node)
            )
        )
    ]
    if len(acknowledgments) != 1:
        _fail("ATTENDANCE_PUNCH_ACK_MISSING")
    ack = acknowledgments[0]
    history = parse_attendance_history(text, expected_state=expected_state)
    stamp, location = _datetime(ack.group(1)), ack.group(2)
    matches = [
        row for row in history.records if row.occurred_at == stamp and row.location_code == location
    ]
    if not matches:
        _fail("ATTENDANCE_PUNCH_ACK_RECORD_MISMATCH")
    return AttendancePunchReceipt(matches[-1], history)


def parse_attendance_wait_target(text: str, *, response_url: str, base_url: str) -> str:
    """Accept only the recorded literal body onload and fixed read destination."""
    soup = BeautifulSoup(text, "html.parser")
    body = soup.find("body")
    match = re.fullmatch(
        r"\s*window\.location\.replace\(\s*(['\"])([^'\"\\]+)\1\s*\)\s*;?\s*",
        str(body.get("onload", "")) if body else "",
    )
    if match is None:
        _fail("ATTENDANCE_WAIT_TARGET_INVALID")
    target = urljoin(response_url, match.group(2))
    configured, actual = urlsplit(base_url), urlsplit(target)
    if (
        actual.scheme != configured.scheme
        or actual.netloc.casefold() != configured.netloc.casefold()
        or actual.path != _PATH
        or actual.fragment
        or parse_qsl(actual.query, keep_blank_values=True) != [("reqCode", "getPCClockInLog")]
    ):
        _fail("ATTENDANCE_WAIT_TARGET_INVALID")
    return target
