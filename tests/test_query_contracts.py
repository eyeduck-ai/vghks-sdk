"""Cross-layer catalog and Protocol checks with synthetic references only."""

from __future__ import annotations

import inspect
import unittest
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from typing import get_type_hints
from unittest.mock import create_autospec, patch

from tools.generate_api_reference import CLASSES
from vghks_sdk.core.errors import ConfigurationError
from vghks_sdk.extension_protocols import PatientQueriesProtocol
from vghks_sdk.live.atomic import _query_inputs
from vghks_sdk.live.config import LiveTestConfig
from vghks_sdk.models import (
    ClinicalOrder,
    HtmlDocument,
    OrderDetail,
    OrderDetailRef,
    OrderReport,
    OrderReportRef,
    PacsImageRef,
    PacsStudy,
    PacsStudyRef,
    PatientSurgeryRecord,
    PdfAttachmentRef,
    ReviewCase,
    ReviewCaseRef,
    SurgeryCase,
    SurgeryCaseRef,
    SurgeryNoteRef,
    TextReportHistory,
    UploadHistory,
    VisitCase,
    VisitFilter,
)
from vghks_sdk.queries import QUERY_SPECS, Queries, query_spec, validate_query_bindings
from vghks_sdk.services.auth import AuthService
from vghks_sdk.services.earnings import EarningsService
from vghks_sdk.services.protocols import (
    AuthServiceProtocol,
    RecordsServiceProtocol,
    SDKProtocol,
)

MRN = "SYN001"
DAY = date(2026, 1, 2)


def discovery_values():
    case = VisitCase(MRN, DAY, "O", "C1", "70", "Synthetic")
    detail = OrderDetailRef(MRN, "C1", "O", "1")
    report = OrderReportRef(MRN, "C1", "O", "1")
    study = PacsStudyRef(MRN, "R1")
    image = PacsImageRef(MRN, "R1", uid="1.2.3")
    pdf = PdfAttachmentRef(MRN, f"//HFS01_3A0.vghks.gov.tw/EMRU/8/{MRN}/sample.pdf")
    document = HtmlDocument("", "", ())
    surgery = SurgeryCaseRef(MRN, "R1", "1")
    order = ClinicalOrder(
        MRN, "C1", "O", "DBR", detail_ref=detail, report_ref=report, pacs_ref=study
    )
    return {
        "prq.visit_cases": [[case]],
        "prq.order_history": [[order]],
        "prq.case_orders": [[order]],
        "prq.order_detail": [OrderDetail(detail, {}, (report,), (study,), (pdf,))],
        "prq.order_report": [OrderReport(report, {}, (pdf,), (study,))],
        "prq.text_report": [OrderReport(report, {}, (pdf,), (study,))],
        "prq.pacs_study": [PacsStudy(study, (image,))],
        "prq.text_report_history": [TextReportHistory(MRN, "CHK", document, (report,))],
        "prq.upload_history": [UploadHistory(MRN, document, (pdf,))],
        "prq.surgery_history": [[PatientSurgeryRecord(MRN, surgery_record_refs=(pdf,))]],
        "oppl.patient_info": [{"surgs": [{"orcaseno": "C1"}]}],
        "oppl.consent_catalog": [{"forms": {"OPH": ["Synthetic"]}}],
        "oppl_records.cases": [[SurgeryCase(surgery, DAY, "Synthetic")]],
        "oppl_records.note": [SurgeryNoteRef(surgery, f"//HFS01_1A0/OPG/{MRN}/R1/note.pdf")],
        "review.options": [{"InsuSectNo": [{"Value": "70", "Text": "Synthetic"}]}],
        "review.cases": [
            [
                ReviewCase(
                    ReviewCaseRef("1"),
                    DAY.isoformat(),
                    MRN,
                    "Synthetic",
                    "TEST",
                    "70",
                    "0",
                    "",
                    "",
                    {},
                )
            ]
        ],
    }


def signature_shape(value):
    signature = inspect.signature(value)
    return signature.replace(
        parameters=[
            parameter.replace(annotation=inspect.Parameter.empty)
            for parameter in signature.parameters.values()
        ],
        return_annotation=inspect.Signature.empty,
    )


