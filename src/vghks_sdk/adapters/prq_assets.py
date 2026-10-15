"""PRQ order details/reports and patient-bound PACS/PDF downloads."""

from __future__ import annotations

from urllib.parse import quote, urlencode

from ..core.browser_headers import IMAGE_ACCEPT
from ..core.errors import ConfigurationError
from ..core.operations import operation_spec
from ..models import (
    BinaryAsset,
    OrderDetail,
    OrderDetailRef,
    OrderReport,
    OrderReportRef,
    PacsImageRef,
    PacsStudy,
    PacsStudyRef,
    PdfAttachmentRef,
)
from ..parsing.assets import parse_binary_asset
from ..parsing.clinical import parse_order_detail, parse_order_report, parse_pacs_study
from .prq_context import PrqPatientContext

_ORDER_DETAIL = operation_spec("prq.order_detail")


_ORDER_REPORT = operation_spec("prq.order_report")


_PACS_STUDY = operation_spec("prq.pacs_study")


_PACS_IMAGE = operation_spec("prq.pacs_image")


_PDF_ATTACHMENT = operation_spec("prq.pdf_attachment")


_MAX_ASSET_BYTES = 64 * 1024 * 1024


class PrqAssetOperations(PrqPatientContext):
    def get_order_detail(self, reference: OrderDetailRef) -> OrderDetail:
        if not isinstance(reference, OrderDetailRef):
            raise ConfigurationError("order detail requires an OrderDetailRef")

        def operation() -> OrderDetail:
            hid = self.runtime.auth.hid_for("prq")
            base = self.runtime.settings.prq_base_url.rstrip("/")
            html_text = self.runtime.request_text(
                _ORDER_DETAIL,
                f"{base}/QueryOrderDetail.do",
                params={
                    "caseNo": reference.case_no,
                    "caseType": reference.case_type,
                    "hhisnum": reference.mrn,
                    "hid": hid,
                    "orRsType": reference.result_type,
                    "orStep": reference.order_step,
                    "seqNo": reference.sequence_no,
                    "source": reference.source,
                },
            )
            return parse_order_detail(html_text, reference=reference)

        return self.runtime.execute(
            _ORDER_DETAIL,
            operation,
            operation_name="get_order_detail",
        )

    def get_order_report(self, reference: OrderReportRef) -> OrderReport:
        if not isinstance(reference, OrderReportRef):
            raise ConfigurationError("order report requires an OrderReportRef")

        def operation() -> OrderReport:
            self._prime_key_raw()
            hid = self.runtime.auth.hid_for("prq")
            base = self.runtime.settings.prq_base_url.rstrip("/")
            html_text = self.runtime.request_text(
                _ORDER_REPORT,
                f"{base}/QueryReportByOrder.do",
                params={
                    "caseNo": reference.case_no,
                    "caseType": reference.case_type,
                    "hhisnum": reference.mrn,
                    "hid": hid,
                    "orDept": reference.department,
                    "orRsType": reference.result_type,
                    "orpfcode": reference.fee_code,
                    "seqNo": reference.sequence_no,
                    "source": reference.source,
                },
            )
            return parse_order_report(html_text, reference=reference)

        return self.runtime.execute(
            _ORDER_REPORT,
            operation,
            operation_name="get_order_report",
        )

    def get_pacs_study(self, reference: PacsStudyRef) -> PacsStudy:
        if not isinstance(reference, PacsStudyRef):
            raise ConfigurationError("PACS query requires a PacsStudyRef")

        def operation() -> PacsStudy:
            hid = self.runtime.auth.hid_for("prq")
            base = self.runtime.settings.prq_base_url.rstrip("/")
            html_text = self.runtime.request_text(
                _PACS_STUDY,
                f"{base}/Adm_QueryPACS.do",
                params={
                    "hhisnum": reference.mrn,
                    "hid": hid,
                    "reqCode": "queryPACS",
                    "reqno": reference.request_no,
                },
            )
            return parse_pacs_study(html_text, reference=reference)

        return self.runtime.execute(
            _PACS_STUDY,
            operation,
            operation_name="get_pacs_study",
        )

    def download_pacs_image(self, reference: PacsImageRef) -> BinaryAsset:
        if not isinstance(reference, PacsImageRef):
            raise ConfigurationError("PACS download requires a PacsImageRef")

        def operation() -> BinaryAsset:
            hid = self.runtime.auth.hid_for("prq")
            base = self.runtime.settings.prq_base_url.rstrip("/")
            if reference.img_url:
                params = {
                    "hhisnum": reference.mrn,
                    "hid": hid,
                    "reqno": reference.request_no,
                    "imgUrl": reference.img_url,
                }
            else:
                params = {
                    "SERIES_UID": reference.series_uid,
                    "STUDY_UID": reference.study_uid,
                    "hhisnum": reference.mrn,
                    "hid": hid,
                    "reqno": reference.request_no,
                    "uid": reference.uid,
                }
            content, _ = self.runtime.request_binary(
                _PACS_IMAGE,
                f"{base}/Page/JSP/showPACSPic.jsp",
                max_bytes=_MAX_ASSET_BYTES,
                headers={
                    "Accept": IMAGE_ACCEPT,
                    "Referer": f"{base}/Adm_QueryPACS.do?"
                    + urlencode(
                        {
                            "hhisnum": reference.mrn,
                            "hid": hid,
                            "reqCode": "queryPACS",
                            "reqno": reference.request_no,
                        }
                    ),
                },
                params=params,
            )
            return parse_binary_asset(content, media_type="image/jpeg")

        return self.runtime.execute(
            _PACS_IMAGE,
            operation,
            operation_name="download_pacs_image",
        )

    def download_pdf(self, reference: PdfAttachmentRef) -> BinaryAsset:
        if not isinstance(reference, PdfAttachmentRef):
            raise ConfigurationError("PDF download requires a PdfAttachmentRef")

        def operation() -> BinaryAsset:
            hid = self.runtime.auth.hid_for("prq")
            base = self.runtime.settings.prq_base_url.rstrip("/")
            # requests performs the second encoding while serializing params.
            encoded_once = quote(reference.file_path, safe="")
            content, _ = self.runtime.request_binary(
                _PDF_ATTACHMENT,
                f"{base}/Page/JSP/showPDF.jsp",
                max_bytes=_MAX_ASSET_BYTES,
                params={
                    "fileName": encoded_once,
                    "hhisnum": reference.mrn,
                    "hid": hid,
                },
            )
            return parse_binary_asset(content, media_type="application/pdf")

        return self.runtime.execute(
            _PDF_ATTACHMENT,
            operation,
            operation_name="download_pdf",
        )
