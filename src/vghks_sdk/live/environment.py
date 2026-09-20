"""Credential-free runtime facts used to diagnose portable EXE failures."""

from __future__ import annotations

import locale
import os
import platform
import shutil
import ssl
import sys
from pathlib import Path
from typing import Any

import bs4
import requests
import urllib3

from .._version import __version__
from ..build_info import build_identity
from ..core.errors import ConfigurationError
from ..core.tls import validate_tls_trust_store


def environment_report(
    output_directory: Path,
    *,
    ca_bundle: Path | None = None,
) -> dict[str, Any]:
    writable = _directory_writable(output_directory)
    try:
        free_bytes = shutil.disk_usage(output_directory).free
    except OSError:
        free_bytes = None
    try:
        trust_mode = validate_tls_trust_store(
            ca_bundle=str(ca_bundle) if ca_bundle is not None else None
        )
    except ConfigurationError:
        ca_status = "INVALID"
    else:
        ca_status = "CUSTOM_VALID" if trust_mode == "CUSTOM_PEM" else trust_mode
    return {
        "schema_version": 1,
        "sdk_version": __version__,
        "build": build_identity(),
        "operating_system": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "architecture": platform.architecture()[0],
        },
        "runtime": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "frozen": bool(getattr(sys, "frozen", False)),
            "requests": requests.__version__,
            "urllib3": urllib3.__version__,
            "beautifulsoup4": bs4.__version__,
            "openssl": ssl.OPENSSL_VERSION,
        },
        "console": {
            "stdout_encoding": str(getattr(sys.stdout, "encoding", "") or ""),
            "stderr_encoding": str(getattr(sys.stderr, "encoding", "") or ""),
            "preferred_encoding": locale.getpreferredencoding(False),
        },
        "output": {
            "writable": writable,
            "free_bytes": free_bytes,
        },
        "ca_mode": ca_status,
        "network_environment": {
            # Presence only: proxy URLs can contain usernames and passwords.
            "environment_variables_present": sorted(
                key.upper()
                for key in os.environ
                if key.upper()
                in {
                    "HTTP_PROXY",
                    "HTTPS_PROXY",
                    "ALL_PROXY",
                    "NO_PROXY",
                    "REQUESTS_CA_BUNDLE",
                    "CURL_CA_BUNDLE",
                }
            ),
            "tls12_available": ssl.HAS_TLSv1_2,
            "tls13_available": ssl.HAS_TLSv1_3,
        },
    }


def _directory_writable(directory: Path) -> bool:
    probe = directory / ".vghks-write-probe"
    try:
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError:
        return False
    return True
