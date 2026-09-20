"""TLS trust-store selection and Requests session construction."""

from __future__ import annotations

import importlib
import platform
import ssl
from typing import Any, Final
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter

from .errors import ConfigurationError

CUSTOM_PEM: Final = "CUSTOM_PEM"
WINDOWS_SYSTEM: Final = "WINDOWS_SYSTEM"
REQUESTS_DEFAULT: Final = "REQUESTS_DEFAULT"
UNVERIFIED: Final = "UNVERIFIED"
TLS_DEFAULT: Final = "DEFAULT"
TLS12: Final = "TLS12"
TLS12_COMPAT: Final = "TLS12_COMPAT"
TLS_PROFILES: Final = (TLS_DEFAULT, TLS12, TLS12_COMPAT)

# Python 3.10 excludes static RSA and SHA1-MAC suites by default. Some older
# Java services still use these AES suites with TLS 1.2. Keep security level 2,
# modern suites first; never enable TLS 1.0/1.1, anonymous encryption, RC4 or
# 3DES. Certificate verification is a separate, explicit connection policy.
_COMPAT_CIPHERS: Final = (
    "ECDHE+AESGCM:ECDHE+CHACHA20:DHE+AESGCM:ECDHE+AES:DHE+AES:"
    "AES256-GCM-SHA384:AES128-GCM-SHA256:AES256-SHA256:AES128-SHA256:"
    "AES256-SHA:AES128-SHA:!aNULL:!eNULL:!MD5:!DSS:!PSK:!SRP:@SECLEVEL=2"
)


class TLSContextAdapter(HTTPAdapter):
    """Keep an explicit TLS policy for direct and proxied HTTPS."""

    def __init__(
        self,
        ssl_context: ssl.SSLContext,
        *,
        tls_profile: str = TLS_DEFAULT,
        direct: bool = False,
        verify_certificate: bool = True,
        **kwargs: object,
    ) -> None:
        self.ssl_context = ssl_context
        self.tls_profile = tls_profile
        self.direct = direct
        self.verify_certificate = verify_certificate
        super().__init__(**kwargs)

    def send(self, request: Any, **kwargs: Any):
        if not self.verify_certificate:
            kwargs["verify"] = False
        if self.direct:
            # Session.merge_environment_settings has already resolved the CA.
            # Change routing only for this mounted origin, retaining its cookies.
            kwargs["proxies"] = {}
        return super().send(request, **kwargs)

    def build_response(self, req: Any, resp: Any):
        response = super().build_response(req, resp)
        details = {
            "profile": self.tls_profile,
            "route": "DIRECT" if self.direct else "SYSTEM",
            "certificate_verification": self.verify_certificate,
        }
        # Capture negotiation before Requests consumes the response and releases
        # its socket. Metadata is optional; a missing socket never fails a query.
        connection = getattr(resp, "connection", None)
        sock = getattr(connection, "sock", None)
        if sock is None:
            fp = getattr(getattr(resp, "_fp", None), "fp", None)
            sock = getattr(getattr(fp, "raw", None), "_sock", None)
        try:
            if sock is not None:
                cipher = sock.cipher()
                details.update(
                    negotiated_protocol=sock.version(),
                    cipher=cipher[0] if cipher else None,
                    cipher_bits=cipher[2] if cipher else None,
                )
        except (AttributeError, OSError, ValueError):
            pass
        response.tls_details = details
        return response

    def init_poolmanager(
        self,
        connections: int,
        maxsize: int,
        block: bool = False,
        **pool_kwargs: object,
    ) -> None:
        pool_kwargs["ssl_context"] = self.ssl_context
        super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)

    def proxy_manager_for(self, proxy: str, **proxy_kwargs: object):
        proxy_kwargs.setdefault("ssl_context", self.ssl_context)
        return super().proxy_manager_for(proxy, **proxy_kwargs)


# Compatibility name for SDK clients that inspected the Windows adapter.
WindowsSystemTrustAdapter = TLSContextAdapter


def resolve_tls_trust_mode(ca_bundle: str | None) -> str:
    """Return the effective trust source without disabling certificate checks."""

    if ca_bundle:
        return CUSTOM_PEM
    if platform.system() == "Windows":
        return WINDOWS_SYSTEM
    return REQUESTS_DEFAULT


