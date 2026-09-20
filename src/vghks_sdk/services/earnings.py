from __future__ import annotations

from ..adapters.earnings import EarningsAdapter
from ..core.config import EarningsCredentials
from ..models import EarningsReportContext, HtmlDocument


class EarningsService:
    """Secondary credentials are explicit and separate from configuration."""

    def __init__(self, adapter: EarningsAdapter) -> None:
        self._adapter = adapter

    def open_performance(self, credentials: EarningsCredentials) -> EarningsReportContext:
        return self._adapter.open_report("performance", credentials)

    def open_bonus(self, credentials: EarningsCredentials) -> EarningsReportContext:
        return self._adapter.open_report("payroll", credentials)

    def get_report(self, context: EarningsReportContext, period: str | None = None) -> HtmlDocument:
        return self._adapter.get_report(context, period)
