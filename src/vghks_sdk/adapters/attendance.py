"""Own-account attendance reads and explicit, non-replayed punch submission."""

from __future__ import annotations

from urllib.parse import urlencode, urlsplit

from ..core.errors import ConfigurationError, ParseError, RequestError, SDKError
from ..core.operations import operation_spec
from ..models.attendance import (
    AttendanceHistory,
    AttendancePunchReceipt,
    AttendanceQuery,
    AttendanceState,
)
from ..parsing.attendance import (
    parse_attendance_history,
    parse_attendance_punch,
    parse_attendance_state,
)
from ..runtime import SDKRuntime


class AttendanceAdapter:
    def __init__(self, runtime: SDKRuntime) -> None:
        self.runtime = runtime

    def _request(self, key: str, **kwargs: object):
        spec = operation_spec("attendance." + key)
        base = urlsplit(self.runtime.settings.attendance_base_url)
        response = self.runtime.request_response(
            spec,
            f"{base.scheme}://{base.netloc}{spec.path}",
            allow_redirects=False,
            headers={
                "Origin": f"{base.scheme}://{base.netloc}",
                "Referer": f"{base.scheme}://{base.netloc}{spec.path}?reqCode=getPCClockInLog",
                **(
                    {"Content-Type": "application/x-www-form-urlencoded"}
                    if spec.method == "POST"
                    else {}
                ),
            },
            **kwargs,
        )
        if response.status_code != 200:
            raise ParseError(
                "attendance response was not a recognized read or acknowledgment",
                code="ATTENDANCE_REDIRECT_UNRECOGNIZED",
            )
        return self.runtime.transport.text(response)

    def _state(self) -> AttendanceState:
        text = self.runtime.auth.take_attendance_landing()
        if not text:
            text = self._request("status", params={"reqCode": "getPCClockInLog"})
        return parse_attendance_state(
            text, expected_employee_id=self.runtime.auth.credentials.username
        )

    def get_status(self) -> AttendanceState:
        spec = operation_spec("attendance.status")
        return self.runtime.execute(spec, self._state, operation_name=spec.key)

    def get_records(self, query: AttendanceQuery) -> AttendanceHistory:
        if not isinstance(query, AttendanceQuery):
            raise ConfigurationError(
                "attendance records require AttendanceQuery", code="ATTENDANCE_QUERY_INVALID"
            )
        spec = operation_spec("attendance.records")
        body = urlencode(query.to_form(), encoding="cp950", errors="strict").encode("ascii")

        def operation() -> AttendanceHistory:
            state = self._state()
            return parse_attendance_history(
                self._request("records", data=body), query=query, expected_state=state
            )

        return self.runtime.execute(spec, operation, operation_name=spec.key)

    def punch(self) -> AttendancePunchReceipt:
        spec = operation_spec("attendance.punch")

        def operation() -> AttendancePunchReceipt:
            # Identity and recorded form validation happen before any mutation.
            state = self._state()
            try:
                text = self._request("punch", data={"reqCode": "setPCClockInLog"})
                return parse_attendance_punch(text, expected_state=state)
            except SDKError as exc:
                raise RequestError(
                    "attendance punch outcome is unknown; read the records before another submission",
                    code="MUTATION_OUTCOME_UNKNOWN",
                    endpoint_path=spec.path,
                    phase=exc.info.phase,
                    retry_safe=False,
                    status_code=exc.info.http_status,
                ).with_context(operation=spec.key, app="attendance") from exc

        return self.runtime.execute(spec, operation, operation_name=spec.key)
