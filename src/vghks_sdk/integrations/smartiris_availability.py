"""Read-only Windows evidence for the configured DownloadApp executable/listener.

No HTTP request, socket connection, registry change, or executable launch occurs.
The snapshot is advisory: ownership can change immediately after it is observed.
"""

from __future__ import annotations

import ctypes
import ntpath
import os
import re
from dataclasses import dataclass
from pathlib import PureWindowsPath

from ..core.errors import ConfigurationError

DEFAULT_DOWNLOAD_APP_PATH = r"C:\TEDPCOCX\DownloadApp.exe"
_DWORD = ctypes.c_uint32
_BOOL = ctypes.c_int32
_HANDLE = ctypes.c_void_p
_WCHAR = ctypes.c_uint16
_TABLE_CAPACITY = 4 * 1024 * 1024
_PATH_CAPACITY = 32768
_MAX_PROCESSES = 65536


@dataclass(frozen=True, slots=True)
class SmartIrisAvailability:
    """Safe states only; no process IDs, paths, account names, or HTTP bodies.

    AVAILABLE means the configured executable owns the IPv4 listener observed
    twice at ``port``. It is not a protocol-health or image-loaded confirmation.
    ``installation`` describes only the configured file, not all installations
    or the separate SmartIris viewer. UNKNOWN must not mean 'not installed'.
    """

    status: str
    installation: str = "UNKNOWN"
    process: str = "UNKNOWN"
    listener: str = "UNKNOWN"
    owner: str = "UNKNOWN"
    port: int = 15516
    issues: tuple[str, ...] = ()


class _ProbeFailure(Exception):
    pass


class _ProcessEntry(ctypes.Structure):
    # Windows DWORD/LONG/WCHAR widths are fixed, including on non-Windows tests.
    _fields_ = [
        ("size", _DWORD), ("usage", _DWORD), ("pid", _DWORD),
        ("heap", ctypes.c_size_t), ("module", _DWORD), ("threads", _DWORD),
        ("parent_pid", _DWORD), ("priority", ctypes.c_int32), ("flags", _DWORD),
        ("name", _WCHAR * 260),
    ]


class _TcpRow(ctypes.Structure):
    _fields_ = [(name, _DWORD) for name in (
        "state", "local_address", "local_port", "remote_address", "remote_port", "pid",
    )]


class _TcpTable(ctypes.Structure):
    _fields_ = [("count", _DWORD), ("table", _TcpRow * 1)]


class _Tcp6Row(ctypes.Structure):
    _fields_ = [
        ("local_address", ctypes.c_ubyte * 16), ("local_scope", _DWORD), ("local_port", _DWORD),
        ("remote_address", ctypes.c_ubyte * 16), ("remote_scope", _DWORD), ("remote_port", _DWORD),
        ("state", _DWORD), ("pid", _DWORD),
    ]


class _Tcp6Table(ctypes.Structure):
    _fields_ = [("count", _DWORD), ("table", _Tcp6Row * 1)]


def _validated_path(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) >= 260:
        raise ConfigurationError("invalid local executable path", code="SMARTIRIS_PATH_INVALID")
    path = PureWindowsPath(value)
    if (not re.fullmatch(r"[A-Za-z]:", path.drive) or path.root != "\\"
            or path.suffix.casefold() != ".exe"
            or any(c in value[2:] for c in ':<>"|?*')
            or any(ord(c) < 32 for c in value)
            or any(part in {".", ".."} or part.endswith((" ", ".")) for part in path.parts[1:])):
        raise ConfigurationError("invalid local executable path", code="SMARTIRIS_PATH_INVALID")
    return str(path)


def _same_path(first: str, second: str) -> bool:
    # QueryFullProcessImageNameW(flags=0) returns Win32 paths. Do not resolve
    # filesystem links or shares just to compare their text.
    return ntpath.normcase(ntpath.normpath(first)) == ntpath.normcase(ntpath.normpath(second))


def _wide_text(buffer: object, length: int | None = None) -> str:
    units = list(buffer)  # type: ignore[arg-type]
    if length is None:
        try:
            length = units.index(0)
        except ValueError:
            raise _ProbeFailure("SMARTIRIS_PROCESS_DATA_INVALID") from None
    if not 0 <= length < len(units) or units[length] != 0:
        raise _ProbeFailure("SMARTIRIS_PROCESS_DATA_INVALID")
    try:
        return b"".join(int(unit).to_bytes(2, "little") for unit in units[:length]).decode("utf-16-le")
    except UnicodeError:
        raise _ProbeFailure("SMARTIRIS_PROCESS_DATA_INVALID") from None


