"""Public independent reads for the preauthorization review system."""

from ..extension_protocols import ReviewsProtocol
from ..models.review import (
    ReviewCase,
    ReviewCaseFilter,
    ReviewCasePart,
    ReviewCaseRef,
    ReviewLoginInfo,
)


class ReviewsService:
    def __init__(self, adapter: ReviewsProtocol) -> None:
        self._adapter = adapter

    def get_login_info(self) -> ReviewLoginInfo:
        return self._adapter.get_login_info()

    def get_options(self) -> dict:
        return self._adapter.get_options()

    def get_doctors(self, department: str) -> list[dict]:
        return self._adapter.get_doctors(department)

    def get_cases(self, filter: ReviewCaseFilter) -> list[ReviewCase]:
        return self._adapter.get_cases(filter)

    def get_case(self, ref: ReviewCaseRef) -> ReviewCase:
        return self._adapter.get_case(ref)

    def get_orders(self, ref: ReviewCaseRef) -> ReviewCasePart:
        return self._adapter.get_orders(ref)

    def get_attachments(self, ref: ReviewCaseRef) -> ReviewCasePart:
        return self._adapter.get_attachments(ref)

    def get_pacs(self, ref: ReviewCaseRef) -> ReviewCasePart:
        return self._adapter.get_pacs(ref)
