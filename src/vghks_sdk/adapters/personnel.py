"""Read-only personnel directory over the shared Portal SSO session."""

from __future__ import annotations

from urllib.parse import urlencode, urlsplit

from ..core.errors import AuthExpiredError, ConfigurationError
from ..core.operations import operation_spec
from ..models.personnel import PersonnelFilter, PersonnelOptions, PersonnelRecord
from ..parsing.personnel import parse_personnel_options, parse_personnel_records
from ..runtime import SDKRuntime


class PersonnelAdapter:
    def __init__(self, runtime: SDKRuntime) -> None:
        self.runtime = runtime

    def _read(self, key: str, **kwargs: object) -> str:
        spec = operation_spec("personnel." + key)
        base = self.runtime.settings.personnel_base_url.rstrip("/")
        response = self.runtime.request_response(
            spec,
            base + spec.path.removeprefix("/DDPortal"),
            allow_redirects=False,
            headers={
                "Referer": base + "/DRQuerySql.jsp",
                "Origin": f"{urlsplit(base).scheme}://{urlsplit(base).netloc}",
                **(
                    {"Content-Type": "application/x-www-form-urlencoded"} if key == "search" else {}
                ),
            },
            **kwargs,
        )
        if 300 <= response.status_code < 400:
            raise AuthExpiredError("personnel session redirected", app="personnel")
        return self.runtime.transport.text(response)

    def get_options(self) -> PersonnelOptions:
        spec = operation_spec("personnel.options")
        return self.runtime.execute(
            spec, lambda: parse_personnel_options(self._read("options")), operation_name=spec.key
        )

    def search(self, filter: PersonnelFilter) -> list[PersonnelRecord]:
        if not isinstance(filter, PersonnelFilter):
            raise ConfigurationError("personnel search requires PersonnelFilter")
        # The query form is BIG5; requests' default UTF-8 encoding breaks Chinese
        # name searches and the submit value even though result pages are UTF-8.
        try:
            body = urlencode(filter.to_form(), encoding="cp950", errors="strict").encode("ascii")
        except UnicodeEncodeError as exc:
            raise ConfigurationError(
                "personnel query cannot be encoded as Big5", code="PERSONNEL_ENCODING_INVALID"
            ) from exc
        spec = operation_spec("personnel.search")

        def operation() -> list[PersonnelRecord]:
            options = parse_personnel_options(self._read("options"))
            for value, choices in ((filter.title, options.titles), (filter.unit, options.units)):
                if value and value not in {item.value for item in choices}:
                    raise ConfigurationError(
                        "unknown personnel option", code="PERSONNEL_OPTION_INVALID"
                    )
            return parse_personnel_records(self._read("search", data=body))

        return self.runtime.execute(spec, operation, operation_name=spec.key)
