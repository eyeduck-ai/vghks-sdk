"""Pure preparation and validation of the four explicitly recorded write actions."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from .core.errors import ConfigurationError
from .models import SurgeryCommand

_SCHEDULE_FIELDS = frozenset(
    [
        "orhisnum",
        "orcasetp",
        "orcaseno",
        "ordseqno",
        "orreqno",
        "orsignnm",
        "orsign",
        "incomplete",
        "ori_date",
        "cancelReason",
        "cancelPrint",
        "repeatsurg",
        "repeatReason",
        "repeatPlan",
        "rsnursta",
        "rsbedno",
        "transpat",
        "noclass",
        "emergency",
        "opforemr_hidden",
        "AE",
        "searchOPCode1",
        "searchOPName1",
        "searchOPIcd1",
        "searchOPCode2",
        "searchOPName2",
        "searchOPIcd2",
        "searchOPCode3",
        "searchOPName3",
        "searchOPIcd3",
        "searchOPCode4",
        "searchOPName4",
        "searchOPIcd4",
        "opdate",
        "optime",
        "oproomSelectA",
        "estimatedtime",
        "opsectselect",
        "borrowrecord",
        "moveHard",
        "anesselect",
        "prepareblood",
        "orradtxt",
        "doct",
        "doctId",
        "orguinm",
        "orguinmId",
        "doct1",
        "doctId1",
        "doct2",
        "doctId2",
        "doct3",
        "doctId3",
        "doct4",
        "doctId4",
        "doct5",
        "doctId5",
        "doctVS",
        "doctVSId",
        "diag",
        "supplement",
        "oranetxt",
        "eras_option",
        "opSites",
        "oropbx",
        "systemDiseases",
        "otherDisease",
        "instr_option",
        "wound",
        "bio_option",
        "ncopaplynisin",
        "ncopaplyprice",
        "ncopaplynum",
        "eraset",
        "erasdt",
        "erastm",
        "instrs",
        "antibiotics",
        "dosage",
        "ornpodt",
        "npotime",
    ]
)
_CONSENT_FIELDS = frozenset(
    [
        "userNm",
        "userId",
        "hhisnum",
        "orreqno",
        "hcasetyp",
        "hcaseno",
        "hsexc",
        "hsex",
        "hnamec",
        "hpatadr",
        "hidno",
        "hbirthday",
        "phone",
        "patsect",
        "rwrecno",
        "hnursta",
        "hbedno",
        "formCode",
        "status",
        "preopMark",
        "emptyPaint",
        "surgreqno",
        "agreeType",
        "opdoctId",
        "opdoct",
        "seldrsect",
        "drsect",
        "selprediseasename",
        "seldiseasename",
        "diseasename",
        "selopreason",
        "opreason",
        "selopname1",
        "opname1",
        "selopname2",
        "opname2",
        "useLA",
        "declare",
        "tempreply",
        "reply",
    ]
)
ACTIONS = frozenset({"create_schedule", "edit_schedule", "cancel_schedule", "create_consent"})


def prepare_command(
    action: str, fields: Mapping[str, str] | Iterable[tuple[str, str]]
) -> SurgeryCommand:
    """No clinical defaults. Repeated fields (supplies/declarations) retain order."""
    action = action.removeprefix("oppl.")
    if action not in ACTIONS:
        raise ConfigurationError("unsupported surgery action", code="SURGERY_ACTION_INVALID")
    pairs = tuple(fields.items() if isinstance(fields, Mapping) else fields)
    allowed = _CONSENT_FIELDS if action == "create_consent" else _SCHEDULE_FIELDS
    cleaned = []
    for key, value in pairs:
        if key in {"hid", "HID", "method"}:
            continue  # runtime owns fresh authentication and fixed dispatch
        if key not in allowed or not isinstance(value, str) or "\x00" in value:
            raise ConfigurationError("unsupported form field", code="SURGERY_FIELD_INVALID")
        cleaned.append((key, value))
    values = dict(cleaned)
    repeated = {"declare", "ncopaplynisin", "ncopaplyprice", "ncopaplynum", "instrs"}
    names = [key for key, _ in cleaned]
    if any(names.count(key) > 1 for key in set(names) - repeated):
        raise ConfigurationError("duplicate scalar field", code="SURGERY_FIELD_DUPLICATE")
    if action == "create_consent":
        required = {
            "hhisnum",
            "hcasetyp",
            "hcaseno",
            "formCode",
            "status",
            "opdoctId",
            "drsect",
            "surgreqno",
        }
        if values.get("status") != "A" or values.get("rwrecno"):
            raise ConfigurationError(
                "only new consent creation is recorded", code="CONSENT_CREATE_ONLY"
            )
    else:
        required = {
            "orhisnum",
            "orcasetp",
            "orcaseno",
            "opdate",
            "optime",
            "opsectselect",
            "doctId",
            "searchOPCode1",
        }
        if action == "create_schedule":
            if values.get("orreqno") or values.get("ordseqno"):
                raise ConfigurationError(
                    "new schedule contains existing identifiers", code="SURGERY_CREATE_HAS_ID"
                )
        else:
            required |= {"orreqno", "ordseqno"}
        if action == "cancel_schedule":
            required.add("cancelReason")
    if any(not values.get(key, "").strip() for key in required):
        raise ConfigurationError(
            "required fields missing: "
            + ", ".join(sorted(key for key in required if not values.get(key, "").strip())),
            code="SURGERY_FIELDS_MISSING",
        )
    counts = [names.count(key) for key in ("ncopaplynisin", "ncopaplyprice", "ncopaplynum")]
    if len(set(counts)) != 1:
        raise ConfigurationError("supply arrays differ in length", code="SURGERY_SUPPLIES_INVALID")
    return SurgeryCommand(f"oppl.{action}", tuple(cleaned))
