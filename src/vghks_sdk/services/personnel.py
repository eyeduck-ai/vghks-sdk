from __future__ import annotations

from ..core.errors import ParseError
from ..extension_protocols import PersonnelProtocol
from ..models.personnel import (
    PersonnelFilter,
    PersonnelOptions,
    PersonnelRecord,
    personnel_employee_id,
)


class PersonnelService:
    def __init__(self, adapter: PersonnelProtocol) -> None:
        self._adapter = adapter

    def get_options(self) -> PersonnelOptions:
        """Read current title and unit codes/labels from the directory form."""
        return self._adapter.get_options()

    def search(self, filter: PersonnelFilter) -> list[PersonnelRecord]:
        """Submit combined server-side name, employee, title and unit criteria."""
        return self._adapter.search(filter)

    def get_by_card(self, card_no: str) -> PersonnelRecord | None:
        """Resolve an employee account (or its observed +F alias) to one exact row.

        A doctor stamp number is a separate identifier. None means no exact
        employee match; multiple matches fail instead of selecting the first.
        Use the returned name explicitly with VisitFilter.doctor_names.
        """
        employee_id = personnel_employee_id(card_no)
        candidates = self.search(PersonnelFilter(employee_id=employee_id))
        matches = [row for row in candidates if row.employee_id.upper() == employee_id]
        if len(matches) > 1:
            raise ParseError(
                "personnel employee match is ambiguous", code="PERSONNEL_MATCH_AMBIGUOUS"
            )
        return matches[0] if matches else None