class _WindowsApi:
    """Lazy bindings to a narrow set of documented, read-only Win32 APIs."""

    def __init__(self) -> None:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        iphelper = ctypes.WinDLL("iphlpapi", use_last_error=True)

        def bind(library, name, arguments, result):
            function = getattr(library, name)
            function.argtypes, function.restype = arguments, result
            return function

        self.drive_type = bind(kernel, "GetDriveTypeW", (ctypes.c_wchar_p,), _DWORD)
        self.attributes = bind(kernel, "GetFileAttributesW", (ctypes.c_wchar_p,), _DWORD)
        self.snapshot = bind(kernel, "CreateToolhelp32Snapshot", (_DWORD, _DWORD), _HANDLE)
        self.first = bind(kernel, "Process32FirstW", (_HANDLE, ctypes.POINTER(_ProcessEntry)), _BOOL)
        self.next = bind(kernel, "Process32NextW", (_HANDLE, ctypes.POINTER(_ProcessEntry)), _BOOL)
        self.open_process = bind(kernel, "OpenProcess", (_DWORD, _BOOL, _DWORD), _HANDLE)
        self.image_name = bind(kernel, "QueryFullProcessImageNameW",
                               (_HANDLE, _DWORD, ctypes.POINTER(_WCHAR), ctypes.POINTER(_DWORD)), _BOOL)
        self.close = bind(kernel, "CloseHandle", (_HANDLE,), _BOOL)
        self.tcp_table = bind(iphelper, "GetExtendedTcpTable",
                             (ctypes.c_void_p, ctypes.POINTER(_DWORD), _BOOL, _DWORD,
                              ctypes.c_int32, _DWORD), _DWORD)
        self.last_error = ctypes.get_last_error


def _installation(api: _WindowsApi, path: str) -> str:
    parsed = PureWindowsPath(path)
    # A drive letter can be a mapped network drive. Never inspect its files.
    if api.drive_type(parsed.anchor) != 3:  # DRIVE_FIXED
        raise _ProbeFailure("SMARTIRIS_INSTALL_LOCATION_UNVERIFIED")
    current = PureWindowsPath(parsed.anchor)
    for index, part in enumerate(parsed.parts[1:], 1):
        current /= part
        attributes = api.attributes(str(current))
        if attributes == 0xFFFFFFFF:
            if api.last_error() in {2, 3}:  # FILE_NOT_FOUND / PATH_NOT_FOUND
                return "MISSING"
            raise _ProbeFailure("SMARTIRIS_INSTALL_QUERY_FAILED")
        # Stop before traversing a junction/symlink, which could lead to a share.
        if attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
            raise _ProbeFailure("SMARTIRIS_INSTALL_LOCATION_UNVERIFIED")
        last = index == len(parsed.parts) - 1
        if bool(attributes & 0x10) == last:  # intermediate directory / final file
            return "MISSING"
    return "PRESENT"


def _process_image(api: _WindowsApi, pid: int) -> str:
    handle = api.open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        raise _ProbeFailure("SMARTIRIS_PROCESS_IMAGE_UNKNOWN")
    try:
        buffer = (_WCHAR * _PATH_CAPACITY)()
        size = _DWORD(_PATH_CAPACITY)
        if not api.image_name(handle, 0, buffer, ctypes.byref(size)):
            raise _ProbeFailure("SMARTIRIS_PROCESS_IMAGE_UNKNOWN")
        image = _wide_text(buffer, size.value)
        if not image:
            raise _ProbeFailure("SMARTIRIS_PROCESS_DATA_INVALID")
        return image
    finally:
        api.close(handle)


def _running(api: _WindowsApi, path: str) -> str:
    handle = api.snapshot(2, 0)  # TH32CS_SNAPPROCESS; no modules or process memory.
    if handle in {None, 0, ctypes.c_void_p(-1).value}:
        raise _ProbeFailure("SMARTIRIS_PROCESS_SNAPSHOT_FAILED")
    unknown = False
    expected_name = PureWindowsPath(path).name.casefold()
    try:
        entry = _ProcessEntry()
        entry.size = ctypes.sizeof(entry)
        present = api.first(handle, ctypes.byref(entry))
        count = 0
        while present:
            count += 1
            if count > _MAX_PROCESSES:
                raise _ProbeFailure("SMARTIRIS_PROCESS_LIMIT_EXCEEDED")
            if _wide_text(entry.name).casefold() == expected_name:
                try:
                    if _same_path(_process_image(api, entry.pid), path):
                        return "RUNNING"
                except _ProbeFailure:
                    unknown = True
            present = api.next(handle, ctypes.byref(entry))
        if api.last_error() != 18:  # ERROR_NO_MORE_FILES; other failures are not an empty list.
            raise _ProbeFailure("SMARTIRIS_PROCESS_SNAPSHOT_FAILED")
        if unknown:
            raise _ProbeFailure("SMARTIRIS_PROCESS_IMAGE_UNKNOWN")
        return "NOT_RUNNING"
    finally:
        api.close(handle)


