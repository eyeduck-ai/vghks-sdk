"""Configuration with safe defaults derived from endpoint structure in HARs."""

from __future__ import annotations

import os
import ssl
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from .errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class RequestPolicy:
    min_delay_seconds: float = 0.8
    max_delay_seconds: float = 1.8
    max_attempts: int = 3
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 45.0
    backoff_base_seconds: float = 1.0
    retry_statuses: frozenset[int] = frozenset({429, 502, 503, 504})
    max_retry_after_seconds: float = 60.0

    def validate(self) -> RequestPolicy:
        if self.min_delay_seconds < 0:
            raise ConfigurationError("minimum request delay cannot be negative")
        if self.max_delay_seconds < self.min_delay_seconds:
            raise ConfigurationError("maximum request delay must be >= minimum delay")
        if self.max_attempts < 1:
            raise ConfigurationError("max_attempts must be at least 1")
        if self.max_attempts > 3:
            raise ConfigurationError("max_attempts cannot exceed the safety limit of 3")
        if self.connect_timeout_seconds <= 0 or self.read_timeout_seconds <= 0:
            raise ConfigurationError("request timeouts must be positive")
        if self.backoff_base_seconds < 0 or self.max_retry_after_seconds < 0:
            raise ConfigurationError("retry delays cannot be negative")
        return self


@dataclass(frozen=True, slots=True)
class AppProfile:
    key: str
    app_dn: str
    app_ou: str
    app_description: str
    expected_base_url: str


@dataclass(frozen=True, slots=True)
class PortalCredentials:
    username: str
    password: str

    def validate(self) -> PortalCredentials:
        if not self.username.strip():
            raise ConfigurationError(
                "VGHKS_USERNAME is required",
                code="CREDENTIAL_USERNAME_MISSING",
                operation="config.credentials",
                app="portal",
            )
        if not self.password:
            raise ConfigurationError(
                "portal password is required",
                code="CREDENTIAL_PASSWORD_MISSING",
                operation="config.credentials",
                app="portal",
            )
        return self


@dataclass(frozen=True, slots=True)
class EarningsCredentials:
    """MIS second login uses national ID, not the Portal physician card."""

    national_id: str = field(repr=False)
    password: str = field(repr=False)

    def validate(self) -> EarningsCredentials:
        if not self.national_id.strip() or not self.password:
            raise ConfigurationError(
                "MIS national ID and password required", code="EARNINGS_CREDENTIALS_MISSING"
            )
        return self


