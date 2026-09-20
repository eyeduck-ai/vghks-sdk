"""Serializable configuration for the closed-network live-test runner.

Credentials are deliberately not part of this model.  Configuration files are
safe to reuse only in that narrow sense: the resulting live-test bundle is
still intentionally unredacted and highly sensitive.
"""

from __future__ import annotations

import json
import os
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ..core.config import RequestPolicy, SDKSettings
from ..core.errors import ConfigurationError
from ..models import ReviewCaseFilter, SurgeryCaseFilter, VisitFilter, to_jsonable
from ..queries import query_spec
from ..search import SoapSearch
from .defaults import default_test_mrn

LIVE_CONFIG_SCHEMA_VERSION = 6
_ENDPOINT_FIELDS = (
    "portal_base_url",
    "prq_base_url",
    "sectord_base_url",
    "webmaas_base_url",
    "oppl_base_url",
    "audit_base_url",
    "mis_base_url",
    "review_base_url",
)
_CREDENTIAL_KEYS = {
    "national_id",
    "earnings_credentials",
    "earnings_password",
    "credential",
    "credentials",
    "password",
    "portal_credentials",
    "username",
    "user",
}
_TOP_LEVEL_KEYS = {
    "test_mrn",
    "schema_version",
    "weekly_opd_soap",
    "weekly_opd_end",
    "profile",
    "output_root",
    "visit_filter",
    "soap_search",
    "doctor_card",
    "opd_date",
    "range_start",
    "range_end",
    "include_surgery",
    "include_unsigned",
    "include_earnings",
    "visit_date",
    "order_date",
    "download_assets",
    "asset_terms",
    "all_matching_orders",
    "request_policy",
    "ca_bundle",
    "endpoint_overrides",
    "only_operations",
    "max_cases",
    "max_items",
    "allow_unverified_tls",
    "surgery_query",
    "review_query",
}