def create_requests_session(
    *,
    ca_bundle: str | None = None,
    tls12_only: bool = False,
    tls_profile: str = TLS_DEFAULT,
    verify_certificate: bool = True,
) -> tuple[requests.Session, str]:
    """Create the SDK-owned Session and apply the platform trust policy."""

    if tls12_only and tls_profile == TLS_DEFAULT:
        tls_profile = TLS12
    if tls_profile not in TLS_PROFILES:
        raise ConfigurationError("unknown TLS profile", code="TLS_PROFILE_INVALID")
    trust_mode = resolve_tls_trust_mode(ca_bundle) if verify_certificate else UNVERIFIED
    session = requests.Session()
    if trust_mode == WINDOWS_SYSTEM or tls_profile != TLS_DEFAULT or not verify_certificate:
        session.mount(
            "https://",
            make_tls_adapter(
                ca_bundle=ca_bundle,
                tls_profile=tls_profile,
                verify_certificate=verify_certificate,
            ),
        )
    return session, trust_mode


def make_tls_adapter(
    *,
    ca_bundle: str | None,
    tls_profile: str,
    direct: bool = False,
    verify_certificate: bool = True,
) -> TLSContextAdapter:
    if tls_profile not in TLS_PROFILES:
        raise ConfigurationError("unknown TLS profile", code="TLS_PROFILE_INVALID")
    if verify_certificate:
        context = (
            _windows_ssl_context()
            if resolve_tls_trust_mode(ca_bundle) == WINDOWS_SYSTEM
            else ssl.create_default_context(cafile=ca_bundle or requests.certs.where())
        )
    else:
        # A separate context/session is essential: do not mutate the verified
        # Windows truststore context or let an environment CA override this test.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    if tls_profile in (TLS12, TLS12_COMPAT):
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.maximum_version = ssl.TLSVersion.TLSv1_2
    if tls_profile == TLS12_COMPAT:
        context.set_ciphers(_COMPAT_CIPHERS)
    return TLSContextAdapter(
        context,
        tls_profile=tls_profile,
        direct=direct,
        verify_certificate=verify_certificate,
    )


def mount_tls_profile(
    session: requests.Session,
    url: str,
    *,
    ca_bundle: str | None,
    tls_profile: str,
    direct: bool = False,
    verify_certificate: bool = True,
) -> None:
    """Configure only this HTTPS origin; preserve the existing login session."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ConfigurationError("invalid TLS origin", code="TLS_ORIGIN_INVALID")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    port = parsed.port or 443
    adapter = make_tls_adapter(
        ca_bundle=ca_bundle,
        tls_profile=tls_profile,
        direct=direct,
        verify_certificate=verify_certificate,
    )
    prefixes = [f"https://{host}:{port}/"]
    if port == 443:
        prefixes.append(f"https://{host}/")
    old = {session.adapters[prefix] for prefix in prefixes if prefix in session.adapters}
    for prefix in prefixes:
        session.mount(prefix, adapter)
    for previous in old:
        if previous not in session.adapters.values():
            previous.close()


def validate_tls_trust_store(*, ca_bundle: str | None = None) -> str:
    """Validate the selected trust source without sending a network request."""

    trust_mode = resolve_tls_trust_mode(ca_bundle)
    if trust_mode == CUSTOM_PEM:
        try:
            ssl.create_default_context(cafile=ca_bundle)
        except (OSError, ssl.SSLError) as exc:
            raise ConfigurationError(
                "CA bundle is not a valid PEM trust store",
                code="CA_BUNDLE_INVALID",
                operation="config.ca_bundle",
                app="local",
                cause_type=exc.__class__.__name__,
            ) from exc
    elif trust_mode == WINDOWS_SYSTEM:
        _windows_ssl_context()
    return trust_mode


def _windows_ssl_context() -> ssl.SSLContext:
    try:
        truststore = importlib.import_module("truststore")
        context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception as exc:
        raise ConfigurationError(
            "Windows system trust store is unavailable",
            code="WINDOWS_TRUSTSTORE_UNAVAILABLE",
            operation="config.tls_trust",
            app="local",
            cause_type=exc.__class__.__name__,
        ) from exc
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise ConfigurationError(
            "Windows system trust store did not enable secure TLS verification",
            code="WINDOWS_TRUSTSTORE_UNAVAILABLE",
            operation="config.tls_trust",
            app="local",
        )
    return context
