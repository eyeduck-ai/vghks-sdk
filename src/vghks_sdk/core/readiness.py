"""Registry and safe report helpers for authentication readiness checks."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .._version import __version__
from ..models import AuthCheckReport, AuthCheckTarget
from .errors import ConfigurationError, ErrorInfo, error_info

AUTH_REPORT_SCHEMA_VERSION = 2
DEFAULT_AUTH_REPORT_PATH = Path("output/auth-check/auth_check_report.json")


@dataclass(frozen=True, slots=True)
class AuthCheckSpec:
    key: str
    capability: tuple[str, ...]
    dependencies: tuple[str, ...]


AUTH_CHECK_REGISTRY: tuple[AuthCheckSpec, ...] = (
    AuthCheckSpec("portal", ("Login", "Session"), ()),
    AuthCheckSpec("prq", ("OPD", "Records", "SOAP", "Numeric"), ("portal",)),
    AuthCheckSpec("sectord", ("WebMAAS bridge",), ("portal",)),
    AuthCheckSpec("webmaas", ("Patients", "Registration"), ("sectord",)),
    AuthCheckSpec("oppl", ("Surgery", "Consent"), ("portal",)),
    AuthCheckSpec("audit", ("Unsigned Records",), ("portal",)),
    AuthCheckSpec("oppl_records", ("Surgery Cases", "Operation Notes"), ("portal",)),
    AuthCheckSpec("review", ("Review Cases", "Decisions", "Attachments"), ("portal",)),
    AuthCheckSpec("personnel", ("Personnel", "Physician Directory"), ("portal",)),
)

_SPEC_BY_KEY = {spec.key: spec for spec in AUTH_CHECK_REGISTRY}


def resolve_auth_targets(only: Sequence[str] | None = None) -> tuple[AuthCheckSpec, ...]:
    """Resolve a target selection before any network request is allowed.

    The default is registry order.  For an explicit selection, duplicates are
    removed while input order is retained and dependencies are inserted before
    the first target that needs them.  Portal is always required.
    """

    if only is None:
        return AUTH_CHECK_REGISTRY

    requested: list[str] = []
    seen_input: set[str] = set()
    for raw in only:
        key = str(raw).strip().lower()
        if not key:
            continue
        if key not in _SPEC_BY_KEY:
            allowed = ", ".join(spec.key for spec in AUTH_CHECK_REGISTRY)
            raise ConfigurationError(
                f"unknown auth-check target '{key}'; expected one of: {allowed}"
            )
        if key not in seen_input:
            seen_input.add(key)
            requested.append(key)
    if not requested:
        raise ConfigurationError("--only requires at least one target")

    resolved: list[AuthCheckSpec] = []
    added: set[str] = set()

    def add_with_dependencies(key: str) -> None:
        if key in added:
            return
        spec = _SPEC_BY_KEY[key]
        for dependency in spec.dependencies:
            add_with_dependencies(dependency)
        added.add(key)
        resolved.append(spec)

    add_with_dependencies("portal")
    for key in requested:
        add_with_dependencies(key)
    return tuple(resolved)


def make_auth_report(
    targets: Iterable[AuthCheckTarget], *, reauthenticated: bool = False
) -> AuthCheckReport:
    target_tuple = tuple(targets)
    portal = next((item for item in target_tuple if item.target == "portal"), None)
    if target_tuple and all(item.status == "OK" for item in target_tuple):
        status = "OK"
    elif portal is None or portal.status != "OK":
        status = "ERROR"
    else:
        status = "COMPLETED_WITH_ERRORS"
    return AuthCheckReport(
        schema_version=AUTH_REPORT_SCHEMA_VERSION,
        sdk_version=__version__,
        generated_at=datetime.now(timezone.utc).isoformat(),
        status=status,
        targets=target_tuple,
        reauthenticated=reauthenticated,
    )


def failed_auth_report(specs: Sequence[AuthCheckSpec], exc: BaseException) -> AuthCheckReport:
    """Build a no-network report for settings/credential setup failures."""

    issue = error_info(exc)
    targets: list[AuthCheckTarget] = []
    for spec in specs:
        is_portal = spec.key == "portal"
        targets.append(
            AuthCheckTarget(
                target=spec.key,
                capability=spec.capability,
                landing_path="",
                hid_present=False,
                cookie_count=0,
                duration_ms=0.0,
                dependencies=spec.dependencies,
                status="ERROR" if is_portal else "BLOCKED",
                issue=(issue if is_portal else ErrorInfo("DEPENDENCY_FAILED", "AUTHENTICATION")),
            )
        )
    return make_auth_report(targets)
