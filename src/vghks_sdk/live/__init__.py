"""Live-test exports loaded on demand, independently of offline analysis."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bundle import LiveTestBundleManager, pack_incomplete_run
    from .config import LiveTestConfig
    from .profile import LIVE_TEST_MRN, LiveTestResult, run_live_test

__all__ = [
    "LIVE_TEST_MRN",
    "LiveTestBundleManager",
    "LiveTestConfig",
    "LiveTestResult",
    "pack_incomplete_run",
    "run_live_test",
]

_EXPORT_MODULES = {
    "LIVE_TEST_MRN": "profile",
    "LiveTestBundleManager": "bundle",
    "LiveTestConfig": "config",
    "LiveTestResult": "profile",
    "pack_incomplete_run": "bundle",
    "run_live_test": "profile",
}


def __getattr__(name: str):
    module = _EXPORT_MODULES.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f".{module}", __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
