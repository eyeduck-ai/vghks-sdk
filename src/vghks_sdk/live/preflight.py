"""Independent service connectivity and readiness checks for a first intranet run."""

from __future__ import annotations

import platform
import queue
import socket
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from requests.auth import AuthBase
from requests.utils import get_environ_proxies, select_proxy

from ..core.config import SDKSettings
from ..core.connections import preferred_tls_profile
from ..core.errors import ErrorInfo, RequestError, error_info
from ..core.readiness import (
    AUTH_CHECK_REGISTRY,
    AuthCheckSpec,
    make_auth_report,
    resolve_auth_targets,
)
from ..core.tls import TLS12, TLS12_COMPAT, TLS_DEFAULT, create_requests_session
from ..core.transport import SafeSessionTransport
from ..local_io import write_json_atomic
from ..models import AuthCheckReport, AuthCheckTarget, to_jsonable
from .profile import LiveTestStep, _run_step
from .schannel import probe_schannel


def run_network_checks(
    settings: SDKSettings,
    steps: list[LiveTestStep],
    *,
    root: Path,
    configure_connection: Callable[..., None] | None = None,
    allow_unverified_tls: bool = False,
    include_mis: bool = False,
    only: tuple[str, ...] | None = None,
    **capture: Any,
) -> dict[str, Any]:
    """Probe selected services and optional MIS, without credentials or redirects.

    Each phase has its own result. An HTTPS request can still succeed via a
    proxy when local DNS or direct TCP fails, so no phase gates another.
    """
    timeout = min(settings.request_policy.connect_timeout_seconds, 10.0)
    selections: dict[str, Any] = {}
    applied_origins: dict[tuple[str | None, int], tuple[str, dict[str, Any]]] = {}
    targets = resolve_auth_targets(only) if only is not None else AUTH_CHECK_REGISTRY
    if include_mis:
        targets = (*targets, AuthCheckSpec("mis", ("Earnings",), ("portal",)))
    for spec in targets:
        url = getattr(settings, f"{spec.key}_base_url").rstrip("/") + "/"
        parsed = urlsplit(url)
        origin = (parsed.hostname, parsed.port or 443)
        if origin in applied_origins:
            shared_app, shared_choice = applied_origins[origin]
            selections[spec.key] = {**shared_choice, "shared_origin_with": shared_app}
            write_json_atomic(root / "parsed" / "network" / "selected_profiles.json", selections)
            continue
        phases = (
            ("dns", lambda url=url: _dns(*_host_port(url), timeout)),
            ("tcp", lambda url=url: _tcp(*_host_port(url), timeout)),
        )
        for phase, callback in phases:
            _run_step(
                steps,
                name=f"network.{spec.key}.{phase}",
                operation=callback,
                output_path=root / "parsed" / "network" / f"{spec.key}-{phase}.json",
                root=root,
                **capture,
            )
        working: list[dict[str, Any]] = []
        unverified_working: list[dict[str, Any]] = []

        def probe(
            phase: str,
            profile: str = TLS_DEFAULT,
            direct: bool = False,
            verify_certificate: bool = True,
            *,
            url: str = url,
            key: str = spec.key,
            working: list[dict[str, Any]] = working,
            unverified_working: list[dict[str, Any]] = unverified_working,
        ) -> bool:
            result, failure = _run_https_probe(
                settings,
                url,
                key,
                phase,
                steps,
                root,
                capture,
                tls_profile=profile,
                direct=direct,
                verify_certificate=verify_certificate,
            )
            if failure is None and result is not None:
                (working if verify_certificate else unverified_working).append(
                    {
                        "tls_profile": profile,
                        "direct": direct,
                        "probe": f"network.{key}.{phase}",
                        "certificate_verification": verify_certificate,
                    }
                )
                return True
            return False

        preferred = preferred_tls_profile(settings, spec.key)
        profiles = tuple(dict.fromkeys((preferred, TLS_DEFAULT, TLS12, TLS12_COMPAT)))
        suffixes = {TLS_DEFAULT: "", TLS12: "_tls12", TLS12_COMPAT: "_tls12_compat"}
        for profile in profiles:
            if probe("https" + suffixes[profile], profile):
                break
        if not working:
            has_proxy = bool(select_proxy(url, get_environ_proxies(url)))
            if has_proxy:
                for profile in profiles:
                    if probe("https_direct" + suffixes[profile], profile, direct=True):
                        break
            if not working:
                # Certificate-only A/B tests: same protocol/ciphers and route,
                # fresh anonymous sessions and no redirects. SDK adoption is a
                # separate explicit live-test policy, applied before login.
                for direct in (False, True) if has_proxy else (False,):
                    route = "https_direct" if direct else "https"
                    for profile in profiles:
                        if probe(f"{route}{suffixes[profile]}_unverified", profile, direct, False):
                            break
            if not working and platform.system() == "Windows":
                _run_step(
                    steps,
                    name=f"network.{spec.key}.https_schannel",
                    operation=lambda url=url, key=spec.key: probe_schannel(
                        url,
                        timeout_seconds=timeout,
                        context_path=root
                        / "parsed"
                        / "network"
                        / f"{key}-https_schannel-context.json",
                    ),
                    output_path=root / "parsed" / "network" / f"{spec.key}-https_schannel.json",
                    root=root,
                    **capture,
                )
        candidates = working or (unverified_working if allow_unverified_tls else [])
        choice = (
            {
                **candidates[0],
                "status": "VERIFIED" if working else "UNVERIFIED_REACHABLE",
                "applied": False,
            }
            if candidates
            else {
                "status": "NO_WORKING_REQUESTS_PROFILE",
                "applied": False,
            }
        )
        parsed = urlsplit(url)
        origin = (parsed.hostname, parsed.port or 443)
        if origin in applied_origins:
            shared_app, shared_choice = applied_origins[origin]
            choice = {**shared_choice, "shared_origin_with": shared_app}
        elif candidates:
            if configure_connection is not None:
                options = {"tls_profile": choice["tls_profile"], "direct": choice["direct"]}
                if not choice["certificate_verification"]:
                    options["verify_certificate"] = False
                    print(
                        f"TLS TEST: {spec.key} will use HTTPS without certificate verification.",
                        flush=True,
                    )
                _, error = _run_step(
                    steps,
                    name=f"network.{spec.key}.configure",
                    operation=lambda key=spec.key, options=options: configure_connection(
                        key,
                        **options,
                    ),
                    output_path=None,
                    root=root,
                    **capture,
                )
                choice["applied"] = error is None
            else:
                choice["applied"] = (
                    choice["tls_profile"] == preferred
                    and not choice["direct"]
                    and choice["certificate_verification"]
                )
        if choice["applied"]:
            applied_origins.setdefault(origin, (spec.key, choice))
        if unverified_working:
            choice["unverified_probe"] = {
                **unverified_working[0],
                "anonymous_probe": True,
                "applied_to_sdk": choice["applied"]
                and not choice.get("certificate_verification", True),
            }
        selections[spec.key] = choice
        write_json_atomic(root / "parsed" / "network" / "selected_profiles.json", selections)
    return selections