@dataclass(frozen=True, slots=True)
class LiveTestConfig:
    """Validated, credential-free live-test configuration."""

    profile: str = "full"
    test_mrn: str = field(default_factory=default_test_mrn)
    output_root: Path | None = None
    visit_filter: VisitFilter = field(
        default_factory=lambda: VisitFilter(section_name_contains=("眼科",))
    )
    soap_search: SoapSearch | None = None
    doctor_card: str | None = None
    opd_date: date | None = None
    range_start: date | None = None
    range_end: date | None = None
    include_surgery: bool = False
    include_unsigned: bool = False
    include_earnings: bool = False
    visit_date: date | None = None
    order_date: date | None = None
    download_assets: bool = True
    asset_terms: tuple[str, ...] = ("Microsonography", "DBR")
    all_matching_orders: bool = False
    request_policy: RequestPolicy = field(default_factory=RequestPolicy)
    ca_bundle: Path | None = None
    endpoint_overrides: Mapping[str, str] = field(default_factory=dict)
    only_operations: tuple[str, ...] = ()
    max_cases: int | None = None
    max_items: int | None = None
    # Match the SDK policy; callers can require verified HTTPS explicitly.
    allow_unverified_tls: bool = True
    weekly_opd_soap: bool = True
    weekly_opd_end: date | None = None
    surgery_query: Mapping[str, Any] = field(default_factory=dict)
    review_query: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.test_mrn, str) or not self.test_mrn.strip():
            raise ConfigurationError("test_mrn must be nonempty", code="TEST_MRN_REQUIRED")
        object.__setattr__(self, "test_mrn", self.test_mrn.strip())
        if not isinstance(self.review_query, Mapping) or set(self.review_query) - {
            "doctor_card",
            "department",
            "mrn",
            "verify_code",
            "apply_mode",
            "start_date",
            "end_date",
        }:
            raise ConfigurationError("invalid review_query configuration")
        review = dict(self.review_query)
        for key in ("start_date", "end_date"):
            if key in review:
                review[key] = _date_value(review[key], key)
        validation = review or {"doctor_card": "CONFIGURATION-ONLY"}
        ReviewCaseFilter(**validation)
        object.__setattr__(self, "review_query", review)
        if not isinstance(self.surgery_query, Mapping):
            raise ConfigurationError("surgery_query must be an object")
        allowed_surgery = {
            "periods",
            "surgeon_card",
            "supervising_card",
            "assistant_cards",
            "department",
            "procedure_code",
        }
        if set(self.surgery_query) - allowed_surgery:
            raise ConfigurationError("unknown surgery_query filter")
        surgery = dict(self.surgery_query)
        periods = surgery.pop("periods", ("1M",))
        if (
            not isinstance(periods, (tuple, list))
            or not periods
            or not all(isinstance(p, str) for p in periods)
        ):
            raise ConfigurationError("surgery periods must be a nonempty array")
        validation = dict(surgery)
        if not isinstance(validation.get("assistant_cards", ()), (list, tuple)):
            raise ConfigurationError("assistant_cards must be an array")
        if not any(
            (
                validation.get("surgeon_card"),
                validation.get("supervising_card"),
                *validation.get("assistant_cards", ()),
            )
        ) and ("surgeon_card" not in validation or isinstance(validation["surgeon_card"], str)):
            validation["surgeon_card"] = "CONFIGURATION-ONLY"
        for period in periods:
            SurgeryCaseFilter(**validation, period=period)
        object.__setattr__(
            self, "surgery_query", {**surgery, "periods": tuple(dict.fromkeys(periods))}
        )
        profile = str(self.profile).strip().lower()
        object.__setattr__(self, "profile", profile)
        if profile not in {
            "auth",
            "atomic",
            "comprehensive",
            "ophthalmology",
            "visits",
            "core",
            "full",
        }:
            raise ConfigurationError(
                "live-test profile must be auth, atomic, comprehensive, ophthalmology, visits, core or full"
            )
        if self.max_cases is None:
            object.__setattr__(
                self,
                "max_cases",
                6
                if profile in {"comprehensive", "ophthalmology"}
                else 3
                if profile == "visits"
                else 1,
            )
        if self.max_items is None:
            object.__setattr__(
                self, "max_items", 8 if profile in {"comprehensive", "ophthalmology"} else 2
            )
        if type(self.allow_unverified_tls) is not bool:
            raise ConfigurationError("allow_unverified_tls must be a boolean")
        if type(self.weekly_opd_soap) is not bool:
            raise ConfigurationError("weekly_opd_soap must be a boolean")
        if type(self.include_earnings) is not bool:
            raise ConfigurationError("include_earnings must be a boolean")
        if self.include_earnings and self.profile not in {"atomic", "comprehensive"}:
            raise ConfigurationError("earnings tests require atomic or comprehensive profile")
        if self.weekly_opd_end is not None and not isinstance(self.weekly_opd_end, date):
            raise ConfigurationError("weekly_opd_end must be a date")
        if isinstance(self.only_operations, str) or not isinstance(
            self.only_operations, (tuple, list)
        ):
            raise ConfigurationError("only_operations must be an array")
        if not all(isinstance(key, str) for key in self.only_operations):
            raise ConfigurationError("only_operations must contain query names")
        operations = tuple(dict.fromkeys(self.only_operations))
        for key in operations:
            query_spec(key)
        object.__setattr__(self, "only_operations", operations)
        if operations and profile not in {"atomic", "comprehensive"}:
            raise ConfigurationError("only_operations requires atomic or comprehensive profile")
        for limit in (self.max_cases, self.max_items):
            if type(limit) is not int or not 1 <= limit <= 100:
                raise ConfigurationError("test limits must be integers between 1 and 100")
        if not isinstance(self.visit_filter, VisitFilter):
            raise ConfigurationError("live-test requires a valid VisitFilter")
        if self.soap_search is not None and not isinstance(self.soap_search, SoapSearch):
            raise ConfigurationError("live-test SOAP search is invalid")
        if (
            self.profile in {"auth", "atomic", "comprehensive", "ophthalmology", "visits"}
            and self.soap_search is not None
        ):
            raise ConfigurationError("SOAP search requires the core or full profile")
        if self.visit_date is not None and not isinstance(self.visit_date, date):
            raise ConfigurationError("live-test visit_date must be a date")
        if self.order_date is not None and not isinstance(self.order_date, date):
            raise ConfigurationError("live-test order_date must be a date")
        terms: list[str] = []
        seen_terms: set[str] = set()
        for value in self.asset_terms:
            term = unicodedata.normalize("NFKC", str(value)).strip()
            identity = term.casefold()
            if not term:
                raise ConfigurationError("live-test asset terms must not be blank")
            if identity not in seen_terms:
                seen_terms.add(identity)
                terms.append(term)
        if self.download_assets and not terms:
            raise ConfigurationError("live-test asset download requires search terms")
        if self.all_matching_orders and not self.download_assets:
            raise ConfigurationError("live-test all_matching_orders requires asset download")
        object.__setattr__(self, "asset_terms", tuple(terms))
        self.request_policy.validate()

        card = str(self.doctor_card or "").strip()
        object.__setattr__(self, "doctor_card", card or None)
        if (self.doctor_card is None) != (self.opd_date is None):
            raise ConfigurationError("live-test doctor_card and opd_date must be provided together")
        if (self.range_start is None) != (self.range_end is None):
            raise ConfigurationError(
                "live-test range_start and range_end must be provided together"
            )
        if (
            self.range_start is not None
            and self.range_end is not None
            and self.range_end < self.range_start
        ):
            raise ConfigurationError("live-test range end precedes its start")
        if self.profile in {"full", "comprehensive"} and self.range_start is None:
            object.__setattr__(self, "range_start", self.opd_date)
            object.__setattr__(self, "range_end", self.opd_date)

        normalized_endpoints: dict[str, str] = {}
        for key, value in self.endpoint_overrides.items():
            if key not in _ENDPOINT_FIELDS:
                raise ConfigurationError("live-test endpoint override key is unknown")
            endpoint = str(value).strip().rstrip("/")
            if not endpoint.lower().startswith("https://"):
                raise ConfigurationError("live-test endpoint overrides must use HTTPS")
            normalized_endpoints[key] = endpoint
        object.__setattr__(self, "endpoint_overrides", normalized_endpoints)

        if self.output_root is not None:
            object.__setattr__(self, "output_root", Path(self.output_root).expanduser())
        if self.ca_bundle is not None:
            object.__setattr__(self, "ca_bundle", Path(self.ca_bundle).expanduser())

    def with_default_doctor(self, username: str) -> LiveTestConfig:
        """Use the operator's login card for first-run doctor queries."""
        needs_doctor = (
            self.profile == "comprehensive"
            or self.include_surgery
            or self.include_unsigned
            or any(query_spec(key).scope.startswith("doctor_") for key in self.only_operations)
        )
        if self.doctor_card or not needs_doctor or not username.strip():
            return self
        today = date.today()
        return replace(
            self,
            doctor_card=username.strip(),
            opd_date=today,
            range_start=self.range_start
            or (today - timedelta(days=29) if self.profile == "comprehensive" else today),
            range_end=self.range_end or today,
        )

    def validate_for_execution(self) -> LiveTestConfig:
        if (
            self.profile == "atomic"
            and self.doctor_card is None
            and any(
                query_spec(key).scope.startswith("doctor_")
                and not (key == "oppl_records.cases" and self.has_surgery_physician())
                and not (key == "review.cases" and self.review_query)
                for key in self.only_operations
            )
        ):
            raise ConfigurationError("selected doctor queries require doctor_card and opd_date")
        if not self.download_assets and any(
            query_spec(key).scope in {"image", "pdf", "surgery_pdf"} for key in self.only_operations
        ):
            raise ConfigurationError("selected binary queries require download_assets")
        if (self.include_surgery or self.include_unsigned) and self.doctor_card is None:
            raise ConfigurationError(
                "live-test optional surgery/audit checks require doctor_card and opd_date"
            )
        if self.range_start is not None and self.doctor_card is None:
            raise ConfigurationError(
                "live-test optional date range requires doctor_card and opd_date"
            )
        return self

    def has_surgery_physician(self) -> bool:
        return any(
            (
                self.surgery_query.get("surgeon_card"),
                self.surgery_query.get("supervising_card"),
                *self.surgery_query.get("assistant_cards", ()),
            )
        )

    def to_safe_dict(self) -> dict[str, Any]:
        """Return the persisted configuration; it can never contain credentials."""

        return {
            "test_mrn": self.test_mrn,
            "surgery_query": to_jsonable(self.surgery_query),
            "review_query": to_jsonable(self.review_query),
            "schema_version": LIVE_CONFIG_SCHEMA_VERSION,
            "profile": self.profile,
            "only_operations": list(self.only_operations),
            "max_cases": self.max_cases,
            "max_items": self.max_items,
            "allow_unverified_tls": self.allow_unverified_tls,
            "weekly_opd_soap": self.weekly_opd_soap,
            "weekly_opd_end": self.weekly_opd_end.isoformat() if self.weekly_opd_end else None,
            "output_root": str(self.output_root) if self.output_root else None,
            "visit_filter": to_jsonable(self.visit_filter),
            "soap_search": to_jsonable(self.soap_search),
            "doctor_card": self.doctor_card,
            "opd_date": self.opd_date.isoformat() if self.opd_date else None,
            "range_start": self.range_start.isoformat() if self.range_start else None,
            "range_end": self.range_end.isoformat() if self.range_end else None,
            "include_surgery": self.include_surgery,
            "include_unsigned": self.include_unsigned,
            "include_earnings": self.include_earnings,
            "visit_date": self.visit_date.isoformat() if self.visit_date else None,
            "order_date": self.order_date.isoformat() if self.order_date else None,
            "download_assets": self.download_assets,
            "asset_terms": list(self.asset_terms),
            "all_matching_orders": self.all_matching_orders,
            "request_policy": {
                "min_delay_seconds": self.request_policy.min_delay_seconds,
                "max_delay_seconds": self.request_policy.max_delay_seconds,
                "max_attempts": self.request_policy.max_attempts,
                "connect_timeout_seconds": self.request_policy.connect_timeout_seconds,
                "read_timeout_seconds": self.request_policy.read_timeout_seconds,
                "backoff_base_seconds": self.request_policy.backoff_base_seconds,
                "max_retry_after_seconds": self.request_policy.max_retry_after_seconds,
            },
            "ca_bundle": str(self.ca_bundle) if self.ca_bundle else None,
            "endpoint_overrides": dict(self.endpoint_overrides),
        }

    def build_settings(self) -> SDKSettings:
        """Apply CA/endpoint overrides on top of the environment-aware SDK settings."""

        defaults = SDKSettings()
        values = {
            field_name: os.getenv(
                "VGHKS_" + field_name.removesuffix("_base_url").upper() + "_BASE_URL",
                getattr(defaults, field_name),
            )
            for field_name in _ENDPOINT_FIELDS
        }
        values.update(self.endpoint_overrides)
        ca_bundle = self.ca_bundle or (
            Path(os.environ["VGHKS_CA_BUNDLE"]).expanduser()
            if os.getenv("VGHKS_CA_BUNDLE")
            else None
        )
        if ca_bundle is not None:
            if not ca_bundle.is_file():
                raise ConfigurationError(
                    "live-test CA bundle is not a readable file",
                    code="CA_BUNDLE_NOT_FOUND",
                    operation="config.ca_bundle",
                    app="local",
                )
            ca_value: str | None = str(ca_bundle.resolve())
        else:
            ca_value = None
        settings = SDKSettings(
            **values,
            ca_bundle=ca_value,
            request_policy=self.request_policy,
            allow_unverified_tls=self.allow_unverified_tls,
        )
        profiles = dict(settings.profiles)
        for key, prefix in {"prq": "PRQ", "sectord": "SECTORD", "audit": "AUDIT"}.items():
            current = profiles[key]
            profiles[key] = replace(
                current,
                app_dn=os.getenv(f"VGHKS_{prefix}_APP_DN", current.app_dn),
                app_ou=os.getenv(f"VGHKS_{prefix}_APP_OU", current.app_ou),
                app_description=os.getenv(
                    f"VGHKS_{prefix}_APP_DESCRIPTION", current.app_description
                ),
            )
        object.__setattr__(settings, "profiles", profiles)
        return settings


