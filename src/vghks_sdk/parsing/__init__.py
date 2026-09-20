"""Pure response parsers grouped by internal application domain."""

from .audit import parse_unsigned_records
from .clinical import (
    parse_clinical_orders,
    parse_consults,
    parse_medication_orders,
    parse_numeric_history,
    parse_order_detail,
    parse_order_report,
    parse_pacs_study,
    parse_surgery_history,
    parse_treatments,
)
from .oppl import parse_surgery_records
from .prq import (
    parse_case_detail,
    parse_numeric_report,
    parse_opd_patients,
    parse_patient_identity,
    parse_soap,
    parse_visit_cases,
)
from .webmaas import (
    find_next_displaytag_href,
    parse_patient_basic_info,
    parse_patient_demographics,
    parse_registration_records,
)

__all__ = [
    "find_next_displaytag_href",
    "parse_case_detail",
    "parse_clinical_orders",
    "parse_consults",
    "parse_medication_orders",
    "parse_numeric_history",
    "parse_numeric_report",
    "parse_opd_patients",
    "parse_order_detail",
    "parse_order_report",
    "parse_pacs_study",
    "parse_patient_basic_info",
    "parse_patient_demographics",
    "parse_patient_identity",
    "parse_registration_records",
    "parse_soap",
    "parse_surgery_history",
    "parse_surgery_records",
    "parse_treatments",
    "parse_unsigned_records",
    "parse_visit_cases",
]