def _run_https_probe(
    settings: SDKSettings,
    url: str,
    key: str,
    phase: str,
    steps: list[LiveTestStep],
    root: Path,
    capture: dict[str, Any],
    **options: Any,
) -> tuple[Any, BaseException | None]:
    return _run_step(
        steps,
        name=f"network.{key}.{phase}",
        operation=lambda: _https(
            settings,
            url,
            context_path=root / "parsed" / "network" / f"{key}-{phase}-context.json",
            **options,
            **capture,
        ),
        output_path=root / "parsed" / "network" / f"{key}-{phase}.json",
        root=root,
        **capture,
    )


def independent_readiness(
    sdk: Any,
    steps: list[LiveTestStep],
    *,
    root: Path,
    only: tuple[str, ...] | None = None,
    **capture: Any,
) -> AuthCheckReport:
    """Check each subsystem separately so one failed SSO cannot hide the rest."""
    targets: list[AuthCheckTarget] = []
    by_key: dict[str, AuthCheckTarget] = {}
    reauthenticated = False
    for spec in resolve_auth_targets(only) if only is not None else AUTH_CHECK_REGISTRY:
        if any(by_key[key].status != "OK" for key in spec.dependencies):
            target = AuthCheckTarget(
                spec.key,
                spec.capability,
                "",
                False,
                0,
                0,
                spec.dependencies,
                "BLOCKED",
                ErrorInfo("DEPENDENCY_FAILED", "DEPENDENCY"),
            )
        else:
            report, error = _run_step(
                steps,
                name=f"auth_check.{spec.key}",
                operation=lambda key=spec.key: sdk.auth.check(only=(key,)),
                output_path=root / "parsed" / "readiness" / f"{spec.key}.json",
                classify=lambda value: "OK" if value.ok else "ERROR",
                root=root,
                **capture,
            )
            matches = [item for item in report.targets if item.target == spec.key] if report else []
            target = (
                matches[-1]
                if matches
                else AuthCheckTarget(
                    spec.key,
                    spec.capability,
                    "",
                    False,
                    0,
                    0,
                    spec.dependencies,
                    "ERROR",
                    error_info(error)
                    if error
                    else ErrorInfo("AUTH_READINESS_INCOMPLETE", "AUTHENTICATION"),
                )
            )
            reauthenticated = reauthenticated or bool(report and report.reauthenticated)
        targets.append(target)
        by_key[spec.key] = target
        # Keep partial readiness even if a later check is interrupted.
        result = make_auth_report(targets, reauthenticated=reauthenticated)
        write_json_atomic(root / "parsed" / "readiness.json", to_jsonable(result))
    return result


