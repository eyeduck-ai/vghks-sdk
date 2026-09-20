"""Per-SDK HTTPS choices, shared by services on the same exact origin."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit

import requests

from .config import SDKSettings
from .network_errors import network_error_code
from .tls import TLS12, TLS12_COMPAT, TLS_DEFAULT, mount_tls_profile

_APPS = ("portal", "prq", "sectord", "webmaas", "oppl", "oppl_records", "audit", "mis", "review")
_COMPAT_APPS = ("prq", "sectord", "webmaas")
_TLS_ERRORS = {"TLS_EOF", "TLS_PROTOCOL_FAILED", "NETWORK_TLS_FAILED", "TLS_VERIFY_FAILED"}


def https_origin(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"https://{host}:{parsed.port or 443}"


def preferred_tls_profile(settings: SDKSettings, app: str) -> str:
    origin = https_origin(getattr(settings, f"{app}_base_url"))
    compatible = {
        https_origin(getattr(settings, f"{key}_base_url")) for key in _COMPAT_APPS
    }
    return TLS12_COMPAT if settings.auto_tls and origin and origin in compatible else TLS_DEFAULT


@dataclass(frozen=True)
class ConnectionChoice:
    tls_profile: str
    certificate_verification: bool = True
    direct: bool = False
    confirmed: bool = False
    source: str = "PREFERRED"


class TLSConnectionManager:
    """No network on construction; choices persist for this SDK's lifetime.

    The transport serializes all calls. Unknown origins never receive automatic
    changes, and protocol failures alone never disable certificate verification.
    """

    def __init__(self, settings: SDKSettings) -> None:
        self.settings = settings
        self.origins = {
            app: origin
            for app in _APPS
            if (origin := https_origin(getattr(settings, f"{app}_base_url")))
        }
        self.choices = {
            origin: ConnectionChoice(preferred_tls_profile(settings, app))
            for app, origin in self.origins.items()
        }

    def apply(self, session: requests.Session, url: str) -> None:
        origin = https_origin(url)
        choice = self.choices.get(origin)
        if choice is None:
            return
        current = session.get_adapter(url)
        fingerprint = (id(self), choice.tls_profile, choice.certificate_verification, choice.direct)
        if getattr(current, "_sdk_connection_choice", None) == fingerprint:
            return
        mount_tls_profile(
            session,
            url,
            ca_bundle=self.settings.ca_bundle,
            tls_profile=choice.tls_profile,
            direct=choice.direct,
            verify_certificate=choice.certificate_verification,
        )
        session.get_adapter(url)._sdk_connection_choice = fingerprint

    def apply_all(self, session: requests.Session) -> None:
        for origin in self.choices:
            self.apply(session, origin + "/")

    def configure(
        self, url: str, *, tls_profile: str, direct: bool, verify_certificate: bool
    ) -> None:
        origin = https_origin(url)
        if origin in self.choices:
            self.choices[origin] = ConnectionChoice(
                tls_profile, verify_certificate, direct, True, "CONFIGURED"
            )

    def requires_confirmation(self, url: str) -> bool:
        choice = self.choices.get(https_origin(url))
        return bool(self.settings.auto_tls and choice and not choice.confirmed)

    def mark_response(self, response: requests.Response) -> None:
        for item in [*response.history, response]:
            origin = https_origin(item.url)
            if origin in self.choices:
                self.choices[origin] = replace(self.choices[origin], confirmed=True)

    def mark_failure(self, url: str, error: requests.RequestException) -> None:
        if isinstance(error, requests.exceptions.ProxyError) or network_error_code(error) not in _TLS_ERRORS:
            return
        failed_url = getattr(getattr(error, "request", None), "url", None) or url
        origin = https_origin(failed_url)
        if origin in self.choices:
            # Even when a POST cannot be retried, let the next operation confirm
            # the connection anonymously instead of reusing a stale success.
            self.choices[origin] = replace(self.choices[origin], confirmed=False)

    def recover(
        self, url: str, error: requests.RequestException, tried: set[tuple[Any, ...]]
    ) -> bool:
        if not self.settings.auto_tls or isinstance(error, requests.exceptions.ProxyError):
            return False
        failed_url = getattr(getattr(error, "request", None), "url", None) or url
        origin = https_origin(failed_url)
        current = self.choices.get(origin)
        if current is None:
            return False
        code = network_error_code(error)
        if code not in _TLS_ERRORS:
            return False
        tried.add((origin, current.tls_profile, current.certificate_verification, current.direct))
        if code == "TLS_VERIFY_FAILED":
            candidates = (
                [replace(current, certificate_verification=False)]
                if current.certificate_verification and self.settings.allow_unverified_tls
                else []
            )
        else:
            candidates = [
                replace(current, tls_profile=profile)
                for profile in (TLS12_COMPAT, TLS_DEFAULT, TLS12)
            ]
        for candidate in candidates:
            key = (origin, candidate.tls_profile, candidate.certificate_verification, candidate.direct)
            if key not in tried:
                self.choices[origin] = replace(candidate, confirmed=False, source="RECOVERED")
                return True
        return False

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {
            app: {
                "tls_profile": choice.tls_profile,
                "certificate_verification": choice.certificate_verification,
                "direct": choice.direct,
                "confirmed": choice.confirmed,
                "source": choice.source,
            }
            for app, origin in self.origins.items()
            for choice in (self.choices[origin],)
        }
