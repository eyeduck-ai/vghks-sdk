"""Shared order-attachment layout; no HTTP, execution flow or filesystem I/O."""

from collections.abc import Mapping
from types import MappingProxyType

ORDER_WORKFLOW_PATH = "parsed/workflows/ophthalmology_orders/"
ORDER_ASSET_STAGE = "stage3_assets"
ORDER_ASSET_FORMATS: Mapping[str, tuple[str, str]] = MappingProxyType({
    "prq.pdf_attachment": ("application/pdf", ".pdf"),
    "prq.pacs_image": ("image/jpeg", ".jpg"),
})
