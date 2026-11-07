"""Prepare a native PACS handoff without contacting the workstation bridge."""

from __future__ import annotations

from urllib.parse import urlsplit

from ..core.errors import ConfigurationError, ParseError, SDKError
from ..core.operations import OperationSpec, operation_spec
from ..models.pacs_launch import PacsLaunchContext, PacsLaunchRef
from ..parsing.pacs_launch import parse_pacs_launch_bootstrap, parse_pacs_launch_context
from ..runtime import SDKRuntime

_BASE = "http://pacswonder.vghks.gov.tw:8080/SmartWonderRAD"
_BOOTSTRAP = operation_spec("smartwonder.launch_bootstrap")
_VIEWER = operation_spec("smartwonder.launch_viewer")


def _validated_base(value: str) -> str:
    """The recorded source, or an explicit loopback simulator with that path."""
    if not isinstance(value, str):
        raise ConfigurationError("unrecognized SmartWonder source", code="PACS_SOURCE_INVALID")
    base = value.rstrip("/")
    try:
        parsed = urlsplit(base)
        port = parsed.port
        valid = base == _BASE or (
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost"}
            and port is not None
            and parsed.path == "/SmartWonderRAD"
            and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment
        )
    except (ValueError, AttributeError):
        valid = False
    if not valid or any(c.isspace() for c in base) or "\\" in base:
        raise ConfigurationError("unrecognized SmartWonder source", code="PACS_SOURCE_INVALID")
    return base


class PacsLaunchOperations:
    runtime: SDKRuntime

    def prepare_pacs_launch(self, reference: PacsLaunchRef) -> PacsLaunchContext:
        if not isinstance(reference, PacsLaunchRef):
            raise ConfigurationError("native PACS preparation requires a PacsLaunchRef")
        base = _validated_base(self.runtime.settings.smartwonder_base_url)

        def operation() -> PacsLaunchContext:
            with self.runtime.operation_lock:
                try:
                    # Reuse the SDK's normal authorized PRQ session. There is
                    # no recorded independent SmartWonder password/SSO flow.
                    self.runtime.auth.ensure("prq")
                    login_id = self.runtime.auth.credentials.username
                    page = self._pacs_page(_BOOTSTRAP, base, {
                        "LoginID": login_id, "PatID": reference.mrn,
                        "AccNo": reference.request_no, "LoginLevel": "RAD",
                    })
                    bootstrap = parse_pacs_launch_bootstrap(
                        page, reference=reference, expected_login_id=login_id,
                    )
                    viewer = self._pacs_page(_VIEWER, base, {
                        "LoginID": bootstrap.login_id,
                        "WonderClientIP": bootstrap.client_ip,
                    })
                    return parse_pacs_launch_context(bootstrap, viewer)
                except SDKError as exc:
                    # A denial in this unverified authentication handoff must
                    # not trigger Portal password replay or automatic retry.
                    exc.with_context(retry_safe=False)
                    raise

        return self.runtime.trace_direct("prepare_pacs_launch", "smartwonder", operation)

    def _pacs_page(self, spec: OperationSpec, base: str, params: dict[str, str]) -> str:
        response = self.runtime.request_response(
            spec, base + spec.path.removeprefix("/SmartWonderRAD"),
            params=params, allow_redirects=False, allow_retry=False,
        )
        try:
            if response.status_code != 200:
                raise ParseError(
                    "SmartWonder did not return its expected document",
                    code="PACS_LAUNCH_HTTP_UNEXPECTED", http_status=response.status_code,
                    endpoint_path=spec.path, retry_safe=False,
                )
            return self.runtime.transport.text(response)
        finally:
            response.close()