@dataclass(frozen=True, slots=True)
class SDKSettings:
    portal_base_url: str = "https://portal.vghks.gov.tw"
    prq_base_url: str = "https://zwmc01p.vghks.gov.tw:4434/PRQWeb"
    sectord_base_url: str = "https://zwmc01p.vghks.gov.tw:4430/SectOrdWeb"
    webmaas_base_url: str = "https://zwac01p.vghks.gov.tw:4432/webmaas"
    oppl_base_url: str = "https://zwmc01p.vghks.gov.tw:4430/OPPLWeb"
    audit_base_url: str = "https://wmc01p.vghks.gov.tw:4439/PRQWeb"
    mis_base_url: str = "https://mis01p.vghks.gov.tw"
    review_base_url: str = "https://pck01p.vghks.gov.tw/Pck"
    ca_bundle: str | None = None
    request_policy: RequestPolicy = field(default_factory=RequestPolicy)
    profiles: Mapping[str, AppProfile] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.ca_bundle:
            ca_path = Path(self.ca_bundle).expanduser()
            if not ca_path.is_file():
                raise ConfigurationError(
                    "CA bundle does not point to a readable file",
                    code="CA_BUNDLE_NOT_FOUND",
                    operation="config.ca_bundle",
                    app="local",
                )
            try:
                ssl.create_default_context(cafile=str(ca_path))
            except (OSError, ssl.SSLError) as exc:
                raise ConfigurationError(
                    "CA bundle is not a valid PEM trust store",
                    code="CA_BUNDLE_INVALID",
                    operation="config.ca_bundle",
                    app="local",
                    cause_type=exc.__class__.__name__,
                ) from exc
        if not self.profiles:
            object.__setattr__(self, "profiles", self._default_profiles())

    def _default_profiles(self) -> Mapping[str, AppProfile]:
        return {
            **{
                key: AppProfile(
                    key=key,
                    app_dn=f"ou={ou},ou=050706_01,ou=0507_06,ou=05_07,ou=05,ou=aproot,o=prodroot",
                    app_ou=ou,
                    app_description=description,
                    expected_base_url=self.mis_base_url,
                )
                for key, ou, description in (
                    ("performance", "05070601_01", "醫療績點明細"),
                    ("payroll", "05070601_02", "專勤工作獎金明細"),
                )
            },
            "oppl": AppProfile(
                key="oppl",
                app_dn="ou=0108_04,ou=01_08,ou=01,ou=aproot,o=prodroot",
                app_ou="0108_04",
                app_description="手術排程",
                expected_base_url=self.oppl_base_url.removesuffix("/OPPLWeb"),
            ),
            "oppl_records": AppProfile(
                key="oppl_records",
                app_dn="ou=010801_04,ou=0108_01,ou=01_08,ou=01,ou=aproot,o=prodroot",
                app_ou="010801_04",
                app_description="手術紀錄查詢",
                expected_base_url=self.oppl_base_url.removesuffix("/OPPLWeb"),
            ),
            "prq": AppProfile(
                key="prq",
                app_dn="ou=010601_01,ou=0106_01,ou=01_06,ou=01,ou=aproot,o=prodroot",
                app_ou="010601_01",
                app_description="所有查詢方式",
                expected_base_url=self.prq_base_url.removesuffix("/PRQWeb"),
            ),
            "sectord": AppProfile(
                key="sectord",
                app_dn="ou=011911_06,ou=0119_11,ou=01_19,ou=01,ou=aproot,o=prodroot",
                app_ou="011911_06",
                app_description="科室功能",
                expected_base_url=self.sectord_base_url.removesuffix("/SectOrdWeb"),
            ),
            "audit": AppProfile(
                key="audit",
                app_dn="ou=010501_01,ou=0105_01,ou=01_05,ou=01,ou=aproot,o=prodroot",
                app_ou="010501_01",
                app_description="住院病歷書寫清單",
                expected_base_url=self.audit_base_url.removesuffix("/PRQWeb"),
            ),
        }

    @property
    def oppl_records_base_url(self) -> str:
        """A separate SSO capability on the same configurable OPPL origin."""
        return self.oppl_base_url

    @property
    def requests_verify(self) -> bool | str:
        return self.ca_bundle or True

    @classmethod
    def from_env(cls, *, policy: RequestPolicy | None = None) -> SDKSettings:
        defaults = cls()
        ca_bundle = os.getenv("VGHKS_CA_BUNDLE") or None
        if ca_bundle and not Path(ca_bundle).expanduser().is_file():
            raise ConfigurationError(
                "VGHKS_CA_BUNDLE does not point to a readable file",
                code="CA_BUNDLE_NOT_FOUND",
                operation="config.ca_bundle",
                app="local",
            )
        settings = cls(
            portal_base_url=os.getenv("VGHKS_PORTAL_BASE_URL", defaults.portal_base_url),
            prq_base_url=os.getenv("VGHKS_PRQ_BASE_URL", defaults.prq_base_url),
            sectord_base_url=os.getenv("VGHKS_SECTORD_BASE_URL", defaults.sectord_base_url),
            webmaas_base_url=os.getenv("VGHKS_WEBMAAS_BASE_URL", defaults.webmaas_base_url),
            oppl_base_url=os.getenv("VGHKS_OPPL_BASE_URL", defaults.oppl_base_url),
            audit_base_url=os.getenv("VGHKS_AUDIT_BASE_URL", defaults.audit_base_url),
            mis_base_url=os.getenv("VGHKS_MIS_BASE_URL", defaults.mis_base_url),
            review_base_url=os.getenv("VGHKS_REVIEW_BASE_URL", defaults.review_base_url),
            ca_bundle=ca_bundle,
            request_policy=(policy or RequestPolicy()).validate(),
        )
        overrides: dict[str, AppProfile] = dict(settings.profiles)
        env_keys = {
            "prq": "PRQ",
            "sectord": "SECTORD",
            "audit": "AUDIT",
            "oppl": "OPPL",
            "oppl_records": "OPPL_RECORDS",
        }
        for key, prefix in env_keys.items():
            current = overrides[key]
            overrides[key] = replace(
                current,
                app_dn=os.getenv(f"VGHKS_{prefix}_APP_DN", current.app_dn),
                app_ou=os.getenv(f"VGHKS_{prefix}_APP_OU", current.app_ou),
                app_description=os.getenv(
                    f"VGHKS_{prefix}_APP_DESCRIPTION", current.app_description
                ),
            )
        object.__setattr__(settings, "profiles", overrides)
        return settings
