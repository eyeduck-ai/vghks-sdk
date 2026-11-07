"""Opt-in PACS raw evidence writers, separate from test orchestration.

Hospital discovery documents and local bridge exchanges remain distinct. These
writers store unredacted local evidence; they never issue requests or commands.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..local_io import restrict_permissions, write_bytes_atomic, write_json_atomic
from .capture import RawCaptureRecorder

_CAPTURE_PATHS = frozenset({
    "/SmartWonderRAD/Query/qryExternalAP.jsp",
    "/SmartWonderRAD/Viewer/vwrIris.jsp",
    "/PRQWeb/QueryPatientRecord.do",
    "/PRQWeb/Page/JSP/OrderC.jsp",
    "/PRQWeb/Page/JSP/OrderC_Select.jsp",
    "/PRQWeb/QueryOrderResult.do",
    "/PRQWeb/QueryOrderDetail.do",
    "/PRQWeb/QueryReportByOrder.do",
})


class PacsLaunchCapture(RawCaptureRecorder):
    """Keep PACS and order discovery documents, excluding password POSTs."""

    def record_response(self, *, response: Any, **kwargs: Any) -> None:
        # Do not capture a redirect chain containing an unrelated
        # authentication request, even when the final path is allowed.
        chain = [*(getattr(response, "history", ()) or ()), response]
        if all(urlsplit(str(getattr(item, "url", ""))).path in _CAPTURE_PATHS
               for item in chain):
            super().record_response(response=response, **kwargs)

    def record_network_error(self, *, url: str, **kwargs: Any) -> None:
        if urlsplit(url).path in _CAPTURE_PATHS:
            super().record_network_error(url=url, **kwargs)


class NativeCapture:
    """Persist opt-in unredacted bridge exchanges separately from safe results."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.count = 0

    def __call__(self, exchange: Any) -> None:
        self.count += 1
        stem = f"{self.count:03d}"
        body_path = write_bytes_atomic(self.directory / f"{stem}.body", exchange.body)
        metadata_path = write_json_atomic(self.directory / f"{stem}.json", {
            "schema_version": 1,
            "endpoint_path": exchange.endpoint_path,
            "method": exchange.method,
            "request_url": exchange.request_url,
            "http_status": exchange.http_status,
            "response_headers": exchange.response_headers,
            "body_file": body_path.name,
            "complete": exchange.complete,
            "error_code": exchange.error_code,
        })
        restrict_permissions(body_path)
        restrict_permissions(metadata_path)
