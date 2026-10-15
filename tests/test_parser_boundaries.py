"""Compatibility and failure semantics for pure parsers moved out of Adapters."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from vghks_sdk.adapters import oppl as old_oppl
from vghks_sdk.adapters import prq_extensions as old_prq
from vghks_sdk.core.errors import ParseError
from vghks_sdk.offline.replay import replay_response
from vghks_sdk.parsing import oppl
from vghks_sdk.parsing import prq_extensions as prq


class PureParserBoundaryTests(unittest.TestCase):
    def test_existing_imports_reexport_the_production_pure_functions(self):
        self.assertIs(old_oppl.mutation_acknowledged, oppl.mutation_acknowledged)
        for name in ("parse_patient_flags", "parse_text_history", "parse_upload_history"):
            self.assertIs(getattr(old_prq, name), getattr(prq, name))

    def test_upload_type_schema_and_legal_empty_values_are_shared(self):
        for value in ([], [{"maintp": "OPD", "mainnm": "Synthetic", "extra": "kept"}]):
            self.assertIs(prq.parse_upload_types(value), value)
        for value in ({}, [None], [{"maintp": "OPD"}]):
            with self.subTest(value=value), self.assertRaises(ParseError) as caught:
                prq.parse_upload_types(value)
            self.assertEqual(caught.exception.info.code, "UPLOAD_TYPES_INVALID")

    def test_patient_flags_keep_original_value_and_patient_binding(self):
        value = {"hhisnum": "SYN001", "allergy": False, "allergyMsg": "", "extra": []}
        self.assertIs(prq.parse_patient_flags("prq.allergy", value, "SYN001"), value)
        with self.assertRaises(ParseError) as caught:
            prq.parse_patient_flags("allergy", value, "OTHER")
        self.assertEqual(caught.exception.info.code, "PATIENT_CONTEXT_MISMATCH")

    def test_empty_histories_remain_distinct_from_unknown_schema(self):
        with patch("socket.socket.connect", side_effect=AssertionError("offline only")):
            self.assertFalse(
                prq.parse_text_history('<div id="height_bd"></div>', "SYN001", "CHK").report_refs
            )
            self.assertFalse(
                prq.parse_upload_history('<table id="tbObj"></table>', "SYN001").pdf_refs
            )
            for parser, arguments, code in (
                (
                    prq.parse_text_history,
                    ("unknown", "SYN001", "CHK"),
                    "TEXT_HISTORY_STRUCTURE_MISSING",
                ),
                (
                    prq.parse_upload_history,
                    ("unknown", "SYN001"),
                    "UPLOAD_HISTORY_STRUCTURE_MISSING",
                ),
            ):
                with self.subTest(parser=parser), self.assertRaises(ParseError) as caught:
                    parser(*arguments)
                self.assertEqual(caught.exception.info.code, code)

    def test_acknowledgment_requires_the_exact_recorded_success_shape(self):
        success = "手術排程編輯成功!!!"
        with patch("socket.socket.connect", side_effect=AssertionError("offline only")):
            self.assertTrue(oppl.mutation_acknowledged("oppl.edit_schedule", success))
            self.assertTrue(
                oppl.mutation_acknowledged(
                    "oppl.edit_schedule", success.encode("utf-8").decode("latin1")
                )
            )
            self.assertFalse(oppl.mutation_acknowledged("oppl.edit_schedule", success + " unknown"))
            self.assertFalse(
                oppl.mutation_acknowledged("oppl.create_schedule", {"msg": "手術排程新增成功!!!"})
            )
            self.assertTrue(oppl.mutation_acknowledged("oppl.create_consent", "Y"))
            self.assertFalse(oppl.mutation_acknowledged("unknown", "Y"))

    def test_replay_uses_shared_upload_schema_and_preserves_failure_codes(self):
        for source, status, code in (
            (b"[]", "EMPTY", ""),
            (b"{}", "PARSE_ERROR", "UPLOAD_TYPES_INVALID"),
            (b'[{"maintp":"OPD"}]', "PARSE_ERROR", "UPLOAD_TYPES_INVALID"),
        ):
            with self.subTest(source=source):
                result = replay_response("prq.upload_types", source, {})
                self.assertEqual((result["status"], result["error_code"]), (status, code))