def load_live_test_config(path: Path) -> dict[str, Any]:
    """Load and structurally validate a credential-free JSON configuration."""

    config_path = path.expanduser().resolve()
    if not config_path.is_file():
        raise ConfigurationError(
            "live-test configuration file does not exist",
            code="LIVE_CONFIG_NOT_FOUND",
            operation="config.live_test",
            app="local",
        )
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError(
            "live-test configuration is not valid UTF-8 JSON",
            code="LIVE_CONFIG_INVALID_JSON",
            operation="config.live_test",
            app="local",
            cause_type=exc.__class__.__name__,
        ) from exc
    if not isinstance(payload, dict):
        raise ConfigurationError("live-test configuration must be a JSON object")
    _reject_credential_keys(payload)
    unknown = set(payload) - _TOP_LEVEL_KEYS
    if unknown:
        raise ConfigurationError("live-test configuration contains unknown fields")
    if payload.get("schema_version", LIVE_CONFIG_SCHEMA_VERSION) not in {2, 3, 4, 5, 6}:
        raise ConfigurationError(
            "unsupported live-test configuration schema version",
            code="LIVE_CONFIG_SCHEMA_UNSUPPORTED",
            operation="config.live_test",
            app="local",
        )
    base = config_path.parent
    for key in ("output_root", "ca_bundle"):
        raw = payload.get(key)
        if raw and not Path(str(raw)).is_absolute():
            payload[key] = str((base / str(raw)).resolve())
    return payload


