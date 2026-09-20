"""Composition examples. Import these functions with an authenticated SDK.

Nothing executes on import. The write example only prepares a command.
"""

from pathlib import Path

from vghks_sdk import VghksSDK
from vghks_sdk.local_io import write_json_atomic
from vghks_sdk.models import to_jsonable


def inspect_patient(sdk: VghksSDK, mrn: str, output: Path):
    """Independent checkpoints remain available if a later query fails."""
    results = {}
    for name, query in (
        ("allergy", lambda: sdk.records.get_allergy(mrn)),
        ("surgery", lambda: sdk.surgery.get_patient_info(mrn)),
        ("consents", lambda: sdk.surgery.get_patient_consents(mrn)),
        ("text_reports", lambda: sdk.records.get_text_report_history(mrn, "CHK", 3650)),
        ("uploads", lambda: sdk.records.get_upload_history(mrn)),
    ):
        try:
            results[name] = query()
            write_json_atomic(output / f"{name}.json", to_jsonable(results[name]))
        except Exception as exc:
            from vghks_sdk.core.errors import error_info

            write_json_atomic(output / f"{name}-error.json", to_jsonable(error_info(exc)))
    return results


def prepare_reviewed_schedule(sdk: VghksSDK, reviewed_fields, output: Path):
    """The caller supplies all clinical choices after reviewing the server form."""
    command = sdk.surgery.prepare_command("create_schedule", reviewed_fields)
    write_json_atomic(output / "prepared-create.json", to_jsonable(command))
    # Separate explicit call when the caller intends to change the record:
    # receipt = sdk.surgery.create_schedule(command)
    # write_json_atomic(output / "receipt.json", to_jsonable(receipt))
    # current = sdk.surgery.get_patient_info(dict(command.fields)["orhisnum"])
    # write_json_atomic(output / "after-query.json", to_jsonable(current))
    return command
