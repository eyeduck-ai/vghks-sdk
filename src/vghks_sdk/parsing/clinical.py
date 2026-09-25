"""Pure parsers for PRQ clinical histories, order navigation, and assets."""

from __future__ import annotations

import html
import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup, Tag

from ..core.errors import ConfigurationError, ParseError
from ..core.jsliteral import (
    decode_js_string,
    evaluate_expression,
    evaluated_string_assignments,
    extract_quoted_strings,
    iter_active_constructor_calls,
    static_document_writes,
    strip_js_comments,
)
from ..models import (
    ClinicalOrder,
    ConsultRecord,
    MedicationOrder,
    NumericHistoryReport,
    NumericTable,
    OrderDetail,
    OrderDetailRef,
    OrderReport,
    OrderReportRef,
    PacsImageRef,
    PacsStudy,
    PacsStudyRef,
    PatientSurgeryRecord,
    PdfAttachmentRef,
    TreatmentRecord,
    VisitCase,
)
from .common import normalize_inline_text, strip_markup

_ORDER_VARIABLES = {"orderStr", "qrcodeStr", "mydate", "rcpDt", "orspDept"}
_MEDICATION_VARIABLES = {"rtNameStr", "freqnStr", "argfileStr"}
_INTERNAL_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9:/\\])/PRQWeb/"
    r"(?:QueryOrderDetail|QueryReportByOrder|Adm_QueryPACS)\.do\?[^'\"<>\s\\]+",
    re.IGNORECASE,
)


def parse_clinical_orders(
    html_text: str,
    *,
    mrn: str,
    case: VisitCase | None = None,
) -> list[ClinicalOrder]:
    soup = BeautifulSoup(html_text, "html.parser")
    output: list[ClinicalOrder] = []
    seen: set[tuple[str, ...]] = set()
    for script in soup.find_all("script"):
        if script.get("src"):
            continue
        source = script.string if script.string is not None else script.get_text()
        if "new KSCase" not in source:
            continue
        variables = evaluated_string_assignments(source, _ORDER_VARIABLES)
        refs = _order_refs_from_source(source, expected_mrn=mrn)
        detail_ref = next((item for item in refs if isinstance(item, OrderDetailRef)), None)
        report_ref = next((item for item in refs if isinstance(item, OrderReportRef)), None)
        pacs_ref = next(iter(_pacs_refs_from_source(source, expected_mrn=mrn)), None)
        for call in iter_active_constructor_calls(source, "KSCase", variables):
            if len(call.arguments) < 6:
                continue
            values = tuple(evaluate_expression(argument, variables) for argument in call.arguments)
            if any(value is None for value in values[:6]):
                raise ParseError(
                    "order constructor contained an unsupported expression",
                    code="PRQ_ORDER_EXPRESSION_UNSUPPORTED",
                )
            navigation_ref = detail_ref or report_ref
            resolved_case_no = (
                navigation_ref.case_no
                if navigation_ref is not None
                else case.case_no
                if case
                else ""
            )
            resolved_case_type = (
                navigation_ref.case_type
                if navigation_ref is not None
                else case.case_type
                if case
                else "O"
            )
            order = ClinicalOrder(
                mrn=mrn,
                case_no=resolved_case_no,
                case_type=resolved_case_type,
                name=strip_markup(values[1] or ""),
                order_date=normalize_inline_text(values[2]),
                execution_date=normalize_inline_text(values[3]),
                requester=strip_markup(values[4] or ""),
                status=strip_markup(values[5] or ""),
                attachment=strip_markup(values[6] or "") if len(values) > 6 else "",
                detail_ref=detail_ref,
                report_ref=report_ref,
                pacs_ref=pacs_ref,
                pdf_refs=_pdf_refs_from_source(source, expected_mrn=mrn),
            )
            if not order.name or order.identity in seen:
                continue
            seen.add(order.identity)
            output.append(order)
    return sorted(output, key=_order_sort_key, reverse=True)


