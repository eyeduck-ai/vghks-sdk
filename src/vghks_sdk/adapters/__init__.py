"""Typed adapters for stateful internal application endpoints."""

from .audit import AuditAdapter
from .oppl import OpplAdapter
from .prq import PrqAdapter
from .webmaas import WebMaasAdapter

__all__ = ["AuditAdapter", "OpplAdapter", "PrqAdapter", "WebMaasAdapter"]