def _host_port(url: str) -> tuple[str, int]:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise RequestError("invalid HTTPS endpoint", code="NETWORK_ENDPOINT_INVALID")
    return parsed.hostname, parsed.port or 443


def _dns(host: str, port: int, timeout: float) -> dict[str, Any]:
    # getaddrinfo has no portable timeout. A daemon prevents an unavailable
    # resolver from holding the entire EXE open after the bounded wait.
    resolved: queue.Queue[Any] = queue.Queue(maxsize=1)

    def resolve() -> None:
        try:
            resolved.put(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except Exception as exc:
            resolved.put(exc)

    threading.Thread(target=resolve, daemon=True).start()
    try:
        value = resolved.get(timeout=timeout)
    except queue.Empty as exc:
        raise RequestError("DNS lookup timed out", code="NETWORK_DNS_TIMEOUT") from exc
    if isinstance(value, Exception):
        raise RequestError("DNS lookup failed", code="NETWORK_DNS_FAILED") from value
    addresses = list(dict.fromkeys(item[4][0] for item in value))
    if not addresses:
        raise RequestError("DNS returned no addresses", code="NETWORK_DNS_FAILED")
    return {"host": host, "port": port, "addresses": addresses}


def _tcp(host: str, port: int, timeout: float) -> dict[str, Any]:
    addresses = _dns(host, port, timeout)["addresses"]
    # Check the first resolved address. HTTPS separately uses the OS address
    # selection and configured proxy; this is a direct-connect diagnostic.
    address = addresses[0]
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect((address, port))
        return {"host": host, "port": port, "address": address, "connected": True}
    except OSError as exc:
        raise RequestError("direct TCP connection failed", code="NETWORK_TCP_FAILED") from exc


def _https(
    settings: SDKSettings,
    url: str,
    *,
    tls12_only: bool = False,
    tls_profile: str = TLS_DEFAULT,
    direct: bool = False,
    verify_certificate: bool = True,
    context_path: Path | None = None,
    **capture: Any,
) -> dict[str, Any]:
    if tls12_only and tls_profile == TLS_DEFAULT:
        tls_profile = TLS12
    session, trust_mode = create_requests_session(
        ca_bundle=settings.ca_bundle,
        tls_profile=tls_profile,
        verify_certificate=verify_certificate,
    )
    # Preserve proxy discovery, but do not silently borrow credentials from
    # .netrc for an explicitly unauthenticated reachability check.
    session.auth = _NoAuthentication()
    # Keep the same effective CA source when only the proxy route changes.
    verify = (
        session.merge_environment_settings(url, {}, None, settings.requests_verify, None)["verify"]
        if verify_certificate
        else False
    )
    session.trust_env = not direct
    context = {
        "url": url,
        "trust_mode": trust_mode,
        "tls_profile": tls_profile,
        "tls_protocol": "TLSv1.2" if tls_profile != TLS_DEFAULT else "DEFAULT",
        "route": "DIRECT" if direct else "SYSTEM",
        "effective_proxy_present": bool(
            select_proxy(url, get_environ_proxies(url)) if not direct else None
        ),
        "certificate_verification": verify_certificate,
        "diagnostic_only": not verify_certificate,
        "portal_credentials_sent": False,
    }
    adapter = session.get_adapter(url)
    ssl_context = getattr(adapter, "ssl_context", None)
    if ssl_context is not None:
        adapter.direct = direct
        context["offered_ciphers"] = [
            item["name"]
            for item in ssl_context.get_ciphers()
            if tls_profile == TLS_DEFAULT or item["protocol"] != "TLSv1.3"
        ]
        context["security_level"] = ssl_context.security_level
    transport = SafeSessionTransport(
        policy=replace(
            settings.request_policy,
            max_attempts=1,
            connect_timeout_seconds=min(settings.request_policy.connect_timeout_seconds, 10.0),
            read_timeout_seconds=min(settings.request_policy.read_timeout_seconds, 15.0),
        ),
        verify=verify,
        session=session,
        **capture,
    )
    try:
        if context_path is not None:
            write_json_atomic(context_path, context)
        negotiated = {}
        try:
            response = transport.request("GET", url, allow_redirects=False)
        except RequestError as exc:
            if exc.status_code is None:
                raise
            # A 401/403/404 on an unauthenticated base URL still demonstrates
            # working HTTPS. Authenticated readiness is tested separately.
            status = exc.status_code
        else:
            status = response.status_code
            negotiated = getattr(response, "tls_details", {})
            response.close()
        return {
            **context,
            "http_status": status,
            "reachable": True,
            "negotiated_tls": negotiated,
        }
    finally:
        transport.close()


class _NoAuthentication(AuthBase):
    def __call__(self, request: Any) -> Any:
        return request