def parse_medication_orders(
    html_text: str,
    *,
    mrn: str,
    case: VisitCase | None = None,
) -> list[MedicationOrder]:
    soup = BeautifulSoup(html_text, "html.parser")
    output: list[MedicationOrder] = []
    seen: set[tuple[str, ...]] = set()
    variables: dict[str, str] = {}
    for script in soup.find_all("script"):
        if script.get("src"):
            continue
        source = script.string if script.string is not None else script.get_text()
        variables = evaluated_string_assignments(
            source,
            _MEDICATION_VARIABLES,
            # The captured page appends a conditional TCM hyperlink to the
            # medicine label.  Its URL is presentation-only and is outside the
            # supported attachment protocols, so keep it inert while parsing
            # the allow-listed visible fields.
            initial={**variables, "tcmUrl": ""},
        )
        if "new KSCase" not in source:
            continue
        for call in iter_active_constructor_calls(source, "KSCase", variables):
            if len(call.arguments) < 9:
                continue
            values = tuple(evaluate_expression(argument, variables) for argument in call.arguments)
            if any(value is None for value in values[:9]):
                raise ParseError(
                    "medication constructor contained an unsupported expression",
                    code="PRQ_MEDICATION_EXPRESSION_UNSUPPORTED",
                )
            medication = MedicationOrder(
                mrn=mrn,
                case_no=case.case_no if case else _attribute_value(source, "hcaseno"),
                case_type=case.case_type if case else _attribute_value(source, "hcasetyp") or "O",
                name=strip_markup(values[1] or ""),
                route=strip_markup(values[2] or ""),
                dose=strip_markup(values[3] or ""),
                unit=strip_markup(values[4] or ""),
                frequency=strip_markup(values[5] or ""),
                start_date=normalize_inline_text(values[6]),
                end_date=normalize_inline_text(values[7]),
                status=strip_markup(values[8] or ""),
                prescriber=strip_markup(values[9] or "") if len(values) > 9 else "",
                attachment=strip_markup(values[10] or "") if len(values) > 10 else "",
            )
            if not medication.name or medication.identity in seen:
                continue
            seen.add(medication.identity)
            output.append(medication)
    return sorted(output, key=_medication_sort_key, reverse=True)


def parse_numeric_tables(html_text: str) -> tuple[NumericTable, ...]:
    soup = BeautifulSoup(html_text, "html.parser")
    for script in tuple(soup.find_all("script")):
        source = script.string if script.string is not None else script.get_text()
        if "document.write" not in source:
            continue
        narrative = _numeric_narrative(source)
        if narrative is not None:
            script.replace_with(BeautifulSoup(narrative, "html.parser"))
            continue
        rendered = static_document_writes(
            source,
            # Captured legacy eye tables concatenate these two variables only
            # into presentation attributes.  Keeping them empty preserves the
            # static cell content without interpreting page state or CSS.
            variables={"normalStr": "", "tmpRangeVal": ""},
            allow_unknown_branches=True,
        )
        script.replace_with(BeautifulSoup(rendered, "html.parser"))

    tables: list[NumericTable] = []
    seen: set[tuple[object, ...]] = set()
    for table in soup.find_all("table"):
        table_id = normalize_inline_text(table.get("id"))
        classes = {str(item) for item in table.get("class", ())}
        if "eTable" not in classes and not table_id.casefold().startswith("resnumtable"):
            continue
        parsed = _numeric_table(table)
        if parsed is None:
            continue
        identity = (parsed.title, parsed.header_rows, parsed.rows)
        if identity not in seen:
            seen.add(identity)
            tables.append(parsed)
    return tuple(tables)


def _numeric_narrative(source: str) -> str | None:
    """Recognize the recorded literal narrative/newline template, without JS.

    These free-text blocks sit beside the numeric tables. Preserve their text
    in the document while parsing the tables; never evaluate arbitrary loops,
    expressions or eval calls from a returned page.
    """
    literal = r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')"""
    match = re.fullmatch(
        rf"\s*var\s+resVal\s*=\s*(?P<value>{literal})\s*;\s*"
        r'while\s*\(\s*resVal\.indexOf\("\\n"\)\s*>\s*0\s*\)\s*\{\s*'
        r'resVal\s*=\s*resVal\.replace\("\\n",\s*"<br/>"\)\s*;\s*\}\s*'
        r'document\.write\("<pre>"\s*\+\s*resVal\s*\+\s*"</pre>"\)\s*;\s*',
        strip_js_comments(source),
        re.DOTALL,
    )
    if match is None:
        return None
    value = decode_js_string(match.group("value"))
    return f"<pre>{html.escape(value)}</pre>" if value is not None else None


