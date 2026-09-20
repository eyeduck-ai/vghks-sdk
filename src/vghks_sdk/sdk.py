"""Public SDK facade over shared stateful services."""

from __future__ import annotations

from .adapters import AuditAdapter, OpplAdapter, PrqAdapter, WebMaasAdapter
from .adapters.auth import AuthenticationAdapter
from .adapters.earnings import EarningsAdapter
from .adapters.review import ReviewAdapter
from .adapters.surgery_cases import SurgeryCasesAdapter
from .core.capture import RawCaptureSink
from .core.config import PortalCredentials, SDKSettings
from .core.diagnostics import DiagnosticRecorder
from .core.errors import ConfigurationError
from .core.readiness import AUTH_CHECK_REGISTRY
from .core.tls import create_requests_session, mount_tls_profile
from .core.transport import SafeSessionTransport
from .queries import Queries
from .runtime import SDKRuntime
from .services import (
    AuditService,
    AuthService,
    MedicationsService,
    OpdService,
    OrdersService,
    PatientsService,
    RecordsService,
    SurgeryService,
)
from .services.earnings import EarningsService
from .services.reviews import ReviewsService


class VghksSDK:
    def __init__(
        self,
        *,
        settings: SDKSettings,
        credentials: PortalCredentials,
        transport: SafeSessionTransport | None = None,
        diagnostics: DiagnosticRecorder | None = None,
        raw_capture: RawCaptureSink | None = None,
    ) -> None:
        if transport is None:
            session, _trust_mode = create_requests_session(ca_bundle=settings.ca_bundle)
            resolved_transport = SafeSessionTransport(
                policy=settings.request_policy,
                verify=settings.requests_verify,
                session=session,
                diagnostics=diagnostics,
                raw_capture=raw_capture,
            )
        else:
            resolved_transport = transport
        auth_adapter = AuthenticationAdapter(
            settings=settings,
            credentials=credentials,
            transport=resolved_transport,
        )
        self._runtime = SDKRuntime(
            settings=settings,
            transport=resolved_transport,
            auth=auth_adapter,
            diagnostics=diagnostics,
            raw_capture=raw_capture,
        )
        webmaas = WebMaasAdapter(self._runtime)
        prq = PrqAdapter(self._runtime)
        oppl = OpplAdapter(self._runtime)
        audit = AuditAdapter(self._runtime)
        self.auth = AuthService(self._runtime)
        self.patients = PatientsService(webmaas)
        self.opd = OpdService(prq)
        self.records = RecordsService(prq)
        self.orders = OrdersService(prq)
        self.medications = MedicationsService(prq)
        self.surgery = SurgeryService(oppl, SurgeryCasesAdapter(self._runtime))
        self.audit = AuditService(audit)
        self.earnings = EarningsService(EarningsAdapter(self._runtime))
        self.reviews = ReviewsService(ReviewAdapter(self._runtime))
        self.queries = Queries(self)

    def configure_connection(
        self,
        app: str,
        *,
        tls_profile: str,
        direct: bool = False,
        verify_certificate: bool = True,
    ) -> None:
        """Apply an explicit preflight choice to one configured service origin.

        Call before authentication. No request is retried and no login cookies
        are replaced. Services on the same host/port share the selected mode.
        """
        if app not in {spec.key for spec in AUTH_CHECK_REGISTRY} | {"mis"}:
            raise ConfigurationError("unknown service", code="TLS_TARGET_INVALID")
        transport = self._runtime.transport
        with self._runtime.operation_lock, transport._lock:
            mount_tls_profile(
                transport.session,
                getattr(self._runtime.settings, f"{app}_base_url"),
                ca_bundle=self._runtime.settings.ca_bundle,
                tls_profile=tls_profile,
                direct=direct,
                verify_certificate=verify_certificate,
            )

    def close(self) -> None:
        self._runtime.close()

    def __enter__(self) -> VghksSDK:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