class QueryBindingTests(unittest.TestCase):
    def test_every_catalog_variant_binds_without_constructing_services_or_http(self):
        with patch("socket.socket.connect", side_effect=AssertionError("offline only")):
            validate_query_bindings(CLASSES)

    def test_positional_aliases_and_keyword_only_alternatives_remain_supported(self):
        class Service:
            def get(self, card_no=None, *, national_id=None):
                raise AssertionError("validation must not execute a Service")

        spec = replace(
            query_spec("prq.visit_cases"),
            service="sample",
            method="get",
            inputs=("doctor_card",),
            alternative_inputs=(("national_id",),),
        )
        validate_query_bindings({"sample": Service}, (spec,))

    def test_invalid_primary_and_keyword_bindings_fail_before_execution(self):
        class Service:
            def get(self, mrn, *, mode=None):
                raise AssertionError("validation must not execute a Service")

        base = replace(query_spec("prq.visit_cases"), service="sample", method="get")
        invalid = (
            replace(base, inputs=(), alternative_inputs=()),
            replace(base, inputs=("mrn", "mode"), alternative_inputs=()),
            replace(base, alternative_inputs=(("national_id",),)),
            replace(base, alternative_inputs=(("mrn", "unknown"),)),
            replace(base, inputs=("mrn", "mrn"), alternative_inputs=()),
            replace(base, alternative_inputs=(("mrn",),)),
        )
        for spec in invalid:
            with self.subTest(spec=spec), self.assertRaises(RuntimeError):
                validate_query_bindings({"sample": Service}, (spec,))

    def test_missing_or_noncallable_service_method_is_rejected(self):
        class Service:
            get_visit_cases = None

        for classes in ({}, {"records": Service}):
            with self.subTest(classes=classes), self.assertRaises(RuntimeError):
                validate_query_bindings(classes, (query_spec("prq.visit_cases"),))

    def test_every_query_discovers_inputs_accepted_by_real_service_signatures(self):
        config = LiveTestConfig(
            profile="atomic",
            test_mrn=MRN,
            doctor_card="TEST",
            opd_date=DAY,
            range_start=DAY,
            range_end=DAY,
            visit_filter=VisitFilter(all_sections=True),
        )
        sdk = SimpleNamespace(
            **{name: create_autospec(service, instance=True) for name, service in CLASSES.items()}
        )
        with patch("socket.socket.connect", side_effect=AssertionError("offline only")):
            for spec in QUERY_SPECS:
                with self.subTest(query=spec.key):
                    inputs = _query_inputs(spec, config, discovery_values())
                    self.assertTrue(inputs, "populated synthetic sources need a discovery sample")
                    for arguments in inputs:
                        Queries(sdk).run(spec.key, **arguments)

    def test_unknown_discovery_scope_cannot_fall_through_to_pdf_samples(self):
        spec = replace(query_spec("prq.pdf_attachment"), scope="unknown")
        with self.assertRaises(ConfigurationError) as caught:
            _query_inputs(spec, LiveTestConfig(), discovery_values())
        self.assertEqual(caught.exception.info.code, "QUERY_DISCOVERY_UNSUPPORTED")


class PublicProtocolTests(unittest.TestCase):
    def test_declared_protocol_calls_and_properties_match_public_services(self):
        classes = {**CLASSES, "auth": AuthService, "earnings": EarningsService}
        for service, protocol in get_type_hints(SDKProtocol).items():
            for name in dir(protocol):
                if name.startswith("_"):
                    continue
                contract = getattr(protocol, name)
                actual = getattr(classes[service], name)
                if isinstance(contract, property):
                    self.assertIsInstance(actual, property)
                    contract, actual = contract.fget, actual.fget
                if callable(contract):
                    with self.subTest(service=service, method=name):
                        self.assertEqual(signature_shape(contract), signature_shape(actual))
                        self.assertEqual(
                            get_type_hints(contract),
                            get_type_hints(actual),
                        )

    def test_convenience_api_and_observed_password_status_are_described(self):
        self.assertIsInstance(AuthServiceProtocol.password_status, property)
        self.assertEqual(
            signature_shape(RecordsServiceProtocol.get_ophthalmology_scan_history),
            signature_shape(CLASSES["records"].get_ophthalmology_scan_history),
        )

    def test_existing_adapter_reference_keyword_is_preserved(self):
        from vghks_sdk.adapters.prq import PrqAdapter

        self.assertEqual(
            signature_shape(PatientQueriesProtocol.get_text_report),
            signature_shape(PrqAdapter.get_text_report),
        )
        self.assertEqual(
            signature_shape(RecordsServiceProtocol.get_text_report),
            signature_shape(CLASSES["records"].get_text_report),
        )