def parse_numeric_history(html_text: str, *, mrn: str) -> NumericHistoryReport:
    return NumericHistoryReport(mrn=mrn, tables=parse_numeric_tables(html_text))


def parse_surgery_history(html_text: str, *, mrn: str) -> list[PatientSurgeryRecord]:
    soup = BeautifulSoup(html_text, "html.parser")
    table = next((item for item in soup.find_all("table") if len(item.find_all("th")) >= 8), None)
    if table is None:
        raise ParseError(
            "surgery history did not contain its expected table",
            code="PRQ_SURGERY_HISTORY_STRUCTURE_MISSING",
        )
    rows = table.find_all("tr")
    output: list[PatientSurgeryRecord] = []
    for row in rows[1:]:
        cells = row.find_all("td", recursive=False)
        if len(cells) < 2:
            continue
        values = [normalize_inline_text(cell.get_text(" ", strip=True)) for cell in cells]
        if not any(values):
            continue
        flags = tuple(_cell_has_record(cell) for cell in cells[2:8])
        flags = flags + (False,) * (6 - len(flags))
        refs, issues = _surgery_pdf_refs(cells[2], expected_mrn=mrn) if len(cells) > 2 else ((), ())
        output.append(
            PatientSurgeryRecord(
                mrn=mrn,
                surgery_date=values[0],
                procedure=values[1],
                surgery_record_available=flags[0],
                anesthesia_record_available=flags[1],
                anesthesia_consent_available=flags[2],
                preoperative_record_available=flags[3],
                postoperative_record_available=flags[4],
                gamma_knife_record_available=flags[5],
                surgery_record_refs=refs,
                surgery_record_issues=issues,
            )
        )
    return sorted(output, key=lambda item: _date_key(item.surgery_date), reverse=True)


def _surgery_pdf_refs(
    cell: Tag, *, expected_mrn: str
) -> tuple[tuple[PdfAttachmentRef, ...], tuple[str, ...]]:
    """Read recorded button literals without executing popup JavaScript.

    Only the operation-note column is used; anesthesia documents have a
    different navigation flow. One unsupported button must not hide others.
    """
    refs: list[PdfAttachmentRef] = []
    issues: list[str] = []
    for button in cell.find_all(["input", "button", "a"]):
        if button.has_attr("disabled"):
            continue
        candidates: list[str] = []
        onclick = button.get("onclick", "")
        href = button.get("href", "")
        try:
            if onclick:
                candidates.extend(_pdf_candidates(strip_js_comments(str(onclick))))
            if href:
                parsed = urlsplit(str(href))
                if parsed.path == "/PRQWeb/Page/JSP/showPDF.jsp":
                    value = _last_query_values(parsed.query).get("fileName")
                    if value:
                        candidates.append(value)
        except ValueError:
            issues.append("SURGERY_PDF_BUTTON_UNSUPPORTED")
            continue
        if not candidates:
            issues.append("SURGERY_PDF_BUTTON_UNSUPPORTED")
        for candidate in candidates:
            try:
                ref = PdfAttachmentRef(expected_mrn, candidate)
            except ConfigurationError as exc:
                issues.append(exc.info.code)
            else:
                if ref not in refs:
                    refs.append(ref)
    if _cell_has_record(cell) and not refs and not issues:
        issues.append("SURGERY_PDF_BUTTON_UNSUPPORTED")
    return tuple(refs), tuple(dict.fromkeys(issues))


