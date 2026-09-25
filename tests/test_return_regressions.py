"""Opt-in real-return regressions: structural assertions, never network replay."""

from __future__ import annotations

import os
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from vghks_sdk.offline.analyze import inspect_bundle
from vghks_sdk.offline.bundle import BundleReader
from vghks_sdk.queries import QUERY_SPECS

RETURN = (
    Path(__file__).resolve().parents[1]
    / "data/returns/vghks-live-test-20260919-164834-1c8d7394-COMPLETED_WITH_ERRORS.zip"
)
LATEST_RETURN = RETURN.parent / "vghks-live-test-20260919-172436-3b7faf23-COMPLETED_WITH_ERRORS.zip"


@unittest.skipUnless(
    os.environ.get("VGHKS_RUN_HAR_CONTRACT") == "1" and RETURN.is_file(),
    "Requires the local return and VGHKS_RUN_HAR_CONTRACT=1",
)
class ReturnRegressionTests(unittest.TestCase):
    @unittest.skipUnless(LATEST_RETURN.is_file(), "Requires latest local return")
    def test_latest_return_confirms_fixes_and_recovers_opd_sections_without_network(self):
        with (
            patch("requests.sessions.Session.request") as network,
            BundleReader(LATEST_RETURN) as reader,
        ):
            report, _ = inspect_bundle(reader)
        network.assert_not_called()
        summary = report["query_summary"]
        self.assertEqual(
            {key: summary[key] for key in ("VERIFIED", "EMPTY", "FAILED", "BLOCKED", "MISSING", "NO_SAMPLE")},
            {"VERIFIED": 18, "EMPTY": 2, "FAILED": 0, "BLOCKED": 1, "MISSING": 1, "NO_SAMPLE": 0},
        )
        self.assertEqual(sum(summary.values()), len(QUERY_SPECS))
        self.assertEqual(report["root_cause"]["code"], "HTTP_404")
        self.assertTrue(
            all(row["certificate_verification"] for row in report["connection_profiles"])
        )
        self.assertEqual(report["opd_evidence"]["recorded_rows_without_section"], 93)
        positive = [
            row for row in report["opd_evidence"]["offline_responses"] if row["section_counts"]
        ]
        self.assertEqual(len(positive), 1)
        self.assertEqual(positive[0]["section_counts"], {"70": 53, "71": 42})
        self.assertEqual(positive[0]["missing_mrn_count"], 2)
        self.assertEqual(report["weekly_opd_workflow"]["status"], "NOT_TESTED")

    def test_all_recorded_parser_errors_resolve_without_rewriting_live_status(self):
        with patch("requests.sessions.Session.request") as network, BundleReader(RETURN) as reader:
            report, _ = inspect_bundle(reader)
        network.assert_not_called()
        expected = {
            "prq.treatments": {"PARSED": 2, "EMPTY": 3},
            "prq.numeric_history": {"PARSED": 3},
            "prq.order_report": {"PARSED": 4, "EMPTY": 4},
        }
        for key, counts in expected.items():
            rows = [row for row in report["replay"]["exchanges"] if row["operation"] == key]
            self.assertEqual(dict(Counter(row["status"] for row in rows)), counts)
            original = next(row for row in report["operations"] if row["operation"] == key)
            self.assertEqual(original["live_status"], "FAILED")
            self.assertEqual(original["replay_errors"], 0)
        self.assertEqual(report["query_summary"]["VERIFIED"], 15)
        self.assertFalse(report["unverified_tls_services"])
        self.assertTrue(all(row["recovered"] for row in report["preflight_findings"]))
        self.assertEqual(report["root_cause"]["code"], "HTTP_404")
        modes = report["connection_profiles"]
        self.assertEqual(len(modes), 6)
        self.assertTrue(all(row["certificate_verification"] for row in modes))
        self.assertTrue(
            all(row["cipher"] == "AES256-SHA256" for row in modes if row["target"] != "portal")
        )


if __name__ == "__main__":
    unittest.main()
