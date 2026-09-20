"""WebMAAS patient and registration endpoint adapter."""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urljoin, urlsplit

from ..core.errors import ParseError
from ..core.operations import operation_spec
from ..identifiers import normalize_mrn
from ..models import PatientBasicInfo, PatientDemographics, RegistrationRecord
from ..parsing.webmaas import (
    find_next_displaytag_href,
    parse_patient_basic_info,
    parse_patient_demographics,
    parse_query_form,
    parse_registration_records,
)
from ..runtime import SDKRuntime

_DEMOGRAPHICS = operation_spec("webmaas.demographics")
_REGISTRATION_LANDING = operation_spec("webmaas.registration_landing")
_REGISTRATION_QUERY = operation_spec("webmaas.registration_query")
_BASIC_INFO_LANDING = operation_spec("webmaas.basic_info_landing")
_BASIC_INFO = operation_spec("webmaas.basic_info")


class WebMaasAdapter:
    def __init__(self, runtime: SDKRuntime) -> None:
        self.runtime = runtime

    def get_demographics(self, mrn: str) -> PatientDemographics:
        mrn = normalize_mrn(mrn)

        def operation() -> PatientDemographics:
            self.runtime.auth.ensure_webmaas_page("RSV11W001")
            return self._get_demographics_raw(mrn)

        return self.runtime.execute(
            _DEMOGRAPHICS,
            operation,
            operation_name="get_patient_demographics",
        )

    def get_basic_info(self, mrn: str) -> PatientBasicInfo:
        mrn = normalize_mrn(mrn)

        def operation() -> PatientBasicInfo:
            self.runtime.auth.ensure_webmaas_page("QUY15W001")
            base = self.runtime.settings.webmaas_base_url.rstrip("/")
            url = f"{base}/QUY/QUY15W001.do"
            landing = self.runtime.request_text(_BASIC_INFO_LANDING, url)
            payload = parse_query_form(landing, "QUY15WForm")
            self._get_demographics_raw(mrn, page_id="QUY15W001")
            payload.update(
                {
                    "pageid": "QUY15W001",
                    "prepageid": "",
                    "QRY": "QRY",
                    "pageSize": "20",
                    "out": "N",
                    "nameQ": "false",
                    "history": "false",
                    "hcaseno": "",
                    "amroom": "",
                    "amsec": "",
                    "hfincl": "",
                    "hfincl2": "",
                    "bedtxt": "",
                    "htrnouth": "",
                    "patno": mrn,
                    "type": "A",
                }
            )
            result = self.runtime.request_text(
                _BASIC_INFO, url, data=payload, headers={"Referer": url}
            )
            return parse_patient_basic_info(result, mrn)

        return self.runtime.execute(_BASIC_INFO, operation, operation_name="get_patient_basic_info")

    def get_registration_history(self, mrn: str) -> list[RegistrationRecord]:
        mrn = normalize_mrn(mrn)

        def operation() -> list[RegistrationRecord]:
            self.runtime.auth.ensure_webmaas_page("RSV11W001")
            base = self.runtime.settings.webmaas_base_url.rstrip("/")
            landing_url = f"{base}/RSV/RSV11W001.do"
            landing = self.runtime.request_text(_REGISTRATION_LANDING, landing_url)
            hidden = parse_query_form(landing, "RSV11WForm")
            demographics = self._get_demographics_raw(mrn)
            token_name = "org.apache.struts.taglib.html.TOKEN"
            payload = {
                "QRY": "QRY",
                "birthday": demographics.birthday,
                "canstatus": "",
                "displaytagPageSizeSelector": "100",
                token_name: hidden[token_name],
                "pageSize": "100",
                "pageid": "RSV11W001",
                "patno": mrn,
                "pdate": "",
                "room": "",
                "sect": "",
            }
            html_text = self.runtime.request_text(
                _REGISTRATION_QUERY,
                landing_url,
                data=payload,
                headers={"Referer": landing_url},
            )
            records: list[RegistrationRecord] = []
            seen_records: set[tuple[tuple[str, str], ...]] = set()
            visited_pages: set[str] = set()
            for _ in range(50):
                for record in parse_registration_records(html_text, mrn):
                    identity = tuple(sorted(record.columns.items()))
                    if identity not in seen_records:
                        seen_records.add(identity)
                        records.append(record)
                href = find_next_displaytag_href(html_text)
                if not href:
                    break
                next_url = urljoin(landing_url, href)
                target = urlsplit(next_url)
                origin = urlsplit(landing_url)
                patient = parse_qs(target.query).get("patno", [mrn])
                if (
                    (target.scheme, target.netloc, target.path)
                    != (origin.scheme, origin.netloc, origin.path)
                    or target.fragment
                    or patient != [mrn]
                ):
                    raise ParseError(
                        "registration pagination left the patient query",
                        code="WEBMAAS_REGISTRATION_PAGE_INVALID",
                    )
                if next_url in visited_pages:
                    raise ParseError(
                        "registration pagination repeated a page",
                        code="WEBMAAS_REGISTRATION_PAGINATION_LOOP",
                    )
                visited_pages.add(next_url)
                html_text = self.runtime.request_text(
                    _REGISTRATION_LANDING,
                    next_url,
                    headers={"Referer": landing_url},
                )
            else:
                raise ParseError(
                    "registration pagination exceeded its page limit",
                    code="WEBMAAS_REGISTRATION_PAGE_LIMIT",
                )
            return records

        return self.runtime.execute(
            _REGISTRATION_QUERY,
            operation,
            operation_name="get_registration_history",
        )

    def _get_demographics_raw(self, mrn: str, *, page_id: str = "RSV11W001") -> PatientDemographics:
        base = self.runtime.settings.webmaas_base_url.rstrip("/")
        page_path = "RSV/RSV11W001.do" if page_id == "RSV11W001" else "QUY/QUY15W001.do"
        payload = self.runtime.request_json(
            _DEMOGRAPHICS,
            f"{base}/ajax/AJAXAction.do",
            params={
                "pageid": page_id,
                "querymethod": "CHECK_PAT",
                "simpleData": "N",
                "ts": str(int(time.time() * 1000)),
            },
            data={"patno": mrn},
            headers={"Referer": f"{base}/{page_path}", "X-Requested-With": "XMLHttpRequest"},
        )
        return parse_patient_demographics(payload, mrn)