def parse_consults(html_text: str, *, case: VisitCase) -> list[ConsultRecord]:
    _assert_supported_empty_container(
        html_text,
        root_id="clorderList",
        empty_id="orwordsDiv",
        code="PRQ_CONSULT_NONEMPTY_UNSUPPORTED",
    )
    return []


def parse_treatments(html_text: str, *, case: VisitCase) -> list[TreatmentRecord]:
    soup = BeautifulSoup(html_text, "html.parser")
    root = soup.find(id="orderList")
    if root is None:
        raise ParseError(
            "treatment list container was missing", code="PRQ_TREATMENT_STRUCTURE_MISSING"
        )
    if not root.find("table"):
        _assert_supported_empty_container(
            html_text,
            root_id="orderList",
            empty_id="agrfileDiv",
            code="PRQ_TREATMENT_NONEMPTY_UNSUPPORTED",
        )
        return []
    expected = (
        "常規 治療項目",
        "頻次",
        "開立時間",
        "開立者",
        "停止時間",
        "現況",
        "說明",
        "PDA",
        "附檔",
    )
    output = []
    for table in root.find_all("table"):
        headers = tuple(
            normalize_inline_text(th.get_text(" ", strip=True)) for th in table.find_all("th")
        )
        if headers != expected:
            raise ParseError(
                "treatment table headers changed", code="PRQ_TREATMENT_STRUCTURE_UNSUPPORTED"
            )
        for row in table.find_all("tr"):
            cells = row.find_all("td", recursive=False)
            if not cells:
                continue
            if len(cells) != len(expected):
                raise ParseError("treatment row width changed", code="PRQ_TREATMENT_ROW_INVALID")
            values = [normalize_inline_text(cell.get_text(" ", strip=True)) for cell in cells]
            if any(values):
                output.append(TreatmentRecord(case=case, fields=dict(zip(expected, values))))
    return output


def parse_order_detail(html_text: str, *, reference: OrderDetailRef) -> OrderDetail:
    soup = BeautifulSoup(html_text, "html.parser")
    fields = _labelled_fields(soup)
    if not fields:
        raise ParseError(
            "order detail did not contain labelled fields",
            code="PRQ_ORDER_DETAIL_STRUCTURE_MISSING",
        )
    refs = _order_refs_from_source(html_text, expected_mrn=reference.mrn)
    return OrderDetail(
        reference=reference,
        fields=fields,
        report_refs=tuple(item for item in refs if isinstance(item, OrderReportRef)),
        pacs_refs=_pacs_refs_from_source(html_text, expected_mrn=reference.mrn),
        pdf_refs=_pdf_refs_from_source(html_text, expected_mrn=reference.mrn),
    )


def parse_order_report(html_text: str, *, reference: OrderReportRef) -> OrderReport:
    from .report_data import extract_report_text

    soup = BeautifulSoup(html_text, "html.parser")
    fields = _labelled_fields(soup)
    report_text, extraction_notes = extract_report_text(soup)
    pdf_refs = _pdf_refs_from_source(html_text, expected_mrn=reference.mrn)
    pacs_refs = _pacs_refs_from_source(html_text, expected_mrn=reference.mrn)
    if not fields and not report_text and not pdf_refs and not pacs_refs:
        # The server explicitly reports no data outside an empty rptDiv/data
        # container. Require its current-patient marker, not a generic blank page.
        container = soup.find(id="rptDiv") or soup.find(id="data")
        marker = f"{reference.mrn}該病患本次查詢無報告資料，請確認"  # noqa: RUF001
        if (
            container is not None
            and not container.get_text(strip=True)
            and marker in soup.get_text(" ", strip=True)
            and not container.find(["iframe", "object", "embed", "img", "a"])
        ):
            return OrderReport(reference=reference, fields={}, report_data_status="EMPTY")
        raise ParseError(
            "order report did not contain labelled fields",
            code="PRQ_ORDER_REPORT_STRUCTURE_MISSING",
        )
    return OrderReport(
        reference=reference,
        fields=fields,
        pdf_refs=tuple(pdf_refs),
        pacs_refs=pacs_refs,
        report_text=report_text,
        report_data_status=(
            "TEXT_AVAILABLE"
            if report_text
            else "ATTACHMENT_ONLY"
            if pdf_refs or pacs_refs
            else "METADATA_ONLY"
        ),
        text_extraction_notes=extraction_notes,
    )


