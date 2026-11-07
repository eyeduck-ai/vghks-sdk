"""Synthetic Win32 ABI/readiness checks; only one explicit ephemeral-port test."""

from __future__ import annotations

import ctypes
import json
import os
import socket
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from vghks_sdk.core.errors import ConfigurationError
from vghks_sdk.integrations import smartiris_availability as availability
from vghks_sdk.integrations.smartiris import SmartIrisClient, check_smartiris_availability
from vghks_sdk.models import to_jsonable

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = r"C:\Synthetic\DownloadApp.exe"
OTHER = r"C:\Other\DownloadApp.exe"
PORT = 54323


def _put_wide(buffer, text):
    encoded = text.encode("utf-16-le")
    units = [int.from_bytes(encoded[i:i + 2], "little") for i in range(0, len(encoded), 2)]
    assert len(units) < len(buffer)
    for index in range(len(buffer)):
        buffer[index] = units[index] if index < len(units) else 0
    return len(units)


def _tcp_bytes(rows, *, ipv6=False, count=None):
    row_type, table_type = ((availability._Tcp6Row, availability._Tcp6Table) if ipv6 else
                            (availability._TcpRow, availability._TcpTable))
    payload = bytearray(table_type.table.offset + len(rows) * ctypes.sizeof(row_type))
    payload[:4] = (len(rows) if count is None else count).to_bytes(4, "little")
    for index, (pid, port, address, state) in enumerate(rows):
        row = row_type()
        row.pid, row.state = pid, state
        row.local_port = int.from_bytes(port.to_bytes(2, "big"), "little")
        if ipv6:
            row.local_address[:] = address
        else:
            row.local_address = int.from_bytes(address, "little")
        start = table_type.table.offset + index * ctypes.sizeof(row_type)
        payload[start:start + ctypes.sizeof(row_type)] = bytes(row)
    return bytes(payload)


