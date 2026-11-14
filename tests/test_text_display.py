"""Confirmed source mappings apply only when callers request display text."""

from __future__ import annotations

import json
import unittest

from vghks_sdk import display_patient_name, patient_name_profile
from vghks_sdk.core.response_text import decode_cp950, decode_response_text
from vghks_sdk.parsing.webmaas import parse_patient_demographics


class PatientNameDisplayTests(unittest.TestCase):
    def test_profile_contains_only_the_confirmed_source_characters(self) -> None:
        self.assertEqual(patient_name_profile(), {
            "source": "vghks", "version": "1",
            "mapping": {"\ue3a4": "\u51c3", "\uea81": "\u771e", "\ue03e": "\u7d89"},
        })
        self.assertEqual(display_patient_name("\ue3a4甲乙"), "凃甲乙")

    def test_unknown_sources_and_unknown_private_characters_are_preserved(self) -> None:
        original = "\ue3a4甲\ue3a5\U000F0000乙\uea81\ue03e"
        for source in ("other", "VGHKS", ""):
            with self.subTest(source=source):
                self.assertEqual(patient_name_profile(source), {
                    "source": source, "version": "1", "mapping": {},
                })
                self.assertEqual(display_patient_name(original, source), original)
        self.assertEqual(display_patient_name(original), "凃甲\ue3a5\U000F0000乙眞綉")

    def test_only_confirmed_characters_change_without_normalizing_other_text(self) -> None:
        for original, expected in (("", ""), (" 凃甲乙 ", " 凃甲乙 "),
                                   ("\ue3a4甲乙\ue3a4", "凃甲乙凃"),
                                   ("\ue3a4Ａ 甲\n乙", "凃Ａ 甲\n乙"),
                                   ("\ue3a4甲\uea81乙\ue03e", "凃甲眞乙綉"),
                                   (" 眞綉真繡 ", " 眞綉真繡 ")):
            with self.subTest(original=original):
                self.assertEqual(display_patient_name(original), expected)

    def test_mutating_a_returned_profile_cannot_change_later_display_or_profiles(self) -> None:
        profile = patient_name_profile()
        profile["source"], profile["version"] = "other", "changed"
        profile["mapping"]["\ue3a4"] = "合成修改"
        profile["mapping"]["\ue3a5"] = "合成新增"
        self.assertEqual(display_patient_name("\ue3a4甲\ue3a5"), "凃甲\ue3a5")
        self.assertEqual(patient_name_profile(), {
            "source": "vghks", "version": "1",
            "mapping": {"\ue3a4": "凃", "\uea81": "眞", "\ue03e": "綉"},
        })
        unknown = patient_name_profile("other")
        unknown["mapping"]["\ue3a4"] = "合成修改"
        self.assertEqual(patient_name_profile("other")["mapping"], {})

    def test_utf8_and_cp950_decoding_and_models_keep_original_names(self) -> None:
        original_name = "\ue3a4甲乙"
        utf8 = json.dumps({"patno": "SYNTHETIC", "patname": original_name}, ensure_ascii=False).encode()
        cp950 = (b'{"patno":"SYNTHETIC","patname":"\x8e\xf5'
                 + "甲乙".encode("cp950") + b'"}')
        for raw, mime in ((utf8, "application/json; charset=utf-8"),
                          (cp950, "application/json; charset=big5")):
            with self.subTest(mime=mime):
                before = bytes(raw)
                decoded, _ = decode_response_text(raw, mime)
                patient = parse_patient_demographics(json.loads(decoded), "SYNTHETIC")
                self.assertEqual(patient.name, original_name)
                self.assertEqual(display_patient_name(patient.name), "凃甲乙")
                self.assertEqual(patient.name, original_name)
                self.assertEqual(raw, before)
                self.assertEqual(decode_response_text(raw, mime)[0], decoded)
        self.assertEqual(decode_cp950(b"\x8e\xf5"), "\ue3a4")

    def test_confirmed_variant_characters_keep_the_exact_unicode_spelling(self) -> None:
        result = display_patient_name("\ue3a4\uea81\ue03e")
        self.assertEqual(result, "凃眞綉")
        self.assertEqual([ord(character) for character in result], [0x51C3, 0x771E, 0x7D89])
        self.assertNotIn("真", result)
        self.assertNotIn("繡", result)

    def test_new_mappings_keep_utf8_model_and_original_response_unchanged(self) -> None:
        original_name = "\uea81甲\ue03e乙"
        raw = json.dumps({"patno": "SYNTHETIC", "patname": original_name}, ensure_ascii=False).encode()
        before = bytes(raw)
        decoded, codec = decode_response_text(raw, "application/json; charset=utf-8")
        patient = parse_patient_demographics(json.loads(decoded), "SYNTHETIC")
        self.assertEqual((patient.name, codec), (original_name, "utf-8"))
        self.assertEqual(display_patient_name(patient.name), "眞甲綉乙")
        self.assertEqual(patient.name, original_name)
        self.assertEqual(raw, before)
        self.assertEqual(decode_response_text(raw, "application/json; charset=utf-8")[0], decoded)


if __name__ == "__main__":
    unittest.main()
