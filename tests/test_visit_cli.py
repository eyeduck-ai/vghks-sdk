from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import date
from pathlib import Path
from unittest.mock import patch

from vghks_sdk import DoctorOpdPatientSource, MrnPatientSource
from vghks_sdk.cli import (
    _soap_search_from_args,
    _soap_source_from_args,
    _visit_filter_from_args,
    build_parser,
    main,
)


class VisitCliTests(unittest.TestCase):
    def test_repeated_name_and_code_build_one_or_filter_with_case_dates(self) -> None:
        args = build_parser().parse_args(
            [
                "visit-history",
                "--mrn",
                "00000000",
                "--output",
                "output",
                "--section-name",
                "眼科",
                "--section-name",
                "家醫",
                "--section-code",
                "70",
                "--case-start",
                "2026-01-01",
                "--case-end",
                "2026-01-31",
            ]
        )
        selector = _visit_filter_from_args(args, default_eye=False)
        self.assertEqual(selector.section_name_contains, ("眼科", "家醫"))
        self.assertEqual(selector.section_codes, ("70",))
        self.assertEqual(selector.start_date, date(2026, 1, 1))
        self.assertEqual(selector.end_date, date(2026, 1, 31))

    def test_formal_commands_require_explicit_section_before_sdk_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch("vghks_sdk.cli.VghksSDK") as sdk_factory, redirect_stderr(io.StringIO()):
                exit_code = main(
                    [
                        "latest-records",
                        "--input",
                        str(root / "mrns.txt"),
                        "--output",
                        str(root / "out"),
                    ]
                )
            self.assertEqual(exit_code, 2)
            sdk_factory.assert_not_called()

    def test_all_sections_conflict_and_unpaired_case_dates_fail_before_network(self) -> None:
        cases = (
            [
                "scan-soap",
                "--doctor-card",
                "D001",
                "--start",
                "2026-01-01",
                "--end",
                "2026-01-01",
                "--output",
                "out",
                "--text",
                "APPLY",
                "--all-sections",
                "--section-code",
                "70",
            ],
            [
                "visit-history",
                "--mrn",
                "00000000",
                "--output",
                "out",
                "--section-name",
                "眼科",
                "--case-start",
                "2026-01-01",
            ],
        )
        for argv in cases:
            with (
                self.subTest(argv=argv),
                patch("vghks_sdk.cli.VghksSDK") as sdk_factory,
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(main(argv), 2)
                sdk_factory.assert_not_called()

    def test_diagnose_and_live_test_default_to_ophthalmology_but_allow_override(self) -> None:
        parser = build_parser()
        diagnose = _visit_filter_from_args(parser.parse_args(["diagnose"]), default_eye=True)
        self.assertEqual(diagnose.section_name_contains, ("眼科",))
        live = _visit_filter_from_args(
            parser.parse_args(
                [
                    "live-test",
                    "--output",
                    "live",
                    "--section-code",
                    "60",
                ]
            ),
            default_eye=True,
        )
        self.assertEqual(live.section_name_contains, ())
        self.assertEqual(live.section_codes, ("60",))

    def test_eye_history_is_an_unknown_command(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            build_parser().parse_args(["eye-history", "--mrn", "00000000", "--output", "out"])
        self.assertEqual(raised.exception.code, 2)

    def test_scan_apply_is_an_unknown_command(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            build_parser().parse_args(
                [
                    "scan-apply",
                    "--doctor-card",
                    "D001",
                    "--start",
                    "2026-01-01",
                    "--end",
                    "2026-01-01",
                ]
            )
        self.assertEqual(raised.exception.code, 2)

    def test_scan_soap_source_dates_and_regex_fail_before_sdk_creation(self) -> None:
        invalid = (
            [
                "scan-soap",
                "--doctor-card",
                "D001",
                "--text",
                "APPLY",
                "--all-sections",
                "--output",
                "out",
            ],
            [
                "scan-soap",
                "--mrn",
                "00000000",
                "--start",
                "2026-01-01",
                "--end",
                "2026-01-02",
                "--text",
                "APPLY",
                "--all-sections",
                "--output",
                "out",
            ],
            [
                "scan-soap",
                "--mrn",
                "00000000",
                "--regex",
                "(APPLY)+",
                "--all-sections",
                "--output",
                "out",
            ],
            [
                "scan-soap",
                "--mrn",
                "00000000",
                "--text",
                "APPLY",
                "--multiline",
                "--all-sections",
                "--output",
                "out",
            ],
        )
        for argv in invalid:
            with (
                self.subTest(argv=argv),
                patch("vghks_sdk.cli.VghksSDK") as sdk_factory,
                patch("vghks_sdk.cli._credentials_from_env") as credentials,
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(main(argv), 2)
                sdk_factory.assert_not_called()
                credentials.assert_not_called()

    def test_scan_soap_builds_all_cli_source_forms_and_search_flags(self) -> None:
        parser = build_parser()
        doctor_args = parser.parse_args(
            [
                "scan-soap",
                "--doctor-card",
                "D001",
                "--start",
                "2026-01-01",
                "--end",
                "2026-01-02",
                "--text",
                "APPLY",
                "--all-sections",
                "--output",
                "out",
            ]
        )
        doctor = _soap_source_from_args(doctor_args)
        self.assertIsInstance(doctor, DoctorOpdPatientSource)
        self.assertEqual(doctor.start, date(2026, 1, 1))

        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "mrns.csv"
            input_path.write_text("病歷號\n００００００００\n00000000\n", encoding="utf-8-sig")
            input_args = parser.parse_args(
                [
                    "scan-soap",
                    "--input",
                    str(input_path),
                    "--regex",
                    r"APPLY\s+OD",
                    "--ignore-case",
                    "--multiline",
                    "--section-code",
                    "70",
                    "--output",
                    "out",
                ]
            )
            source = _soap_source_from_args(input_args)
            search = _soap_search_from_args(input_args)
        self.assertIsInstance(source, MrnPatientSource)
        self.assertEqual(source.mrns, ("00000000",))
        self.assertEqual(search.mode, "regex")
        self.assertTrue(search.ignore_case)
        self.assertTrue(search.multiline)

        mrn_args = parser.parse_args(
            [
                "scan-soap",
                "--mrn",
                "00000000",
                "--text",
                "APPLY",
                "--section-name",
                "眼科",
                "--output",
                "out",
            ]
        )
        self.assertEqual(_soap_source_from_args(mrn_args).mrns, ("00000000",))


if __name__ == "__main__":
    unittest.main()