class FakeApi:
    def __init__(self):
        self.drive = 3
        self.attrs = {}
        self.attr_calls = []
        self.error = 18
        self.processes = [(17, "DownloadApp.exe")]
        self.images = {17: EXPECTED}
        self.index = 0
        self.closed = []
        self.opened = []
        self.tcp_calls = []
        self.tcp4 = [_tcp_bytes([(17, PORT, b"\x7f\0\0\x01", 2)])]
        self.tcp6 = [_tcp_bytes([], ipv6=True)]
        self.tcp_status = 0
        self.tcp_size = None
        self.snapshot_error = False
        self.enumeration_error = False
        self.image_size_override = None

    def drive_type(self, root):
        assert root == "C:\\"
        return self.drive

    def attributes(self, path):
        self.attr_calls.append(path)
        value = self.attrs.get(path, 0x20 if path.endswith(".exe") else 0x10)
        if isinstance(value, tuple):
            value, self.error = value
        return value

    def snapshot(self, flags, pid):
        assert (flags, pid) == (2, 0)
        return ctypes.c_void_p(-1).value if self.snapshot_error else 9000

    def first(self, handle, pointer):
        assert handle == 9000 and pointer._obj.size == ctypes.sizeof(availability._ProcessEntry)
        self.index = 0
        return self._entry(pointer)

    def next(self, handle, pointer):
        assert handle == 9000
        self.index += 1
        return self._entry(pointer)

    def _entry(self, pointer):
        if self.index >= len(self.processes):
            self.error = 5 if self.enumeration_error else 18
            return False
        pointer._obj.pid, name = self.processes[self.index]
        _put_wide(pointer._obj.name, name)
        return True

    def open_process(self, rights, inherit, pid):
        assert rights == 0x1000 and inherit is False
        self.opened.append(pid)
        return pid if self.images.get(pid) is not None else None

    def image_name(self, handle, flags, buffer, size):
        assert flags == 0 and size._obj.value == len(buffer)
        length = _put_wide(buffer, self.images[handle])
        size._obj.value = length if self.image_size_override is None else self.image_size_override
        return True

    def close(self, handle):
        self.closed.append(handle)
        return True

    def last_error(self):
        return self.error

    def tcp_table(self, buffer, size, order, family, table_class, reserved):
        assert not order and family in {2, 23} and (table_class, reserved) == (3, 0)
        assert size._obj.value == availability._TABLE_CAPACITY
        self.tcp_calls.append(family)
        replies = self.tcp4 if family == 2 else self.tcp6
        raw = replies.pop(0) if len(replies) > 1 else replies[0]
        assert len(raw) <= len(buffer)
        ctypes.memmove(buffer, raw, len(raw))
        size._obj.value = len(raw) if self.tcp_size is None else self.tcp_size
        return self.tcp_status


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        for replacement in (
            patch.object(availability.os, "name", "nt"),
            patch.object(availability, "_WindowsApi", return_value=self.api),
            patch("socket.socket.connect", side_effect=AssertionError("connect prohibited")),
            patch("requests.Session.request", side_effect=AssertionError("HTTP prohibited")),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    def check(self):
        return check_smartiris_availability(download_app_path=EXPECTED, port=PORT)

    def test_expected_process_must_own_the_listener_and_summary_is_safe(self):
        result = self.check()
        self.assertEqual(result.status, "AVAILABLE")
        self.assertEqual((result.installation, result.process, result.listener, result.owner),
                         ("PRESENT", "RUNNING", "LISTENING", "MATCH_EXPECTED_PATH"))
        self.assertEqual(self.api.tcp_calls, [2, 2])
        self.assertEqual(self.api.closed.count(17), 2)
        self.assertIn(9000, self.api.closed)
        serialized = json.dumps(to_jsonable(result)) + repr(result)
        self.assertNotIn(EXPECTED, serialized)
        self.assertNotIn("pid", serialized)

    def test_installation_absence_is_independent_of_process_and_listener(self):
        self.api.attrs[EXPECTED] = (0xFFFFFFFF, 2)
        self.api.processes = []
        self.api.tcp4 = [_tcp_bytes([])]
        result = self.check()
        self.assertEqual((result.status, result.installation, result.process, result.listener),
                         ("UNAVAILABLE", "MISSING", "NOT_RUNNING", "NOT_LISTENING"))

    def test_same_name_other_path_and_other_listener_owner_are_not_available(self):
        self.api.images[17] = OTHER
        result = self.check()
        self.assertEqual((result.status, result.process, result.owner),
                         ("UNAVAILABLE", "NOT_RUNNING", "OTHER_PROCESS"))

    def test_unreadable_owner_is_unknown_not_uninstalled(self):
        self.api.images[17] = None
        self.api.attrs[EXPECTED] = (0xFFFFFFFF, 5)
        result = self.check()
        self.assertEqual((result.status, result.installation, result.process, result.owner),
                         ("UNKNOWN", "UNKNOWN", "UNKNOWN", "UNKNOWN"))
        self.assertEqual(result.listener, "LISTENING")
        self.assertIn("SMARTIRIS_PROCESS_IMAGE_UNKNOWN", result.issues)
        self.assertIn(9000, self.api.closed)

    def test_mapped_drive_or_reparse_path_is_not_traversed(self):
        self.api.drive = 4
        result = self.check()
        self.assertEqual(result.status, "UNKNOWN")
        self.assertEqual(self.api.attr_calls, [])
        self.assertEqual(self.api.tcp_calls, [])
        self.api.drive = 3
        self.api.attrs[r"C:\Synthetic"] = 0x410
        result = self.check()
        self.assertEqual(result.installation, "UNKNOWN")
        self.assertEqual(self.api.attr_calls, [r"C:\Synthetic"])

    def test_fixed_buffers_bounds_and_unknown_states_fail_closed(self):
        for status, size, raw in (
            (122, availability._TABLE_CAPACITY + 1, _tcp_bytes([])),
            (5, 4, _tcp_bytes([])),
            (0, availability._TABLE_CAPACITY + 1, _tcp_bytes([])),
            (0, 3, _tcp_bytes([])),
            (0, 4, _tcp_bytes([], count=0xFFFFFFFF)),
            (0, None, _tcp_bytes([(17, PORT, b"\x7f\0\0\x01", 0)])),
        ):
            with self.subTest(status=status, size=size):
                self.api.tcp_status, self.api.tcp_size, self.api.tcp4 = status, size, [raw]
                result = self.check()
                self.assertEqual(result.status, "UNKNOWN")
                self.assertNotEqual(result.listener, "NOT_LISTENING")

    def test_listener_changes_or_multiple_owners_are_unknown(self):
        self.api.tcp4.append(_tcp_bytes([]))
        self.assertEqual(self.check().status, "UNKNOWN")
        self.api.tcp4 = [_tcp_bytes([(17, PORT, b"\x7f\0\0\x01", 2),
                                     (18, PORT, b"\0" * 4, 2)])]
        result = self.check()
        self.assertEqual(result.status, "UNKNOWN")
        self.assertIn("SMARTIRIS_LISTENER_AMBIGUOUS", result.issues)

    def test_wildcard_accepts_loopback_but_other_address_or_port_does_not(self):
        self.api.tcp4 = [_tcp_bytes([(17, PORT, b"\0" * 4, 2)])]
        self.assertEqual(self.check().status, "AVAILABLE")
        self.api.tcp4 = [_tcp_bytes([(17, PORT, b"\xc0\0\x02\x01", 2),
                                     (17, PORT + 1, b"\x7f\0\0\x01", 2)])]
        self.assertEqual(self.check().status, "UNAVAILABLE")

    def test_possible_ipv6_dual_stack_listener_is_unknown(self):
        self.api.tcp4 = [_tcp_bytes([])]
        self.api.tcp6 = [_tcp_bytes([(17, PORT, b"\0" * 16, 2)], ipv6=True)]
        result = self.check()
        self.assertEqual((result.status, result.listener), ("UNKNOWN", "UNKNOWN"))
        self.assertIn("SMARTIRIS_IPV6_LISTENER_UNCONFIRMED", result.issues)

    def test_snapshot_failure_cannot_hide_positive_listener_owner_evidence(self):
        self.api.snapshot_error = True
        result = self.check()
        self.assertEqual((result.status, result.process), ("AVAILABLE", "RUNNING"))
        self.assertIn("SMARTIRIS_PROCESS_SNAPSHOT_FAILED", result.issues)
        self.api.tcp4 = [_tcp_bytes([])]
        result = self.check()
        self.assertEqual((result.status, result.process), ("UNAVAILABLE", "UNKNOWN"))

    def test_image_length_and_enumeration_failure_remain_unknown(self):
        self.api.image_size_override = availability._PATH_CAPACITY
        self.assertEqual(self.check().status, "UNKNOWN")
        self.api.image_size_override = None
        self.api.images[17] = OTHER
        self.api.enumeration_error = True
        result = self.check()
        self.assertEqual(result.process, "UNKNOWN")
        self.assertIn(9000, self.api.closed)

    def test_invalid_paths_and_ports_fail_before_any_api(self):
        for path in ("DownloadApp.exe", r"C:DownloadApp.exe", r"\\host\share\x.exe",
                     r"\\?\C:\x.exe", "https://example.test/x.exe", "C:\\x.exe:stream",
                     "C:\\..\\x.exe", "C:\\x.exe\n", "C:\\x.txt", "C:\\x.exe ", None):
            with self.subTest(path=path), self.assertRaises(ConfigurationError):
                check_smartiris_availability(download_app_path=path)
        for port in (0, -1, 65536, True, "15516", None):
            with self.subTest(port=port), self.assertRaises(ConfigurationError):
                check_smartiris_availability(download_app_path=EXPECTED, port=port)
        self.assertEqual(self.api.attr_calls, [])

    def test_nonwindows_and_unavailable_api_are_explicit(self):
        with patch.object(availability.os, "name", "posix"):
            self.assertEqual(self.check().status, "UNSUPPORTED")
        with patch.object(availability, "_WindowsApi", side_effect=OSError("PRIVATE_PATH")):
            result = self.check()
        self.assertEqual(result.status, "UNKNOWN")
        self.assertNotIn("PRIVATE_PATH", repr(result))

    def test_client_method_is_explicit_and_uses_configured_path_and_port(self):
        with SmartIrisClient(bridge_url=f"http://localhost:{PORT}", download_app_path=EXPECTED) as client:
            self.assertEqual(self.api.attr_calls, [])
            self.assertEqual(client.check_availability().status, "AVAILABLE")


class AvailabilityRealApiTests(unittest.TestCase):
    def test_import_and_constructor_never_load_windows_apis_or_connect(self):
        source = r'''
from unittest.mock import patch
with patch('ctypes.WinDLL', side_effect=AssertionError('API load forbidden'), create=True), \
     patch('socket.socket.connect', side_effect=AssertionError('connect forbidden')):
    from vghks_sdk.integrations.smartiris import SmartIrisClient
    with SmartIrisClient():
        pass
'''
        run = subprocess.run([sys.executable, "-c", source], cwd=ROOT, capture_output=True,
                             text=True, timeout=15,
                             env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
        self.assertEqual(run.returncode, 0, run.stderr)

    @unittest.skipUnless(os.name == "nt", "real read-only Windows APIs")
    def test_actual_api_matches_only_our_ephemeral_listener_without_connecting(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            self.assertNotEqual(port, 15516)
            with patch("socket.socket.connect", side_effect=AssertionError("connect forbidden")), \
                    patch("requests.Session.request", side_effect=AssertionError("HTTP forbidden")):
                result = check_smartiris_availability(download_app_path=sys.executable, port=port)
        self.assertEqual(result.status, "AVAILABLE", result)
        self.assertEqual(result.owner, "MATCH_EXPECTED_PATH")


if __name__ == "__main__":
    unittest.main()