def parse_pacs_study(html_text: str, *, reference: PacsStudyRef) -> PacsStudy:
    soup = BeautifulSoup(html_text, "html.parser")
    images: list[PacsImageRef] = []
    seen: set[tuple[str, str, str]] = set()
    for image in soup.find_all("img", src=True):
        parsed = urlsplit(html.unescape(str(image.get("src"))))
        is_pacs_image = "pacsJpg" in {str(item) for item in image.get("class", ())}
        if parsed.scheme or parsed.netloc or ".." in parsed.path.split("/"):
            if is_pacs_image:
                raise ParseError(
                    "external PACS image reference was rejected", code="PACS_REF_EXTERNAL"
                )
            continue
        if parsed.path != "/PRQWeb/Page/JSP/showPACSPic.jsp":
            if is_pacs_image:
                raise ParseError(
                    "unknown PACS image path was rejected", code="PACS_REF_PATH_UNKNOWN"
                )
            continue
        query = _last_query_values(parsed.query)
        if query.get("hhisnum", reference.mrn).strip() != reference.mrn:
            raise ParseError("PACS image MRN did not match its study", code="PACS_REF_MRN_MISMATCH")
        if query.get("reqno", reference.request_no).strip() != reference.request_no:
            raise ParseError(
                "PACS image request did not match its study", code="PACS_REF_REQUEST_MISMATCH"
            )
        series_uid = query.get("SERIES_UID", "")
        study_uid = query.get("STUDY_UID", "")
        uid = query.get("uid", "")
        if not uid:
            raise ParseError("PACS image reference was incomplete", code="PACS_REF_INVALID")
        identity = (series_uid, study_uid, uid)
        if identity in seen:
            continue
        seen.add(identity)
        images.append(
            PacsImageRef(
                mrn=reference.mrn,
                request_no=reference.request_no,
                series_uid=series_uid,
                study_uid=study_uid,
                uid=uid,
            )
        )
    if images:
        return PacsStudy(reference, tuple(images), data_status="IMAGES_AVAILABLE")
    # A JPG button opens a viewer, not necessarily an image. Only an explicit
    # visible empty result qualifies; blank/login/changed layouts are errors.
    content = soup.find(id="pacsContent") or soup
    visible = BeautifulSoup(str(content), "html.parser")
    for node in visible.find_all(["script", "style"]):
        node.decompose()
    text = re.sub(r"\s+", "", visible.get_text())
    if re.fullmatch(r"查無資料[!\uff01。.]?", text):
        return PacsStudy(reference, (), data_status="EMPTY", empty_reason="NO_IMAGES")
    raise ParseError(
        "PACS page contained neither image references nor an explicit empty result",
        code="PACS_STRUCTURE_UNRECOGNIZED",
    )


def _pdf_refs_from_source(source: str, *, expected_mrn: str) -> tuple[PdfAttachmentRef, ...]:
    output: list[PdfAttachmentRef] = []
    for candidate in _pdf_candidates(source):
        try:
            item = PdfAttachmentRef(expected_mrn, candidate)
        except ConfigurationError as exc:
            raise ParseError("order contained an unsafe PDF reference", code=exc.info.code) from exc
        if item not in output:
            output.append(item)
    return tuple(output)


