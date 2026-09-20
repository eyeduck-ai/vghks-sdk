from __future__ import annotations

from collections.abc import Sequence

from ..adapters.protocols import AuthAdapterProtocol
from ..models import AuthCheckReport


class AuthService:
    def __init__(self, adapter: AuthAdapterProtocol) -> None:
        self._adapter = adapter

    def login(self) -> None:
        self._adapter.login()

    def check(self, only: Sequence[str] | None = None) -> AuthCheckReport:
        return self._adapter.auth_check(only)
