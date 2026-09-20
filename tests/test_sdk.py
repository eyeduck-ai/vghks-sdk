from __future__ import annotations

import ast
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from vghks_sdk import PortalCredentials, SDKSettings, VghksSDK, VisitFilter
from vghks_sdk.core.tls import WindowsSystemTrustAdapter
from vghks_sdk.core.transport import SafeSessionTransport
from vghks_sdk.models import VisitCase


class SDKFacadeTests(unittest.TestCase):
    def test_default_windows_sdk_uses_system_trust_session(self) -> None:
        # Patch the SDK's reference, not the global platform module used by truststore.
        with patch("vghks_sdk.core.tls.platform", SimpleNamespace(system=lambda value="Windows": value)):
            sdk = VghksSDK(
                settings=SDKSettings(),
                credentials=PortalCredentials("TEST-USER", "TEST-PASSWORD"),
            )
        try:
            adapter = sdk._runtime.transport.session.get_adapter("https://portal.example")
            self.assertIsInstance(adapter, WindowsSystemTrustAdapter)
            self.assertIsNot(sdk._runtime.transport.verify, False)
        finally:
            sdk.close()

    def test_every_service_shares_one_context_and_transport(self) -> None:
        transport = MagicMock(spec=SafeSessionTransport)
        with patch("vghks_sdk.sdk.create_requests_session") as session_factory:
            sdk = VghksSDK(
                settings=SDKSettings(),
                credentials=PortalCredentials("TEST-USER", "TEST-PASSWORD"),
                transport=transport,
            )
        session_factory.assert_not_called()
        adapter_services = (
            sdk.patients,
            sdk.opd,
            sdk.records,
            sdk.orders,
            sdk.medications,
            sdk.surgery,
            sdk.audit,
        )
        self.assertIs(sdk.auth._adapter, sdk._runtime)
        self.assertTrue(
            all(service._adapter.runtime is sdk._runtime for service in adapter_services)
        )
        self.assertIs(sdk._runtime.transport, transport)
        sdk.close()
        transport.close.assert_called_once_with()

    def test_record_service_delegates_to_atomic_context(self) -> None:
        transport = MagicMock(spec=SafeSessionTransport)
        sdk = VghksSDK(
            settings=SDKSettings(),
            credentials=PortalCredentials("TEST-USER", "TEST-PASSWORD"),
            transport=transport,
        )
        sdk.records._adapter.get_visit_cases = MagicMock(return_value=[])
        self.assertEqual(sdk.records.get_visit_cases("00000000"), [])
        sdk.records._adapter.get_visit_cases.assert_called_once_with("00000000")

    def test_record_service_filters_raw_cases_without_another_transport_layer(self) -> None:
        transport = MagicMock(spec=SafeSessionTransport)
        sdk = VghksSDK(
            settings=SDKSettings(),
            credentials=PortalCredentials("TEST-USER", "TEST-PASSWORD"),
            transport=transport,
        )
        raw = [
            VisitCase("00000000", date(2026, 1, 1), "O", "EYE", "70", "眼科"),
            VisitCase("00000000", date(2026, 1, 2), "O", "OTHER", "60", "家醫科"),
        ]
        sdk.records._adapter.get_visit_cases = MagicMock(return_value=raw)
        selected = sdk.records.find_visit_cases("00000000", VisitFilter(section_codes=("60",)))
        self.assertEqual([case.case_no for case in selected], ["OTHER"])
        sdk.records._adapter.get_visit_cases.assert_called_once_with("00000000")

    def test_workflow_modules_do_not_import_or_access_http_transport(self) -> None:
        workflow_root = Path(__file__).resolve().parents[1] / "src" / "vghks_sdk" / "workflows"
        forbidden_names = {"requests", "BeautifulSoup", "SafeSessionTransport"}
        for path in workflow_root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute):
                    self.assertNotEqual(
                        node.attr,
                        "transport",
                        f"workflow bypassed services in {path.name}",
                    )
                if isinstance(node, ast.Name):
                    self.assertNotIn(node.id, forbidden_names)
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    module = getattr(node, "module", "") or ""
                    self.assertNotIn("core.transport", module)
                    self.assertNotEqual(module, "requests")


if __name__ == "__main__":
    unittest.main()
