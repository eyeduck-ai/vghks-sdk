"""Validated references and private, in-memory SmartIris launch context."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit

from ..core.errors import ConfigurationError
from ._validation import _require_navigation_values, _validate_navigation_ref

SMARTWONDER_ORIGIN = "http://pacswonder.vghks.gov.tw:8080"
SMARTWONDER_BASE_URL = SMARTWONDER_ORIGIN + "/SmartWonderRAD"
SMARTWONDER_BOOTSTRAP_URL = SMARTWONDER_BASE_URL + "/Query/qryExternalAP.jsp"
SMARTWONDER_VIEWER_URL = SMARTWONDER_BASE_URL + "/Viewer/vwrIris.jsp"
SMARTWONDER_WADO_URL = "http://192.168.195.101:9190/dcm4jboss-wado/wado"


@dataclass(frozen=True, slots=True)
class PacsLaunchRef:
    """A native-viewer button reference, independent of the JPEG viewer."""

    mrn: str = field(repr=False)
    request_no: str = field(repr=False)

    def __post_init__(self) -> None:
        _validate_navigation_ref(self.mrn, self.request_no)
        _require_navigation_values(self.request_no)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.request_no):
            raise ConfigurationError("invalid PACS launch reference", code="PACS_LAUNCH_REF_INVALID")


@dataclass(frozen=True, slots=True)
class PacsLaunchBootstrap:
    """Patient-bound fields returned by the recorded SmartWonder entry page."""

    reference: PacsLaunchRef = field(repr=False)
    login_id: str = field(repr=False)
    client_ip: str = field(repr=False)
    study_uid: str = field(repr=False)
    dicom_server: str = field(repr=False)
    level: str = field(repr=False)
    hospital_id: str = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.reference) is not PacsLaunchRef:
            raise ConfigurationError("PACS launch reference required", code="PACS_LAUNCH_REF_INVALID")
        self.reference.__post_init__()
        if not isinstance(self.login_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,32}", self.login_id
        ):
            raise ConfigurationError("invalid PACS login binding", code="PACS_LAUNCH_LOGIN_INVALID")
        try:
            if not isinstance(self.client_ip, str):
                raise ValueError
            ipaddress.IPv4Address(self.client_ip)
        except (ValueError, TypeError):
            raise ConfigurationError(
                "invalid PACS client address", code="PACS_LAUNCH_CLIENT_INVALID"
            ) from None
        if not isinstance(self.study_uid, str) or not re.fullmatch(
            r"[0-9]+(?:\.[0-9]+)*", self.study_uid
        ) or len(self.study_uid) > 64:
            raise ConfigurationError("invalid PACS study UID", code="PACS_LAUNCH_UID_INVALID")
        if self.dicom_server != "SK" or self.level != "STUDY":
            raise ConfigurationError("unsupported PACS launch mode", code="PACS_LAUNCH_MODE_UNSUPPORTED")
        if not isinstance(self.hospital_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{0,32}", self.hospital_id
        ):
            raise ConfigurationError("invalid PACS hospital binding", code="PACS_LAUNCH_HOSPITAL_INVALID")


@dataclass(frozen=True, slots=True)
class PacsLaunchContext(PacsLaunchBootstrap):
    """Validated local launch inputs; repr and SDK JSON omit all private fields."""

    wonder_url: str = field(repr=False)
    wado_url: str = field(repr=False)

    def __post_init__(self) -> None:
        PacsLaunchBootstrap.__post_init__(self)
        if self.wonder_url != SMARTWONDER_BASE_URL:
            raise ConfigurationError("unknown PACS viewer source", code="PACS_LAUNCH_SOURCE_INVALID")
        if not isinstance(self.wado_url, str) or re.search(r"[\x00-\x20\x7f\\#]", self.wado_url):
            raise ConfigurationError("invalid PACS WADO URL", code="PACS_LAUNCH_WADO_INVALID")
        try:
            parsed = urlsplit(self.wado_url)
            if parsed.scheme + "://" + parsed.netloc + parsed.path != SMARTWONDER_WADO_URL:
                raise ValueError
            # The recorded viewer appends one '&' for its empty recycle option.
            query = parsed.query.removesuffix("&")
            pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True, max_num_fields=6)
        except ValueError:
            raise ConfigurationError("invalid PACS WADO URL", code="PACS_LAUNCH_WADO_INVALID") from None
        expected = {
            "requestType": "WADO", "contentType": "application/dicom;text/plain",
            "DicomServer": self.dicom_server, "PID": "vwrIris",
            "Dcm00110010": self.login_id, "Dcm00110011": self.client_ip,
        }
        if len(pairs) != len(expected) or dict(pairs) != expected:
            raise ConfigurationError("PACS WADO binding mismatch", code="PACS_LAUNCH_WADO_MISMATCH")

    @property
    def cmove_url(self) -> str:
        """Build the recorded context URL from already validated fields."""

        return self.wonder_url + "/servlet/dcmCMoveSCUService?&" + urlencode({
            "HospID": self.hospital_id,
            "PatID": self.reference.mrn,
            "AccNo": self.reference.request_no,
            "PID": "vwrIris",
            "UserID": self.login_id,
            "ClientIP": self.client_ip,
            "NotifyResult": "false",
        })
