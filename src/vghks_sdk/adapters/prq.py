"""Compatible PRQ adapter facade; focused operation groups share one Runtime."""

from __future__ import annotations

from ..runtime import SDKRuntime
from .prq_assets import PrqAssetOperations
from .prq_extensions import PrqExtendedOperations
from .prq_orders import PrqOrderOperations
from .prq_visits import PrqVisitOperations


class PrqAdapter(PrqVisitOperations, PrqOrderOperations, PrqAssetOperations, PrqExtendedOperations):
    """Compose PRQ operations on one Runtime, Session and patient context."""

    def __init__(self, runtime: SDKRuntime) -> None:
        self.runtime = runtime
        self._opd_initialized: set[tuple[int, str]] = set()