def _order_refs_from_source(
    source: str,
    *,
    expected_mrn: str,
) -> tuple[OrderDetailRef | OrderReportRef, ...]:
    decoded_source = html.unescape(source.replace('\\"', '"').replace("\\'", "'"))
    output: list[OrderDetailRef | OrderReportRef] = []
    seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    for match in _INTERNAL_PATH_RE.finditer(decoded_source):
        parsed = urlsplit(match.group(0))
        if not parsed.path.casefold().endswith(("queryorderdetail.do", "queryreportbyorder.do")):
            continue
        query = _last_query_values(parsed.query)
        if query.get("hhisnum", "").strip() != expected_mrn:
            raise ParseError("order navigation MRN did not match", code="ORDER_REF_MRN_MISMATCH")
        identity = (parsed.path, tuple(sorted(query.items())))
        if identity in seen:
            continue
        seen.add(identity)
        try:
            if parsed.path.casefold().endswith("queryorderdetail.do"):
                output.append(
                    OrderDetailRef(
                        mrn=expected_mrn,
                        case_no=query.get("caseNo", ""),
                        case_type=query.get("caseType", ""),
                        sequence_no=query.get("seqNo", ""),
                        result_type=query.get("orRsType", ""),
                        order_step=query.get("orStep", ""),
                        source=query.get("source", ""),
                    )
                )
            elif parsed.path.casefold().endswith("queryreportbyorder.do"):
                output.append(
                    OrderReportRef(
                        mrn=expected_mrn,
                        case_no=query.get("caseNo", ""),
                        case_type=query.get("caseType", ""),
                        sequence_no=query.get("seqNo", ""),
                        department=query.get("orDept", ""),
                        result_type=query.get("orRsType", ""),
                        fee_code=query.get("orpfcode", ""),
                        source=query.get("source", ""),
                    )
                )
        except ConfigurationError as exc:
            raise ParseError("order navigation reference was unsafe", code=exc.info.code) from exc
    return tuple(output)


def _pacs_refs_from_source(source: str, *, expected_mrn: str) -> tuple[PacsStudyRef, ...]:
    output: list[PacsStudyRef] = []
    seen: set[str] = set()
    decoded_source = html.unescape(source.replace('\\"', '"').replace("\\'", "'"))
    soup = BeautifulSoup(decoded_source, "html.parser")
    for node in soup.find_all(attrs={"reqno": True}):
        request_no = normalize_inline_text(node.get("reqno"))
        node_mrn = normalize_inline_text(node.get("hhisnum"))
        if node_mrn and node_mrn != expected_mrn:
            raise ParseError("PACS study MRN did not match", code="PACS_REF_MRN_MISMATCH")
        if request_no and request_no not in seen:
            seen.add(request_no)
            output.append(PacsStudyRef(expected_mrn, request_no))
    for match in _INTERNAL_PATH_RE.finditer(decoded_source):
        parsed = urlsplit(match.group(0))
        if not parsed.path.casefold().endswith("adm_querypacs.do"):
            continue
        query = _last_query_values(parsed.query)
        referenced_mrn = query.get("hhisnum", "").strip()
        if referenced_mrn and referenced_mrn != expected_mrn:
            raise ParseError("PACS study MRN did not match", code="PACS_REF_MRN_MISMATCH")
        request_no = query.get("reqno", "").strip()
        if request_no and request_no not in seen:
            seen.add(request_no)
            output.append(PacsStudyRef(expected_mrn, request_no))
    return tuple(output)


def _pdf_candidates(source: str) -> Iterable[str]:
    soup = BeautifulSoup(html.unescape(source), "html.parser")
    sources = [
        script.string if script.string is not None else script.get_text()
        for script in soup.find_all("script")
    ]
    sources.append(html.unescape(source))
    for script_source in sources:
        for value in extract_quoted_strings(script_source):
            candidate = value.strip()
            parsed = urlsplit(candidate)
            if parsed.path.casefold().endswith("showpdf.jsp"):
                query = _last_query_values(parsed.query)
                if query.get("fileName"):
                    yield query["fileName"]
            elif (
                candidate.casefold().endswith(".pdf")
                and not candidate.lstrip().startswith("<")
                and "=" not in candidate
            ):
                yield candidate