def resolve_live_test_config(
    *,
    cli_values: Mapping[str, Any] | None = None,
    json_values: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    default_output_root: Path | None = None,
) -> LiveTestConfig:
    """Resolve CLI > JSON > environment > defaults and return a typed config."""

    cli = dict(cli_values or {})
    file_values = dict(json_values or {})
    _reject_credential_keys(file_values)
    env = environ if environ is not None else os.environ
    defaults: dict[str, Any] = {
        "profile": "full",
        "output_root": default_output_root,
        "visit_filter": {"section_name_contains": ["眼科"]},
        "soap_search": None,
        "download_assets": True,
        "asset_terms": ["Microsonography", "DBR"],
        "all_matching_orders": False,
        "request_policy": {},
        "endpoint_overrides": {},
    }
    environment_values = _environment_values(env)
    merged = _deep_merge(defaults, environment_values)
    merged = _deep_merge(merged, file_values)
    merged = _deep_merge(merged, cli)
    return _config_from_mapping(merged)


def _config_from_mapping(values: Mapping[str, Any]) -> LiveTestConfig:
    visit = values.get("visit_filter") or {}
    if isinstance(visit, VisitFilter):
        visit_filter = visit
    elif isinstance(visit, Mapping):
        allowed_visit = {
            "section_name_contains",
            "section_codes",
            "all_sections",
            "case_types",
            "start_date",
            "end_date",
            "doctor_names",
            "doctor_name_contains",
            "doctor_cards",
        }
        if set(visit) - allowed_visit:
            raise ConfigurationError("live-test visit_filter contains unknown fields")
        visit_filter = VisitFilter(
            section_name_contains=tuple(visit.get("section_name_contains", ()) or ()),
            section_codes=tuple(visit.get("section_codes", ()) or ()),
            all_sections=bool(visit.get("all_sections", False)),
            case_types=tuple(visit.get("case_types", ("O",)) or ("O",)),
            start_date=_date_value(visit.get("start_date"), "visit start date"),
            end_date=_date_value(visit.get("end_date"), "visit end date"),
            doctor_names=tuple(visit.get("doctor_names", ()) or ()),
            doctor_name_contains=tuple(visit.get("doctor_name_contains", ()) or ()),
            doctor_cards=tuple(visit.get("doctor_cards", ()) or ()),
        )
    else:
        raise ConfigurationError("live-test visit_filter must be an object")

    search = values.get("soap_search")
    if search is None or isinstance(search, SoapSearch):
        soap_search = search
    elif isinstance(search, Mapping):
        allowed_search = {"pattern", "mode", "ignore_case", "multiline", "dotall"}
        if set(search) - allowed_search:
            raise ConfigurationError("live-test soap_search contains unknown fields")
        soap_search = SoapSearch(
            pattern=str(search.get("pattern", "")),
            mode=str(search.get("mode", "literal")),
            ignore_case=bool(search.get("ignore_case", False)),
            multiline=bool(search.get("multiline", False)),
            dotall=bool(search.get("dotall", False)),
        )
    else:
        raise ConfigurationError("live-test soap_search must be an object or null")

    policy_value = values.get("request_policy") or {}
    if isinstance(policy_value, RequestPolicy):
        policy = policy_value
    elif isinstance(policy_value, Mapping):
        allowed = {
            "min_delay_seconds",
            "max_delay_seconds",
            "max_attempts",
            "connect_timeout_seconds",
            "read_timeout_seconds",
            "backoff_base_seconds",
            "max_retry_after_seconds",
        }
        if set(policy_value) - allowed:
            raise ConfigurationError("live-test request_policy contains unknown fields")
        policy = RequestPolicy(**dict(policy_value)).validate()
    else:
        raise ConfigurationError("live-test request_policy must be an object")

    endpoints = values.get("endpoint_overrides") or {}
    if not isinstance(endpoints, Mapping):
        raise ConfigurationError("live-test endpoint_overrides must be an object")
    asset_terms_value = values.get("asset_terms", ("Microsonography", "DBR"))
    if isinstance(asset_terms_value, str) or not isinstance(asset_terms_value, (list, tuple)):
        raise ConfigurationError("live-test asset_terms must be an array")
    return LiveTestConfig(
        test_mrn=values.get("test_mrn", default_test_mrn()),
        profile=str(values.get("profile", "full")),
        output_root=Path(str(values["output_root"])) if values.get("output_root") else None,
        visit_filter=visit_filter,
        soap_search=soap_search,
        doctor_card=values.get("doctor_card"),
        opd_date=_date_value(values.get("opd_date"), "OPD date"),
        range_start=_date_value(values.get("range_start"), "range start"),
        range_end=_date_value(values.get("range_end"), "range end"),
        include_surgery=bool(values.get("include_surgery", False)),
        include_unsigned=bool(values.get("include_unsigned", False)),
        include_earnings=values.get("include_earnings", False),
        visit_date=_date_value(values.get("visit_date"), "record visit date"),
        order_date=_date_value(values.get("order_date"), "order date"),
        download_assets=bool(values.get("download_assets", True)),
        asset_terms=tuple(asset_terms_value or ()),
        all_matching_orders=bool(values.get("all_matching_orders", False)),
        request_policy=policy,
        ca_bundle=Path(str(values["ca_bundle"])) if values.get("ca_bundle") else None,
        endpoint_overrides={str(key): str(value) for key, value in endpoints.items()},
        only_operations=values.get("only_operations", ()),
        max_cases=values.get("max_cases"),
        max_items=values.get("max_items"),
        allow_unverified_tls=values.get("allow_unverified_tls", True),
        weekly_opd_soap=values.get("weekly_opd_soap", True),
        weekly_opd_end=_date_value(values.get("weekly_opd_end"), "weekly OPD end date"),
        surgery_query=values.get("surgery_query", {}),
        review_query=values.get("review_query", {}),
    )


