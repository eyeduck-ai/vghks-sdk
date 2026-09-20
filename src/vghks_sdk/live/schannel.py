"""Credential-free Windows TLS comparison, independent of Python/OpenSSL.

WinHTTP performs one direct TLS 1.2 GET, using the Windows trust store. It never
authenticates, follows redirects, reads clinical data, or changes SDK routing.
The numeric Windows error helps separate OpenSSL compatibility from network or
server failures when all verified Python profiles fail.
"""

from __future__ import annotations

import ctypes
import platform
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..core.errors import RequestError
from ..local_io import write_json_atomic


def probe_schannel(url: str, *, timeout_seconds: float, context_path: Path) -> dict[str, Any]:
    context: dict[str, Any] = {
        "engine": "WINHTTP_SCHANNEL",
        "tls_protocol": "TLSv1.2",
        "route": "DIRECT",
        "trust_mode": "WINDOWS_SYSTEM",
        "certificate_verification": True,
        "portal_credentials_sent": False,
        "automatic_authentication": False,
        "redirects_allowed": False,
        "url": url,
    }
    parsed = urlsplit(url)
    if (
        platform.system() != "Windows"
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise RequestError("invalid native TLS probe target", code="NETWORK_SCHANNEL_UNAVAILABLE")
    write_json_atomic(context_path, context)
    api = _load_winhttp()
    timeout_ms = max(1, round(min(timeout_seconds, 10) * 1000))

    def checked(value: Any, phase: str) -> Any:
        if value:
            return value
        number = ctypes.get_last_error()
        context.update(phase=phase, windows_error=number, reachable=False)
        write_json_atomic(context_path, context)
        raise RequestError(
            f"WinHTTP {phase} failed with Windows error {number}",
            code=f"NETWORK_SCHANNEL_{number}",
        )

    def option(handle: Any, key: int, number: int) -> None:
        value = ctypes.c_uint32(number)
        checked(api.WinHttpSetOption(handle, key, ctypes.byref(value), 4), f"configure_{key}")

    with ExitStack() as stack:

        def owned(value: Any, phase: str) -> Any:
            handle = checked(value, phase)
            stack.callback(api.WinHttpCloseHandle, handle)
            return handle

        # NO_PROXY, not automatic proxy discovery; never borrow user credentials.
        session = owned(api.WinHttpOpen("vghks-tls-diagnostic", 1, None, None, 0), "open")
        checked(
            api.WinHttpSetTimeouts(session, timeout_ms, timeout_ms, timeout_ms, timeout_ms),
            "timeouts",
        )
        option(session, 84, 0x800)  # SECURE_PROTOCOLS: TLS 1.2 only.
        connection = owned(
            api.WinHttpConnect(session, parsed.hostname, parsed.port or 443, 0), "connect"
        )
        request = owned(
            api.WinHttpOpenRequest(
                connection,
                "GET",
                parsed.path or "/",
                None,
                None,
                None,
                0x00800000,
            ),
            "request",
        )
        # DISABLE_FEATURE: cookies | redirects | automatic authentication.
        option(request, 63, 1 | 2 | 4)
        option(request, 77, 2)  # AUTOLOGON_POLICY: HIGH (no default credentials).
        # Explicitly supply no client certificate. This diagnostic must not
        # borrow a personal certificate or prompt for a smart card/private key.
        checked(api.WinHttpSetOption(request, 47, None, 0), "no_client_certificate")
        checked(api.WinHttpSendRequest(request, None, 0, None, 0, 0, 0), "send")
        checked(api.WinHttpReceiveResponse(request, None), "receive")
        status = ctypes.c_uint32()
        length = ctypes.c_uint32(4)
        checked(
            api.WinHttpQueryHeaders(
                request,
                19 | 0x20000000,
                None,
                ctypes.byref(status),
                ctypes.byref(length),
                None,
            ),
            "status",
        )
        context.update(http_status=status.value, reachable=True)
        write_json_atomic(context_path, context)
        return context


def _load_winhttp() -> Any:
    """Declare 64-bit-safe signatures; importing this module is platform-neutral."""
    api = ctypes.WinDLL("winhttp.dll", use_last_error=True)
    pointer, text, number = ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32
    definitions = {
        "WinHttpOpen": (pointer, [text, number, text, text, number]),
        "WinHttpSetTimeouts": (
            ctypes.c_int,
            [pointer, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int],
        ),
        "WinHttpSetOption": (ctypes.c_int, [pointer, number, pointer, number]),
        "WinHttpConnect": (pointer, [pointer, text, ctypes.c_ushort, number]),
        "WinHttpOpenRequest": (pointer, [pointer, text, text, text, text, pointer, number]),
        "WinHttpSendRequest": (
            ctypes.c_int,
            [pointer, text, number, pointer, number, number, ctypes.c_size_t],
        ),
        "WinHttpReceiveResponse": (ctypes.c_int, [pointer, pointer]),
        "WinHttpQueryHeaders": (ctypes.c_int, [pointer, number, text, pointer, pointer, pointer]),
        "WinHttpCloseHandle": (ctypes.c_int, [pointer]),
    }
    for name, (result, arguments) in definitions.items():
        function = getattr(api, name)
        function.restype, function.argtypes = result, arguments
    return api