def _numeric_table(table: Tag) -> NumericTable | None:
    rows = [row for row in table.find_all("tr") if row.find_parent("table") is table]
    headers = tuple(
        normalize_inline_text(cell.get_text(" ", strip=True))
        for row in rows
        for cell in row.find_all("th", recursive=False)
        if normalize_inline_text(cell.get_text(" ", strip=True))
    )
    values: list[tuple[str, ...]] = []
    header_cells: list[list[Tag]] = []
    data_started = False
    for row in rows:
        cells = row.find_all("td", recursive=False)
        if not cells:
            if not data_started:
                heading_cells = row.find_all("th", recursive=False)
                if heading_cells:
                    header_cells.append(heading_cells)
            continue
        data_started = True
        parsed = tuple(normalize_inline_text(cell.get_text(" ", strip=True)) for cell in cells)
        if any(parsed):
            values.append(parsed)
    if not headers and not values:
        return None
    title_node = table.find(class_="mTitle") or table.find("caption")
    title = normalize_inline_text(title_node.get_text(" ", strip=True)) if title_node else ""
    if not title:
        previous = table.find_previous(["h1", "h2", "h3", "h4", "strong"])
        title = normalize_inline_text(previous.get_text(" ", strip=True)) if previous else ""
    if not title:
        title = headers[1] if len(headers) > 1 else headers[0] if headers else ""
    header_rows = tuple(
        tuple(normalize_inline_text(cell.get_text(" ", strip=True)) for cell in row)
        for row in header_cells
    )
    widths = {len(row) for row in values}
    issues: list[str] = []
    if len(widths) > 1:
        issues.append("NUMERIC_ROW_WIDTH_MISMATCH")
    column_paths: tuple[tuple[str, ...], ...] = ()
    if values:
        column_paths, header_issues = _numeric_column_paths(header_cells, max(widths))
        issues.extend(header_issues)
        if len(widths) > 1:
            column_paths = ()
    return NumericTable(
        title=title,
        headers=headers,
        rows=tuple(values),
        header_rows=header_rows,
        column_paths=column_paths,
        parsing_issues=tuple(dict.fromkeys(issues)),
    )


def _numeric_column_paths(
    header_rows: list[list[Tag]], width: int
) -> tuple[tuple[tuple[str, ...], ...], tuple[str, ...]]:
    if not header_rows:
        return (), ("NUMERIC_HEADER_MISSING",)
    grid: list[list[str | None]] = [[None] * width for _ in header_rows]
    issues: list[str] = []

    def span(cell: Tag, key: str) -> int:
        raw = cell.get(key, 1)
        try:
            value = int(str(raw))
        except ValueError:
            value = 0
        if value < 1:
            issues.append("NUMERIC_HEADER_SPAN_INVALID")
            return 1
        return value

    for row_index, cells in enumerate(header_rows):
        cursor = 0
        for cell_index, cell in enumerate(cells):
            free = [index for index in range(cursor, width) if grid[row_index][index] is None]
            available = len(free) - (len(cells) - cell_index - 1)
            if available < 1:
                issues.append("NUMERIC_HEADER_UNALIGNED")
                break
            declared_width = span(cell, "colspan")
            actual_width = min(declared_width, available)
            if actual_width != declared_width:
                issues.append("NUMERIC_HEADER_SPAN_MISMATCH")
            declared_height = span(cell, "rowspan")
            actual_height = min(declared_height, len(header_rows) - row_index)
            label = normalize_inline_text(cell.get_text(" ", strip=True))
            for future_row in grid[row_index : row_index + actual_height]:
                for column in free[:actual_width]:
                    if future_row[column] is not None:
                        issues.append("NUMERIC_HEADER_UNALIGNED")
                    else:
                        future_row[column] = label
            cursor = free[actual_width - 1] + 1

    # Legacy result tables may omit empty trailing unit cells.  Their first
    # header row still names every data column, while the second starts with
    # "單位" and supplies units only through the last non-empty unit.
    # Treat only that exact, unspanned shape as omitted empty cells; a shorter
    # grouped header remains ambiguous and must keep the alignment issue.
    if (
        len(header_rows) == 2
        and len(header_rows[0]) == width
        and 0 < len(header_rows[1]) < width
        and all(
            cell.get(attribute) in (None, "1", 1)
            for cells in header_rows
            for cell in cells
            for attribute in ("colspan", "rowspan")
        )
        and normalize_inline_text(header_rows[0][0].get_text(" ", strip=True)) == "日期"
        and normalize_inline_text(header_rows[1][0].get_text(" ", strip=True)) == "單位"
    ):
        for column in range(len(header_rows[1]), width):
            grid[1][column] = ""

    if any(label is None for row in grid for label in row):
        issues.append("NUMERIC_HEADER_UNALIGNED")
    if "NUMERIC_HEADER_UNALIGNED" in issues:
        return (), tuple(dict.fromkeys(issues))

    paths: list[tuple[str, ...]] = []
    for column in range(width):
        labels: list[str] = []
        for row in grid:
            label = row[column]
            if label and (not labels or labels[-1] != label):
                labels.append(label)
        # Legacy laboratory tables put "單位" beneath "日期" as the label
        # for the units row, not as a unit belonging to the date column.
        if labels[:2] == ["日期", "單位"]:
            labels.pop(1)
        paths.append(tuple(labels))
    if any(not path for path in paths):
        issues.append("NUMERIC_HEADER_UNALIGNED")
        return (), tuple(dict.fromkeys(issues))
    return tuple(paths), tuple(dict.fromkeys(issues))


