"""Opt-in JSON typing preserves raw API results and confirmed parser contracts."""

from __future__ import annotations

import json
import unittest
from typing import get_type_hints
from unittest.mock import patch

import vghks_sdk
from vghks_sdk import (
    as_patient_flags,
    as_review_doctors,
    as_review_options,
    as_surgery_payload,
)
from vghks_sdk.core.errors import ConfigurationError, ParseError
from vghks_sdk.models import json_responses
from vghks_sdk.parsing.oppl import OPPL_JSON_FIELDS
from vghks_sdk.services.records import RecordsService
from vghks_sdk.services.surgery import SurgeryService


class TypedResponseTests(unittest.TestCase):
    def test_all_recorded_patient_shapes_retain_raw_identity_values_and_unknown_keys(self):
        from test_query_replay_contracts import MRN, samples

        bodies = samples()
        with patch("socket.socket.connect", side_effect=AssertionError("pure conversion")):
            for suffix in ("allergy", "advance_directives", "research_flags", "bed_transfers", "care_cases"):
                key = "prq." + suffix
                raw = json.loads(bodies[key])
                raw["future_field"] = {"value": [False, None, 0, ""]}
                original = json.dumps(raw, sort_keys=True)
                with self.subTest(key=key):
                    self.assertIs(as_patient_flags(key, raw, MRN), raw)
                    self.assertIs(as_patient_flags(suffix, raw, MRN), raw)
                    self.assertEqual(json.dumps(raw, sort_keys=True), original)

    def test_all_surgery_shapes_preserve_empty_containers_and_opaque_row_values(self):
        from test_query_replay_contracts import MRN, samples

        bodies = samples()
        bodies["oppl.supply_model"] = b'{"items":[],"status":""}'
        with patch("socket.socket.connect", side_effect=AssertionError("pure conversion")):
            for key in OPPL_JSON_FIELDS:
                raw = json.loads(bodies["oppl." + key])
                raw["future_field"] = {"source_code": "UNDECODABLE"}
                with self.subTest(key=key):
                    self.assertIs(as_surgery_payload(key, raw, mrn=MRN), raw)
                    self.assertIs(as_surgery_payload("oppl." + key, raw, mrn=MRN), raw)
                    self.assertEqual(raw["future_field"]["source_code"], "UNDECODABLE")

    def test_review_choices_validate_strings_and_keep_unknown_fields(self):
        option = {"Value": "0", "Text": "source label", "extra": ["unknown"]}
        raw = {"InsuSectNo": [], "VSDrID": [option], "VerifyCode": [], "Future": []}
        self.assertEqual(as_review_options(raw), raw)
        self.assertEqual(as_review_doctors([option]), [option])
        self.assertEqual(as_review_doctors([]), [])
        self.assertEqual(as_review_options(raw)["VSDrID"][0]["Value"], "0")
        with self.assertRaises(ParseError) as caught:
            as_review_doctors([{"Value": 1, "Text": "unknown"}])
        self.assertEqual(caught.exception.info.code, "REVIEW_OPTIONS_INVALID")

    def test_schema_failures_and_patient_mismatch_keep_existing_error_codes(self):
        cases = (
            (lambda: as_patient_flags("allergy", {}, "0000000"), "PATIENT_JSON_SHAPE_INVALID"),
            (lambda: as_patient_flags("allergy", {"hhisnum": "OTHER", "allergy": "", "allergyMsg": ""}, "0000000"), "PATIENT_CONTEXT_MISMATCH"),
            (lambda: as_surgery_payload("request_numbers", {"reqs": {}}), "OPPL_JSON_SHAPE_INVALID"),
            (lambda: as_surgery_payload("patient_info", {"patient": {"hhisnum": "OTHER"}, "surgs": []}, mrn="0000000"), "PATIENT_CONTEXT_MISMATCH"),
            (lambda: as_review_options({}), "REVIEW_OPTIONS_INVALID"),
        )
        for operation, code in cases:
            with self.subTest(code=code), self.assertRaises(ParseError) as caught:
                operation()
            self.assertEqual(caught.exception.info.code, code)
        for operation in (lambda: as_patient_flags("unknown", {}, "0000000"),
                          lambda: as_surgery_payload("unknown", {})):
            with self.assertRaises(ConfigurationError) as caught:
                operation()
            self.assertEqual(caught.exception.info.code, "RESPONSE_OPERATION_UNSUPPORTED")

    def test_original_service_returns_remain_dicts_and_share_no_extra_requests(self):
        from unittest.mock import Mock

        patient = {"hhisnum": "0000000", "allergy": False, "allergyMsg": ""}
        surgery = {"patient": {"hhisnum": "0000000"}, "surgs": []}
        adapter = Mock()
        adapter.get_allergy.return_value = patient
        adapter.get_patient_info.return_value = surgery
        raw_patient = RecordsService(adapter).get_allergy("0000000")
        raw_surgery = SurgeryService(adapter).get_patient_info("0000000")
        self.assertIs(type(raw_patient), dict)
        self.assertIs(type(raw_surgery), dict)
        self.assertIs(as_patient_flags("allergy", raw_patient, "0000000"), patient)
        self.assertIs(as_surgery_payload("patient_info", raw_surgery, mrn="0000000"), surgery)
        adapter.get_allergy.assert_called_once_with("0000000")
        adapter.get_patient_info.assert_called_once_with("0000000")

    def test_public_shapes_resolve_types_without_guessing_unknown_scalars(self):
        for name, shape in vars(json_responses).items():
            if isinstance(shape, type) and hasattr(shape, "__required_keys__"):
                with self.subTest(name=name):
                    self.assertIs(getattr(vghks_sdk, name), shape)
                    self.assertTrue(get_type_hints(shape))
        self.assertIs(get_type_hints(json_responses.AllergyData)["allergy"], object)
        self.assertIs(get_type_hints(json_responses.ReviewOptionData)["Value"], str)


if __name__ == "__main__":
    unittest.main()
