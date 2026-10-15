"""Optional JSON shapes; unconfirmed scalar/row schemas stay opaque.

TypedDict values remain ordinary dictionaries. Unknown source fields survive
the opt-in converters in responses; these types do not interpret source codes.
"""

from typing import TypedDict


class AllergyData(TypedDict):
    hhisnum: object
    allergy: object
    allergyMsg: object


class AdvanceDirectiveData(TypedDict):
    hhisnum: object
    adSign: object
    adSignMsg: object


class ResearchFlagsData(TypedDict):
    hhisnum: object
    irbtype: object


class BedTransfersData(TypedDict):
    hhisnum: object
    frontBed: object
    nextBed: object


class CareCasesData(TypedDict):
    status: object
    caseList: object


class SurgeryPatientData(TypedDict):
    patient: dict[str, object]
    surgs: list[object]


class SurgeryPatientConsentsData(SurgeryPatientData):
    consents: list[object]


class SurgeryRequestNumbersData(TypedDict):
    reqs: list[object]


class SurgeryMedicationData(TypedDict):
    status: object


class SurgeryProcedureCatalogData(TypedDict):
    pfiles: list[object]
    chargenos: list[object]


class SurgeryHolidaysData(TypedDict):
    holidays: object
    maxDate: object


class SurgerySupplyData(TypedDict):
    items: list[object]
    status: object


class SurgeryConsentCatalogData(TypedDict):
    forms: dict[str, object]
    status: object


class SurgeryConsentTemplateData(TypedDict):
    status: object
    diseasename: object
    opname1: object


class SurgeryConsentDoctorData(TypedDict):
    status: object
    upuser: dict[str, object]


class ReviewOptionData(TypedDict):
    Value: str
    Text: str


class ReviewOptionsData(TypedDict):
    InsuSectNo: list[ReviewOptionData]
    VSDrID: list[ReviewOptionData]
    VerifyCode: list[ReviewOptionData]


PatientFlagData = AllergyData | AdvanceDirectiveData | ResearchFlagsData | BedTransfersData | CareCasesData
SurgeryPayloadData = (
    SurgeryPatientData | SurgeryPatientConsentsData | SurgeryRequestNumbersData
    | SurgeryMedicationData | SurgeryProcedureCatalogData | SurgeryHolidaysData
    | SurgerySupplyData | SurgeryConsentCatalogData | SurgeryConsentTemplateData
    | SurgeryConsentDoctorData
)