def _listener_rows(api: _WindowsApi, port: int, *, ipv6: bool = False) -> tuple[tuple[bytes, int], ...]:
    # Fixed allocation: do not trust an OS-returned size to allocate unbounded
    # memory. A changed/oversized table is UNKNOWN, never 'no listener'.
    buffer = ctypes.create_string_buffer(_TABLE_CAPACITY)
    size = _DWORD(_TABLE_CAPACITY)
    outcome = api.tcp_table(buffer, ctypes.byref(size), False, 23 if ipv6 else 2, 3, 0)
    if outcome == 122:  # ERROR_INSUFFICIENT_BUFFER
        raise _ProbeFailure("SMARTIRIS_TCP_TABLE_TOO_LARGE")
    if outcome:
        raise _ProbeFailure("SMARTIRIS_TCP_TABLE_UNAVAILABLE")
    row_type, table_type = (_Tcp6Row, _Tcp6Table) if ipv6 else (_TcpRow, _TcpTable)
    offset, stride = table_type.table.offset, ctypes.sizeof(row_type)
    if not offset <= size.value <= _TABLE_CAPACITY:
        raise _ProbeFailure("SMARTIRIS_TCP_TABLE_INVALID")
    raw = buffer.raw[:size.value]
    count = _DWORD.from_buffer_copy(raw[:ctypes.sizeof(_DWORD)]).value
    if count > (size.value - offset) // stride:
        raise _ProbeFailure("SMARTIRIS_TCP_TABLE_INVALID")
    found = set()
    for index in range(count):
        start = offset + index * stride
        row = row_type.from_buffer_copy(raw[start:start + stride])
        row_port = int.from_bytes(row.local_port.to_bytes(4, "little")[:2], "big")
        address = bytes(row.local_address) if ipv6 else row.local_address.to_bytes(4, "little")
        possible = ({b"\0" * 16, b"\0" * 10 + b"\xff\xff\x7f\0\0\x01"} if ipv6 else
                    {b"\x7f\0\0\x01", b"\0\0\0\0"})
        if row_port != port or address not in possible:
            continue
        if row.state != 2 or not row.pid:
            raise _ProbeFailure("SMARTIRIS_TCP_TABLE_INVALID")
        found.add((address, row.pid))
    return tuple(sorted(found))


def check_smartiris_availability(
    *, download_app_path: str = DEFAULT_DOWNLOAD_APP_PATH, port: int = 15516,
) -> SmartIrisAvailability:
    """Inspect local Windows evidence without sending a bridge command.

    Alternate local executable paths/ports support explicit workstation setup
    and localhost tests. UNC, device, relative and mapped-network paths are not
    inspected. Only fixed local drives and paths without reparse components are
    used for file checks. No installation search or automatic startup occurs.
    """
    path = _validated_path(download_app_path)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ConfigurationError("invalid SmartIris port", code="SMARTIRIS_PORT_INVALID")
    if os.name != "nt":
        return SmartIrisAvailability("UNSUPPORTED", port=port, issues=("SMARTIRIS_WINDOWS_REQUIRED",))
    try:
        api = _WindowsApi()
    except (OSError, AttributeError):
        return SmartIrisAvailability("UNKNOWN", port=port, issues=("SMARTIRIS_API_UNAVAILABLE",))
    installation = process = listener = owner = "UNKNOWN"
    issues = []
    try:
        installation = _installation(api, path)
    except _ProbeFailure as exc:
        issues.append(str(exc))
        if str(exc) == "SMARTIRIS_INSTALL_LOCATION_UNVERIFIED":
            return SmartIrisAvailability("UNKNOWN", port=port, issues=tuple(issues))
    try:
        process = _running(api, path)
    except _ProbeFailure as exc:
        issues.append(str(exc))
    try:
        rows = _listener_rows(api, port)
        if not rows:
            # A wildcard IPv6 listener may be dual-stack; the TCP table does
            # not expose IPV6_V6ONLY. Do not falsely label it absent or ready.
            if _listener_rows(api, port, ipv6=True):
                raise _ProbeFailure("SMARTIRIS_IPV6_LISTENER_UNCONFIRMED")
            listener, owner = "NOT_LISTENING", "NOT_APPLICABLE"
        else:
            listener = "LISTENING"
            pids = {pid for _, pid in rows}
            if len(pids) != 1:
                raise _ProbeFailure("SMARTIRIS_LISTENER_AMBIGUOUS")
            image = _process_image(api, next(iter(pids)))
            if _listener_rows(api, port) != rows:
                listener = "UNKNOWN"
                raise _ProbeFailure("SMARTIRIS_LISTENER_CHANGED")
            owner = "MATCH_EXPECTED_PATH" if _same_path(image, path) else "OTHER_PROCESS"
            if owner == "MATCH_EXPECTED_PATH":
                process = "RUNNING"
    except _ProbeFailure as exc:
        issues.append(str(exc))
    status = ("AVAILABLE" if owner == "MATCH_EXPECTED_PATH" else
              "UNAVAILABLE" if listener == "NOT_LISTENING" or owner == "OTHER_PROCESS" else "UNKNOWN")
    return SmartIrisAvailability(status, installation, process, listener, owner, port,
                                tuple(dict.fromkeys(issues)))
