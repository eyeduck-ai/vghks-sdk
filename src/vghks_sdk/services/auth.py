from __future__ import annotations

from collections.abc import Sequence

from ..adapters.protocols import AuthAdapterProtocol
from ..models import AuthCheckReport, PasswordStatus


class AuthService:
    def __init__(self, adapter: AuthAdapterProtocol) -> None:
        self._adapter = adapter

    def login(self) -> None:
        self._adapter.login()

    def check(self, only: Sequence[str] | None = None) -> AuthCheckReport:
        return self._adapter.auth_check(only)

    @property
    def password_status(self) -> PasswordStatus:
        """Last observed policy notice; reading this property performs no HTTP."""

        return self._adapter.password_status