def _labelled_fields(soup: BeautifulSoup) -> Mapping[str, str]:
    output: dict[str, str] = {}
    for row in soup.find_all("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if not cells:
            continue
        headers = [cell for cell in cells if cell.name == "th"]
        data = [cell for cell in cells if cell.name == "td"]
        if headers and len(headers) == len(data):
            pairs = zip(headers, data)
        elif len(cells) == 2:
            pairs = ((cells[0], cells[1]),)
        else:
            continue
        for label_cell, value_cell in pairs:
            label = normalize_inline_text(label_cell.get_text(" ", strip=True)).rstrip("\uff1a:")
            value = normalize_inline_text(value_cell.get_text(" ", strip=True))
            if label:
                output.setdefault(label, value)
    return output


def _assert_supported_empty_container(
    html_text: str,
    *,
    root_id: str,
    empty_id: str,
    code: str,
) -> None:
    soup = BeautifulSoup(html_text, "html.parser")
    root = soup.find(id=root_id)
    if root is None:
        raise ParseError(
            "clinical list container was missing", code=code.replace("NONEMPTY", "STRUCTURE")
        )
    rows = root.find_all("tr")
    meaningful_cells = [
        cell
        for cell in root.find_all("td")
        if normalize_inline_text(cell.get_text(" ", strip=True)) or cell.find(["a", "img", "input"])
    ]
    if rows or meaningful_cells:
        raise ParseError("non-empty clinical list structure is not verified", code=code)
    if root.find(id=empty_id) is None and soup.find(id=empty_id) is None:
        raise ParseError(
            "clinical empty-state marker was missing", code=code.replace("NONEMPTY", "STRUCTURE")
        )


def _cell_has_record(cell: Tag) -> bool:
    return bool(cell.find(["a", "img", "input"])) or bool(
        normalize_inline_text(cell.get_text(" ", strip=True)).strip("-—")
    )


def _last_query_values(query: str) -> dict[str, str]:
    return {key: values[-1] for key, values in parse_qs(query, keep_blank_values=True).items()}


def _attribute_value(source: str, name: str) -> str:
    match = re.search(rf"\b{re.escape(name)}\s*=\s*\\?['\"](?P<value>[^'\"]*)", source)
    return normalize_inline_text(match.group("value")) if match else ""


def _date_key(value: str) -> tuple[int, int, int, str]:
    candidate = normalize_inline_text(value).replace("/", "-")[:10]
    try:
        parsed = datetime.strptime(candidate, "%Y-%m-%d")
        return parsed.year, parsed.month, parsed.day, value
    except ValueError:
        return 0, 0, 0, value


def _order_sort_key(order: ClinicalOrder) -> tuple[tuple[int, int, int, str], str, str]:
    return _date_key(order.order_date), order.case_no, order.name


def _medication_sort_key(order: MedicationOrder) -> tuple[tuple[int, int, int, str], str, str]:
    return _date_key(order.start_date), order.case_no, order.name
