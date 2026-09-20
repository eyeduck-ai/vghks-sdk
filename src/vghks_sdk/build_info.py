"""Version and build label shared by source runs and portable EXE releases."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ._version import __version__


def build_identity() -> dict[str, Any]:
    bundled = Path(__file__).with_name("_build_info.json")
    if getattr(sys, "frozen", False):
        if bundled.is_file():
            return json.loads(bundled.read_text(encoding="utf-8"))
        return {"sdk_version": __version__, "build_id": "unknown"}
    return {"sdk_version": __version__, "build_id": "source"}
