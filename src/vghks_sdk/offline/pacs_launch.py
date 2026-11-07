"""Replay paired PACS preparation pages; never send HTTP or local commands."""

from __future__ import annotations

from collections.abc import Mapping

from ..core.errors import ParseError, error_code
from ..models.pacs_launch import PacsLaunchBootstrap, PacsLaunchRef
from ..parsing.pacs_launch import parse_pacs_launch_bootstrap, parse_pacs_launch_context

PACS_PREPARATION_OPERATIONS = frozenset({
    "smartwonder.launch_bootstrap", "smartwonder.launch_viewer",
})


class PacsLaunchReplay:
    """Associate only an explicit capture operation or HAR page, never a patient guess."""

    def __init__(self) -> None:
        self._contexts: dict[str, PacsLaunchBootstrap] = {}

    def begin(self, operation: str, scope: str) -> None:
        if operation == "smartwonder.launch_bootstrap":
            self._contexts.pop(scope, None)

    def replay(self, operation: str, text: str, params: Mapping[str, str],
               *, scope: str = "") -> dict:
        self.begin(operation, scope)
        if not text:
            return {"status": "UNAVAILABLE", "error_code": "RESPONSE_BODY_MISSING",
                    "record_count": None}
        try:
            if operation == "smartwonder.launch_bootstrap":
                if params.get("LoginLevel") != "RAD":
                    raise ParseError("unknown PACS login level", code="PACS_LOGIN_LEVEL_UNKNOWN")
                bootstrap = parse_pacs_launch_bootstrap(
                    text, reference=PacsLaunchRef(params.get("PatID", ""), params.get("AccNo", "")),
                    expected_login_id=params.get("LoginID", ""),
                )
                if scope:
                    self._contexts[scope] = bootstrap
                return {"status": "PARSED", "error_code": "", "record_count": 1,
                        "pacs_launch_stage": "BOOTSTRAP_PARSED"}
            bootstrap = self._contexts.get(scope) if scope else None
            if bootstrap is None:
                return {"status": "UNAVAILABLE", "error_code": "PACS_LAUNCH_CONTEXT_REQUIRED",
                        "record_count": None}
            if (params.get("LoginID") != bootstrap.login_id
                    or params.get("WonderClientIP") != bootstrap.client_ip):
                raise ParseError("PACS viewer context mismatch", code="PACS_VIEWER_CONTEXT_MISMATCH")
            parse_pacs_launch_context(bootstrap, text)
            return {"status": "PARSED", "error_code": "", "record_count": 1,
                    "pacs_launch_stage": "CONTEXT_PREPARED"}
        except Exception as exc:
            return {"status": "PARSE_ERROR", "error_code": error_code(exc), "record_count": None}