def _environment_values(env: Mapping[str, str]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    if env.get("VGHKS_TEST_MRN"):
        values["test_mrn"] = env["VGHKS_TEST_MRN"]
    if env.get("VGHKS_LIVE_PROFILE"):
        values["profile"] = env["VGHKS_LIVE_PROFILE"]
    if env.get("VGHKS_LIVE_OUTPUT_ROOT"):
        values["output_root"] = env["VGHKS_LIVE_OUTPUT_ROOT"]
    if env.get("VGHKS_CA_BUNDLE"):
        values["ca_bundle"] = env["VGHKS_CA_BUNDLE"]
    endpoints: dict[str, str] = {}
    for field_name in _ENDPOINT_FIELDS:
        env_name = "VGHKS_" + field_name.removesuffix("_base_url").upper() + "_BASE_URL"
        if env.get(env_name):
            endpoints[field_name] = env[env_name]
    if endpoints:
        values["endpoint_overrides"] = endpoints
    policy: dict[str, Any] = {}
    conversions = {
        "VGHKS_DELAY_MIN": ("min_delay_seconds", float),
        "VGHKS_DELAY_MAX": ("max_delay_seconds", float),
        "VGHKS_MAX_ATTEMPTS": ("max_attempts", int),
        "VGHKS_CONNECT_TIMEOUT": ("connect_timeout_seconds", float),
        "VGHKS_READ_TIMEOUT": ("read_timeout_seconds", float),
    }
    try:
        for env_name, (field_name, converter) in conversions.items():
            if env.get(env_name):
                policy[field_name] = converter(env[env_name])
    except ValueError as exc:
        raise ConfigurationError("live-test environment has an invalid numeric value") from exc
    if policy:
        values["request_policy"] = policy
    return values


def _reject_credential_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).strip().casefold() in _CREDENTIAL_KEYS:
                raise ConfigurationError(
                    "live-test configuration must not contain usernames or passwords"
                )
            _reject_credential_keys(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_credential_keys(item)


def _date_value(value: Any, label: str) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ConfigurationError(f"live-test {label} must be an ISO date") from exc


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _deep_merge(result[key], value)  # type: ignore[arg-type]
        else:
            result[key] = value
    return result
