"""Encoding-safe console setup for Windows, redirected output, and one-file EXEs."""

from __future__ import annotations

import sys
from typing import TextIO


def configure_console_output() -> None:
    """Keep the active encoding while replacing unrepresentable characters safely."""

    _configure_stream(sys.stdout)
    _configure_stream(sys.stderr)


def _configure_stream(stream: TextIO) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if not callable(reconfigure):
        return
    try:
        reconfigure(errors="replace")
    except (OSError, ValueError):
        # Closed and test-provided streams may reject reconfiguration.
        return
