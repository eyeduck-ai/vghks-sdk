from __future__ import annotations

from ..adapters.protocols import PrqAdapterProtocol
from ..models import (
    BinaryAsset,
    ClinicalOrder,
    OrderDetail,
    OrderDetailRef,
    OrderHistoryFilter,
    OrderReport,
    OrderReportRef,
    PacsImageRef,
    PacsStudy,
    PacsStudyRef,
    PdfAttachmentRef,
    VisitCase,
)
from ..models.pacs_launch import PacsLaunchContext, PacsLaunchRef


class OrdersService:
    def __init__(self, adapter: PrqAdapterProtocol) -> None:
        self._adapter = adapter

    def get_case_orders(self, case: VisitCase) -> list[ClinicalOrder]:
        return self._adapter.get_case_orders(case)

    def get_order_history(
        self,
        mrn: str,
        filter: OrderHistoryFilter,
    ) -> list[ClinicalOrder]:
        return self._adapter.get_order_history(mrn, filter)

    def get_order_detail(self, ref: OrderDetailRef) -> OrderDetail:
        return self._adapter.get_order_detail(ref)

    def get_order_report(self, ref: OrderReportRef) -> OrderReport:
        return self._adapter.get_order_report(ref)

    def get_pacs_study(self, ref: PacsStudyRef) -> PacsStudy:
        """Compatible JPEG entry point; prefer get_pacs_jpeg_study."""
        return self.get_pacs_jpeg_study(ref)

    def get_pacs_jpeg_study(self, ref: PacsStudyRef) -> PacsStudy:
        """List JPEG references; does not open the native PACS viewer."""
        return self._adapter.get_pacs_study(ref)

    def download_pacs_image(self, ref: PacsImageRef) -> BinaryAsset:
        """Compatible JPEG download; prefer download_pacs_jpeg."""
        return self.download_pacs_jpeg(ref)

    def download_pacs_jpeg(self, ref: PacsImageRef) -> BinaryAsset:
        return self._adapter.download_pacs_image(ref)

    def prepare_pacs_launch(self, ref: PacsLaunchRef) -> PacsLaunchContext:
        """Fetch patient-bound native launch context, without any local action."""
        return self._adapter.prepare_pacs_launch(ref)

    def download_pdf(self, ref: PdfAttachmentRef) -> BinaryAsset:
        return self._adapter.download_pdf(ref)
