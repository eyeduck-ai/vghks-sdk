"""MIS own-account performance/bonus queries with fresh SSO and secondary login."""

from __future__ import annotations

from urllib.parse import urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

from ..core.config import EarningsCredentials
from ..core.errors import AuthenticationError, ConfigurationError, ParseError
from ..core.operations import operation_spec
from ..models import EarningsReportContext, HtmlDocument
from ..parsing.documents import parse_document, parse_form
from ..runtime import SDKRuntime

_POST_PATHS = {
    "/VGHK/MIS_Por/MADSINGLE.ASP": "mis.single",
    "/VGHK/PA_PMO003M.asp": "mis.performance_entry",
    "/VGHK/PA_PMO004M.asp": "mis.payroll_entry",
    "/VGHK/PAswd2db.asp": "mis.password",
    "/VGHK/MAD/MADMAIN.ASP": "mis.main",
}


class EarningsAdapter:
    def __init__(self, runtime: SDKRuntime) -> None:
        self.runtime = runtime

    def open_report(self, kind: str, credentials: EarningsCredentials) -> EarningsReportContext:
        if kind not in {"performance", "payroll"}:
            raise ConfigurationError("unknown earnings report", code="EARNINGS_KIND_INVALID")
        credentials.validate()

        def operation():
            with self.runtime.operation_lock:
                session = self.runtime.auth.ensure(kind)
                text, url = session.landing_html, session.landing_url
                password_submitted = False
                for _ in range(8):
                    self._validate_url(url)
                    soup = BeautifulSoup(text, "html.parser")
                    form = soup.find("form")
                    if form is not None:
                        target = urljoin(url, str(form.get("action", "")))
                        self._validate_url(target)
                        path = urlsplit(target).path
                        snapshot = parse_form(text)
                        fields = dict(snapshot.fields)
                        if path == "/ibi_apps/WFServlet":
                            expected = "PMO003R1" if kind == "performance" else "PMO004R1"
                            if fields.get("IBIF_ex") != expected or not snapshot.choices.get(
                                "BEGYM"
                            ):
                                raise ParseError(
                                    "unexpected report selector", code="EARNINGS_SELECTOR_INVALID"
                                )
                            return EarningsReportContext(
                                kind, self.runtime.auth.generation, snapshot
                            )
                        if path not in _POST_PATHS:
                            raise AuthenticationError(
                                "unexpected MIS form", code="EARNINGS_FORM_UNEXPECTED"
                            )
                        if path == "/VGHK/PAswd2db.asp":
                            if password_submitted:
                                raise AuthenticationError(
                                    "MIS password was not accepted",
                                    code="EARNINGS_PASSWORD_REJECTED",
                                )
                            password_submitted = True
                            user = self.runtime.auth.credentials.username
                            fields.update(
                                sUSR_ID=user,
                                txtUsrId=credentials.national_id,
                                txtPAPSWD=credentials.password,
                            )
                            submit = form.find("input", attrs={"name": "B1"})
                            if submit is not None:
                                fields["B1"] = str(submit.get("value", ""))
                        response = self._request(
                            operation_spec(_POST_PATHS[path]),
                            target,
                            data=urlencode(fields, encoding="cp950"),
                            headers={
                                "Content-Type": "application/x-www-form-urlencoded",
                                "Referer": url,
                            },
                        )
                    else:
                        frames = [
                            urljoin(url, str(node.get("src", "")))
                            for node in soup.find_all(["frame", "iframe"])
                        ]
                        frames = [
                            target
                            for target in frames
                            if urlsplit(target).path == "/ibi_apps/WFServlet"
                        ]
                        if len(frames) != 1:
                            raise ParseError("MIS frame missing", code="EARNINGS_FRAME_MISSING")
                        self._validate_url(frames[0])
                        response = self._request(
                            operation_spec("mis.frame"), frames[0], headers={"Referer": url}
                        )
                    text, url = self.runtime.transport.text(response), response.url
                raise ParseError(
                    "MIS navigation exceeded expected steps", code="EARNINGS_NAVIGATION_LIMIT"
                )

        # Password POST is sent once. No automatic authentication sweep retries it.
        return self.runtime.trace_direct(f"mis.open_{kind}", "mis", operation)

    def get_report(self, context: EarningsReportContext, period: str | None = None) -> HtmlDocument:
        def operation():
            with self.runtime.operation_lock:
                if context.generation != self.runtime.auth.generation:
                    raise ConfigurationError(
                        "reopen MIS report after re-login", code="EARNINGS_CONTEXT_EXPIRED"
                    )
                fields = dict(context.form.fields)
                allowed = {value for value, _ in context.form.choices.get("BEGYM", ())}
                selected = period if period is not None else fields.get("BEGYM", "")
                if not selected or selected not in allowed:
                    raise ConfigurationError(
                        "choose a period returned by MIS", code="EARNINGS_PERIOD_INVALID"
                    )
                expected = {"performance": "PMO003R1", "payroll": "PMO004R1"}.get(context.kind)
                if not expected or fields.get("IBIF_ex") != expected:
                    raise ConfigurationError(
                        "unexpected report program", code="EARNINGS_PROGRAM_INVALID"
                    )
                fields["BEGYM"] = selected
                url = self.runtime.settings.mis_base_url.rstrip("/") + "/ibi_apps/WFServlet"
                response = self._request(
                    operation_spec("mis.frame"), url, params=urlencode(fields, encoding="cp950")
                )
                self._validate_url(response.url)
                text = self.runtime.transport.text(response)
                soup = BeautifulSoup(text, "html.parser")
                if soup.find("input", attrs={"type": "password"}) is not None:
                    raise AuthenticationError(
                        "MIS returned its password page", code="EARNINGS_SESSION_EXPIRED"
                    )
                if expected not in text or soup.find("form") is not None:
                    raise ParseError(
                        "MIS did not return the requested report", code="EARNINGS_REPORT_INVALID"
                    )
                return parse_document(text, require_table=True)

        return self.runtime.trace_direct(f"mis.{context.kind}_report", "mis", operation)

    def _request(self, spec, target: str, **kwargs):
        self._validate_url(target)
        response = self.runtime.request_response(spec, target, allow_redirects=False, **kwargs)
        if response.status_code in {302, 303} and spec.key in {
            "mis.performance_entry",
            "mis.payroll_entry",
        }:
            destination = urljoin(target, response.headers.get("Location", ""))
            self._validate_url(destination)
            if urlsplit(destination).path != "/VGHK/Pswdchk.asp":
                raise AuthenticationError(
                    "unexpected MIS entry redirect", code="EARNINGS_REDIRECT_INVALID"
                )
            response = self.runtime.request_response(
                operation_spec("mis.password_page"), destination, allow_redirects=False
            )
        if not 200 <= response.status_code < 300:
            raise AuthenticationError("unexpected MIS redirect", code="EARNINGS_REDIRECT_INVALID")
        return response

    def _validate_url(self, url: str) -> None:
        parsed = urlsplit(url)
        expected = urlsplit(self.runtime.settings.mis_base_url)
        if parsed.scheme != "https" or parsed.netloc.lower() != expected.netloc.lower():
            raise AuthenticationError(
                "MIS navigation left its configured host", code="EARNINGS_HOST_INVALID"
            )
