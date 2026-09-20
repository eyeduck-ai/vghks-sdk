"""Read-only PCK case operations; writes and attachment downloads are not replayed."""

from __future__ import annotations

from ..core.errors import AuthExpiredError, ConfigurationError
from ..core.operations import operation_spec
from ..models.review import (
    ReviewCase,
    ReviewCaseFilter,
    ReviewCasePart,
    ReviewCaseRef,
    ReviewLoginInfo,
)
from ..parsing.review import (
    parse_login_info,
    parse_review_case,
    parse_review_cases,
    parse_review_doctors,
    parse_review_options,
    parse_review_part,
)
from ..runtime import SDKRuntime


class ReviewAdapter:
    def __init__(self, runtime: SDKRuntime) -> None:
        self.runtime = runtime

    def _json(self, key, parser, *, fields=None):
        spec = operation_spec("review." + key)

        def operation():
            base = self.runtime.settings.review_base_url.rstrip("/")
            response = self.runtime.request_response(
                spec,
                base + spec.path.removeprefix("/Pck"),
                allow_redirects=False,
                headers={
                    "Accept": "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": base + "/angular/",
                    "Origin": base.removesuffix("/Pck"),
                },
                **({"data": fields} if fields is not None else {}),
            )
            if 300 <= response.status_code < 400:
                raise AuthExpiredError("review session redirected to login", app="review")
            return parser(self.runtime.transport.json(response))

        return self.runtime.execute(spec, operation, operation_name=spec.key)

    def get_login_info(self) -> ReviewLoginInfo:
        def parse(value):
            fields = parse_login_info(value)
            session = self.runtime.auth.ensure("review")
            return ReviewLoginInfo(getattr(session, "authentication_mode", "unknown"), fields)

        return self._json("login_info", parse)

    def get_options(self) -> dict:
        return self._json("options", parse_review_options)

    def get_doctors(self, department: str) -> list[dict]:
        selector = ReviewCaseFilter(department=department)
        return self._json("doctors", parse_review_doctors, fields=selector.to_form())

    def get_cases(self, filter: ReviewCaseFilter) -> list[ReviewCase]:
        if not isinstance(filter, ReviewCaseFilter):
            raise ConfigurationError("review query requires ReviewCaseFilter")
        return self._json("cases", parse_review_cases, fields=filter.to_form())

    def _part(self, key: str, ref: ReviewCaseRef):
        if not isinstance(ref, ReviewCaseRef):
            raise ConfigurationError("review query requires ReviewCaseRef")
        return self._json(
            key,
            lambda value: (
                parse_review_case(value, ref)
                if key == "case_detail"
                else parse_review_part(value, ref, key)
            ),
            fields={"ApplySeq": ref.apply_seq},
        )

    def get_case(self, ref: ReviewCaseRef) -> ReviewCase:
        return self._part("case_detail", ref)

    def get_orders(self, ref: ReviewCaseRef) -> ReviewCasePart:
        return self._part("orders", ref)

    def get_attachments(self, ref: ReviewCaseRef) -> ReviewCasePart:
        return self._part("attachments", ref)

    def get_pacs(self, ref: ReviewCaseRef) -> ReviewCasePart:
        return self._part("pacs", ref)
