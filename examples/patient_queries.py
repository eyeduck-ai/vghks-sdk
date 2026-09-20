"""Compose independent patient queries with a checkpoint for every outcome.

The application owns SDK construction, credentials and connection settings.
Call collect_patient_context(sdk, mrn, output_dir=Path(...)) after that setup.
"""

from pathlib import Path
from typing import Any

from vghks_sdk.core.errors import SDKError, error_info
from vghks_sdk.local_io import write_json_atomic
from vghks_sdk.models import to_jsonable
from vghks_sdk.services.protocols import SDKProtocol


def collect_patient_context(sdk: SDKProtocol, mrn: str, *, output_dir: Path) -> dict[str, Any]:
    results: dict[str, Any] = {}
    queries = (
        ("basic_info", sdk.patients.get_basic_info),
        ("demographics", sdk.patients.get_demographics),
        ("registrations", sdk.patients.get_registration_history),
    )
    for name, query in queries:
        try:
            result = query(mrn)
        except SDKError as exc:
            entry = {"status": "ERROR", "issue": to_jsonable(error_info(exc))}
        else:
            entry = {"status": "EMPTY" if result == [] else "OK", "data": to_jsonable(result)}
        results[name] = entry
        write_json_atomic(output_dir / f"{name}.json", {"input": {"mrn": mrn}, **entry})
    return results
