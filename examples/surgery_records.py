"""Compose both recorded history partitions and all operation notes."""

from pathlib import Path

from vghks_sdk import SurgeryCaseFilter
from vghks_sdk.services.protocols import SDKProtocol
from vghks_sdk.workflows import SurgeryCollectionResult, collect_surgery_records


def collect_doctor_surgeries(
    sdk: SDKProtocol,
    doctor_card: str,
    procedure_code: str,
    *,
    output_dir: Path,
) -> SurgeryCollectionResult:
    return collect_surgery_records(
        sdk,
        SurgeryCaseFilter(surgeon_card=doctor_card, procedure_code=procedure_code),
        periods=("24M", "2YB"),
        output_dir=output_dir,
    )
