"""Classify live and recorded network failures using the same evidence."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def network_error_code(exc: BaseException) -> str:
    pending = [exc]
    visited: set[int] = set()
    names: set[str] = set()
    messages: list[str] = []
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        names.update(cls.__name__ for cls in type(current).__mro__)
        messages.append(str(current))
        for linked in (
            current.__cause__,
            current.__context__,
            getattr(current, "reason", None),
            *current.args,
        ):
            if isinstance(linked, BaseException):
                pending.append(linked)
    return _classify(names, " ".join(messages))


def recorded_network_error_code(error: Mapping[str, Any]) -> str:
    # Legacy bundles contain type/message but no structured exception chain.
    # Inspect those facts instead of trusting the old blanket TLS_VERIFY_FAILED.
    return _classify({str(error.get("type", ""))}, str(error.get("message", "")))


def _classify(names: set[str], message: str) -> str:
    text = message.casefold()
    if "SSLCertVerificationError" in names or any(
        marker in text for marker in ("certificate_verify_failed", "sslcertverificationerror")
    ):
        return "TLS_VERIFY_FAILED"
    if "SSLEOFError" in names or any(
        marker in text
        for marker in ("ssleoferror", "eof occurred in violation of protocol", "unexpected_eof")
    ):
        return "TLS_EOF"
    if any(
        marker in text
        for marker in (
            "wrong_version_number",
            "unsupported_protocol",
            "alert_protocol_version",
            "handshake_failure",
            "no_shared_cipher",
        )
    ):
        return "TLS_PROTOCOL_FAILED"
    if "SSLError" in names:
        return "NETWORK_TLS_FAILED"
    if "ProxyError" in names:
        return "NETWORK_PROXY_FAILED"
    if "ConnectTimeout" in names:
        return "NETWORK_CONNECT_TIMEOUT"
    if "ReadTimeout" in names:
        return "NETWORK_READ_TIMEOUT"
    if "Timeout" in names:
        return "NETWORK_TIMEOUT"
    if names & {"gaierror", "NameResolutionError"} or any(
        marker in text for marker in ("getaddrinfo", "nameresolutionerror")
    ):
        return "NETWORK_DNS_FAILED"
    if "ConnectionError" in names:
        return "NETWORK_CONNECTION_FAILED"
    return "NETWORK_REQUEST_FAILED"
