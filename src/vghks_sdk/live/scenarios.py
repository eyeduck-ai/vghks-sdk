"""Deterministic first-run samples, independent of network and authentication."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, TypeVar

from ..models import (
    MedicationHistoryFilter,
    NumericHistoryFilter,
    OrderHistoryFilter,
    SurgeryHistoryFilter,
)

T = TypeVar("T")


def diverse_sample(items: Iterable[T], limit: int, *, group: Callable[[T], Any]) -> list[T]:
    """Prefer different shapes, then spread remaining slots across newest/oldest."""
    rows = list(items)
    if len(rows) <= limit:
        return rows
    selected: list[int] = []
    seen: set[Any] = set()
    for index, item in enumerate(rows):
        identity = group(item)
        if identity not in seen:
            seen.add(identity)
            selected.append(index)
        if len(selected) >= limit:
            break
    # Deterministic evenly spaced samples include older records, not only the
    # first few rows of one report type. Keep the newest candidate first.
    spread = [round(i * (len(rows) - 1) / max(1, limit - 1)) for i in range(limit)]
    for index in [*spread, *range(len(rows))]:
        if len(selected) >= limit:
            break
        if index not in selected:
            selected.append(index)
    return [rows[index] for index in selected]


def history_scenarios(key: str, *, order_date: Any = None) -> list[Any]:
    """Exercise documented selectors without relying on the preceding result."""
    if key == "prq.order_history":
        return [
            OrderHistoryFilter(category=category, order_date=order_date)
            for category in ("*", "OR", "LAB", "RAD", "PATH")
        ] + [OrderHistoryFilter(lookback_days=30, order_date=order_date)]
    if key == "prq.medication_history":
        return [MedicationHistoryFilter(lookback_days=days) for days in ("all", 30, 365)]
    if key == "prq.numeric_history":
        return [NumericHistoryFilter(lookback_days=days) for days in ("all", 30, 365)]
    return [SurgeryHistoryFilter(lookback_days=days) for days in ("all", 365)]


def reference_group(ref: Any) -> tuple[Any, ...]:
    return tuple(
        getattr(ref, field, "")
        for field in ("case_type", "result_type", "order_step", "source", "study_uid")
    )
