"""Compose tested query units without coupling a workflow to HTTP or HTML.

Call collect_visit_orders(sdk, authorized_mrn, visit_filter) from a process
that owns an authenticated VghksSDK. This module does not log in on import.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from vghks_sdk import AuthenticationError, SDKError, VisitFilter
from vghks_sdk.queries import run_query
from vghks_sdk.search import DoctorOpdPatientSource
from vghks_sdk.workflows.opd_soap import OpdSoapResult, scan_opd_soap


def collect_weekly_cata(
    sdk: Any, login_card: str, output_dir: Path, end: date | None = None
) -> OpdSoapResult:
    """Keep all three stages; use a new output directory for each execution."""
    last_day = end or date.today()
    return scan_opd_soap(
        sdk,
        source=DoctorOpdPatientSource(login_card, last_day - timedelta(days=6), last_day),
        output_dir=output_dir,
    )


def collect_visit_orders(sdk: Any, mrn: str, visit_filter: VisitFilter) -> Iterator[dict[str, Any]]:
    cases = run_query(sdk, "prq.visit_cases", mrn=mrn)
    for case in visit_filter.select(cases):
        row: dict[str, Any] = {"case": case, "results": {}, "issues": []}
        for key in ("prq.soap", "prq.numeric", "prq.case_orders"):
            try:
                row["results"][key] = run_query(sdk, key, case=case)
            except AuthenticationError:
                raise  # The shared Runtime has already attempted recovery.
            except SDKError as exc:
                row["issues"].append(exc.info)
        yield row
