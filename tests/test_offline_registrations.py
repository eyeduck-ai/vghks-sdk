from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import requests
from test_registration_batch import BatchSDK
from test_webmaas import registration

from vghks_sdk.core.config import PortalCredentials
from vghks_sdk.core.errors import LoginRejectedError, ParseError
from vghks_sdk.live.bundle import LiveTestBundleManager, pack_incomplete_run
from vghks_sdk.live.capture import RawCaptureRecorder
from vghks_sdk.live.config import LiveTestConfig
from vghks_sdk.live.registrations import run_registration_test
from vghks_sdk.live.runner import execute_live_test
from vghks_sdk.local_io import write_json_atomic, write_jsonl_line
from vghks_sdk.offline.analyze import analyze_bundle, inspect_bundle
from vghks_sdk.offline.bundle import BundleReader
from vghks_sdk.offline.replay import replay_bundle, replay_response
from vghks_sdk.parsing.webmaas import parse_registration_records, registration_identity

MRNS = ("00000109", "TEST002", "TEST003")
SOURCE = "PRIVATE-SOURCE-ACCOUNT"
ICON = '<label title="未到診"><img src="../images/icon/Rsdefyrc.png"/></label>'
URL = "https://intranet.example/webmaas/RSV/RSV11W001.do"


class RecordedBatchSDK(BatchSDK):
    def __init__(self, capture, pages, *, login_error=False):
        super().__init__(login_error=login_error)
        self.capture, self.pages = capture, pages

    def get_registration_history(self, mrn):
        self.calls.append(mrn)
        pages = self.pages[mrn]
        if isinstance(pages, BaseException):
            raise pages
        rows, seen = [], set()
        for i, text in enumerate(pages, 1):
            request = (requests.Request("POST", URL, data={"QRY": "QRY", "patno": mrn,
                       "org.apache.struts.taglib.html.TOKEN": "SYNTHETIC", "pageid": "RSV11W001", "pageSize": "100"}) if i == 1 else
                       requests.Request("GET", URL, params={"d-123-p": str(i), "patno": mrn})).prepare()
            response = requests.Response()
            response.status_code, response.request, response.url = 200, request, request.url
            response.headers["Content-Type"] = "text/html; charset=big5"
            response._content = text.encode("cp950", errors="xmlcharrefreplace")
            self.capture.record_response(response=response, attempt=1, max_attempts=1,
                                         throttle_delay_seconds=0, elapsed_seconds=0, will_retry=False)
            for row in parse_registration_records(text, mrn):
                key = registration_identity(row)
                if key not in seen:
                    seen.add(key)
                    rows.append(row)
        return rows


class OfflineRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "run"
        self.guard = patch("socket.socket.connect", side_effect=AssertionError("offline only"))
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def bundle(self, *, pages=None, mrns=MRNS, login_error=False):
        config = LiveTestConfig(profile="registrations", patient_mrns=mrns)
        if pages is None:
            pages = {
                MRNS[0]: [registration(MRNS[0], registered_by=SOURCE, notes_html=ICON,
                                       next_href=f"?d-123-p=2&patno={MRNS[0]}"),
                          registration(MRNS[0], registered_by=SOURCE,
                                       next_href=f"?d-123-p=3&patno={MRNS[0]}"),
                          registration(MRNS[0], registered_by=SOURCE)],
                MRNS[1]: [registration(MRNS[1], empty=True)],
                MRNS[2]: [registration(MRNS[2], registered_by="MWEB")],
            }
        with RawCaptureRecorder(self.root) as capture, redirect_stdout(io.StringIO()):
            sdk = RecordedBatchSDK(capture, pages, login_error=login_error)
            try:
                run_registration_test(sdk, config, output_dir=self.root, raw_capture=capture)
            except KeyboardInterrupt:
                write_json_atomic(self.root / "run_summary.json", {
                    "schema_version": 6, "profile": "registrations", "status": "INTERRUPTED",
                    "steps": self.read("step_results.json"),
                })
        write_json_atomic(self.root / "run_config.json", config.to_safe_dict())

    def read(self, name):
        return json.loads((self.root / name).read_text(encoding="utf-8"))

    def write(self, name, value):
        write_json_atomic(self.root / name, value)

    def report(self):
        with BundleReader(self.root) as reader:
            return inspect_bundle(reader)[0]

    def manifest(self):
        with BundleReader(self.root) as reader:
            return reader.jsonl("capture_manifest.jsonl")

    def write_manifest(self, rows):
        path = self.root / "capture_manifest.jsonl"
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    def test_complete_batch_replays_pages_deduplicates_and_preserves_privacy(self):
        self.bundle()
        report = self.report()
        batch = report["registration_batch"]
        self.assertEqual(report["analysis_status"], "OK")
        self.assertEqual(batch["status"], "OBSERVED")
        self.assertEqual((batch["requested_patient_count"], batch["verified_patient_count"],
                          batch["raw_response_count"]), (3, 3, 5))
        self.assertEqual((batch["record_count"], batch["no_show_count"], batch["attendance_unknown_count"]), (3, 1, 2))
        self.assertEqual(batch["patient_status_counts"], {"OK": 2, "EMPTY": 1, "ERROR": 0, "BLOCKED": 0})
        self.assertEqual(batch["distinct_registered_by_code_count"], 2)
        for private in (*MRNS, SOURCE, "intranet.example"):
            self.assertNotIn(private, json.dumps(report))
        output = self.root.parent / "analysis"
        analyze_bundle(self.root, output_dir=output)
        text = (output / "analysis.md").read_text(encoding="utf-8")
        self.assertIn("批次掛號存檔核對", text)
        self.assertNotIn(SOURCE, text)

    def test_page_without_patient_parameter_uses_only_the_same_capture_scope(self):
        self.bundle(mrns=MRNS[:1], pages={MRNS[0]: [
            registration(MRNS[0], notes_html=ICON, next_href="?d-123-p=2"),
            registration(MRNS[0]),
        ]})
        rows = self.manifest()
        rows[1]["request"]["url"] = URL + "?d-123-p=2"
        self.write_manifest(rows)
        report = self.report()
        self.assertEqual(report["analysis_status"], "OK")
        self.assertEqual(report["registration_batch"]["raw_response_count"], 2)
        with BundleReader(self.root) as reader:
            self.assertEqual([row["status"] for row in replay_bundle(reader)], ["PARSED", "PARSED"])

        rows[1]["sdk_operation_id"] = "op-000002"
        self.write_manifest(rows)
        with BundleReader(self.root) as reader:
            self.assertEqual(replay_bundle(reader)[1]["error_code"], "REPLAY_CONTEXT_MISSING")

    def test_pagination_cannot_verify_a_foreign_origin_even_if_the_capture_matches(self):
        foreign_page = "https://outside.invalid/webmaas/RSV/RSV11W001.do?d-123-p=2"
        self.bundle(mrns=MRNS[:1], pages={MRNS[0]: [
            registration(MRNS[0], notes_html=ICON, next_href=foreign_page),
            registration(MRNS[0]),
        ]})
        rows = self.manifest()
        rows[1]["request"]["url"] = foreign_page
        self.write_manifest(rows)
        batch = self.report()["registration_batch"]
        self.assertEqual((batch["status"], batch["verified_patient_count"]), ("ERROR", 0))
        self.assertIn("REGISTRATION_PAGINATION_INCOMPLETE", {issue["code"] for issue in batch["issues"]})

    def test_capture_index_preserves_manifest_page_order(self):
        self.bundle()
        rows = self.manifest()
        rows[1], rows[2] = rows[2], rows[1]
        self.write_manifest(rows)
        batch = self.report()["registration_batch"]
        self.assertEqual((batch["status"], batch["verified_patient_count"]), ("ERROR", 2))
        self.assertIn("REGISTRATION_PAGINATION_INCOMPLETE", {issue["code"] for issue in batch["issues"]})

    def test_saved_result_mismatches_cannot_be_hidden_by_ok_summary(self):
        mutations = {
            "missing": (None, "REGISTRATION_RESULT_MISSING"),
            "patient": (lambda row: row.update(mrn="WRONG"), "REGISTRATION_RESULT_PATIENT_MISMATCH"),
            "marker": (lambda row: row.update(no_show=None), "REGISTRATION_REPLAY_MISMATCH"),
            "raw_code": (lambda row: row.update(registered_by="CHANGED"), "REGISTRATION_REPLAY_MISMATCH"),
            "false": (lambda row: row.update(no_show=False), "REGISTRATION_RESULT_INVALID"),
            "missing_marker": (lambda row: row.pop("no_show"), "REGISTRATION_RESULT_INVALID"),
        }
        self.bundle()
        path = "parsed/registrations/patients/000001.json"
        original = self.read(path)
        for label, (mutation, code) in mutations.items():
            with self.subTest(label=label):
                values = json.loads(json.dumps(original))
                if mutation is None:
                    (self.root / path).unlink()
                else:
                    mutation(values[0])
                    self.write(path, values)
                report = self.report()
                self.assertEqual(report["bundle_status"], "OK")
                self.assertEqual(report["analysis_status"], "NEEDS_ATTENTION")
                self.assertIn(code, {issue["code"] for issue in report["registration_batch"]["issues"]})
                self.write(path, original)

    def test_summary_journal_inputs_and_unexpected_files_are_checked(self):
        self.bundle()
        summary_path = "parsed/registrations/batch_summary.json"
        original = self.read(summary_path)
        for field, value in (("no_show_count", 99), ("registered_by_codes", {}), ("completed_patient_count", 0)):
            with self.subTest(field=field):
                self.write(summary_path, {**original, field: value})
                self.assertIn("REGISTRATION_SUMMARY_MISMATCH", {i["code"] for i in self.report()["problems"]})
        self.write(summary_path, original)
        index_path = "parsed/registrations/patients.json"
        self.write(index_path, list(reversed(self.read(index_path))))
        self.assertIn("REGISTRATION_INDEX_MISMATCH", {i["code"] for i in self.report()["problems"]})
        journal_path = self.root / "parsed/registrations/patient_results.jsonl"
        journal_path.write_text("", encoding="utf-8")
        self.assertIn("REGISTRATION_JOURNAL_MISMATCH", {i["code"] for i in self.report()["problems"]})
        self.write("parsed/registrations/patients/999999.json", [])
        self.assertIn("REGISTRATION_RESULT_UNEXPECTED", {i["code"] for i in self.report()["problems"]})
        config = self.read("run_config.json")
        self.write("run_config.json", {**config, "patient_mrns": list(reversed(MRNS))})
        self.assertIn("REGISTRATION_INDEX_MISMATCH", {i["code"] for i in self.report()["problems"]})

    def test_missing_capture_and_unfinished_pagination_are_not_complete(self):
        self.bundle()
        path = self.root / "capture_manifest.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        for missing in (0, 1, 2):
            with self.subTest(missing=missing):
                path.write_text("\n".join(json.dumps(row) for i, row in enumerate(rows) if i != missing), encoding="utf-8")
                report = self.report()
                codes = {i["code"] for i in report["registration_batch"]["issues"]}
                self.assertTrue(codes & {"REGISTRATION_CAPTURE_MISSING", "REGISTRATION_PAGINATION_INCOMPLETE"})
                self.assertEqual(report["analysis_status"], "NEEDS_ATTENTION")
        path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
        (self.root / rows[0]["response_file"]).unlink()
        self.assertIn("REGISTRATION_REPLAY_INCOMPLETE", {i["code"] for i in self.report()["problems"]})

    def test_unknown_only_stays_a_sample_gap_without_data_errors(self):
        self.bundle(mrns=MRNS[:1], pages={MRNS[0]: [registration(MRNS[0])]})
        report = self.report()
        self.assertEqual(report["analysis_status"], "COMPLETED_WITH_GAPS")
        self.assertEqual(report["registration_batch"]["no_show_sample_status"], "NO_SAMPLE")
        self.assertEqual(report["registration_batch"]["attendance_unknown_count"], 1)
        self.assertIsNone(report["root_cause"])

    def test_auth_failure_and_interruption_preserve_original_outcomes(self):
        cases = (
            ({MRNS[0]: LoginRejectedError("synthetic")}, "AUTHENTICATION_FAILED", 1, 1, 0),
            ({MRNS[0]: [registration(MRNS[0], notes_html=ICON)], MRNS[1]: KeyboardInterrupt()}, "INTERRUPTED", 1, 0, 1),
            ({MRNS[0]: ParseError("synthetic"), MRNS[1]: [registration(MRNS[1])]}, "COMPLETED_WITH_ERRORS", 2, 0, 0),
        )
        for i, (pages, status, completed, blocked, pending) in enumerate(cases):
            with self.subTest(status=status):
                self.root = self.root.parent / f"case-{i}"
                self.bundle(pages=pages, mrns=MRNS[:2])
                report = self.report()
                batch = report["registration_batch"]
                self.assertEqual(report["bundle_status"], status)
                self.assertEqual(report["analysis_status"], "NEEDS_ATTENTION")
                self.assertEqual(batch["status"], "OBSERVED")
                self.assertEqual((batch["completed_patient_count"], batch["blocked_patient_count"], batch["pending_patient_count"]),
                                 (completed, blocked, pending))

    def test_pack_incomplete_replays_only_consecutive_commits_beyond_the_checkpoint(self):
        mrns = (*MRNS, "TEST004")
        config = LiveTestConfig(profile="registrations", patient_mrns=mrns)
        pages = {
            MRNS[0]: [registration(MRNS[0], registered_by=SOURCE, notes_html=ICON)],
            MRNS[1]: [registration(MRNS[1], empty=True)],
            MRNS[2]: [registration(MRNS[2], registered_by="MWEB")],
            mrns[3]: SystemExit(),
        }
        cases = {
            "clean": "", "truncated": "REGISTRATION_JOURNAL_TRUNCATED",
            "duplicate": "REGISTRATION_JOURNAL_MISMATCH",
        }
        for mode, expected_issue in cases.items():
            with self.subTest(mode=mode), redirect_stdout(io.StringIO()):
                self.root = self.root.parent / f"crash-{mode}"
                manager = LiveTestBundleManager(self.root)
                self.addCleanup(manager.close_console)
                manager.write_config(config.to_safe_dict())
                self.write("environment.json", {})
                with (
                    RawCaptureRecorder(self.root, prepare_root=False, run_id=manager.run_id) as capture,
                    patch("vghks_sdk.live.registrations.CHECKPOINT_INTERVAL", 2),
                    self.assertRaises(SystemExit),
                ):
                    run_registration_test(RecordedBatchSDK(capture, pages), config,
                                          output_dir=self.root, raw_capture=capture)
                manager.close_console()
                path = "parsed/registrations/batch_summary.json"
                self.assertEqual(self.read(path)["completed_patient_count"], 2)
                self.assertFalse((self.root / "run_summary.json").exists())

                journal_path = self.root / "parsed/registrations/patient_results.jsonl"
                committed = journal_path.read_bytes()
                last = committed.splitlines(keepends=True)[-1]
                entry = json.loads(last)
                next_line = json.dumps({"index": 4, "mrn": mrns[3], "registered_by_codes": {}, "step": {
                    **entry["step"], "name": "registrations.patient_000004", "status": "BLOCKED",
                    "output": "", "details": None,
                }}).encode()
                tail = b""
                if mode == "truncated":
                    tail = next_line
                elif mode == "duplicate":
                    tail = last + next_line + b"\n"
                if tail:
                    with journal_path.open("ab") as handle:
                        handle.write(tail)

                archive = pack_incomplete_run(self.root, archive_directory=Path(self.temp.name))
                with BundleReader(archive.archive_path) as reader:
                    report = inspect_bundle(reader)[0]
                    summary = reader.json("run_summary.json")
                    self.assertEqual(summary["steps"], reader.json("step_results.json"))
                    self.assertEqual(summary["steps"][-1]["name"], "registrations.patient_000003")
                    self.assertTrue(any(step["name"] == "auth_check.portal" for step in summary["steps"]))
                    self.assertEqual(reader.read("parsed/registrations/patient_results.jsonl"), committed + tail)
                self.assertEqual(journal_path.read_bytes(), committed + tail)
                self.assertEqual(report["bundle_status"], "INTERRUPTED")
                batch = report["registration_batch"]
                self.assertEqual((batch["completed_patient_count"], batch["pending_patient_count"],
                                  batch["verified_patient_count"], batch["distinct_registered_by_code_count"]), (3, 1, 3, 2))
                self.assertEqual({i["code"] for i in batch["issues"]}, {expected_issue} if expected_issue else set())

    def test_journal_write_failure_packages_committed_prefix_and_stops_queries(self):
        config = LiveTestConfig(profile="registrations", patient_mrns=MRNS)
        pages = {mrn: [registration(mrn, notes_html=ICON)] for mrn in MRNS}
        sdk = RecordedBatchSDK(None, pages)

        def sdk_context(*, raw_capture, **_):
            sdk.capture = raw_capture
            return nullcontext(sdk)

        def append(handle, entry, *, durable):
            if entry["index"] == 2:
                with patch("vghks_sdk.local_io.os.fsync", side_effect=[OSError("synthetic sync failure"), None]):
                    write_jsonl_line(handle, entry, durable=durable)
            else:
                write_jsonl_line(handle, entry, durable=durable)

        console = io.StringIO()
        with (
            patch("vghks_sdk.live.runner.VghksSDK", side_effect=sdk_context),
            patch("vghks_sdk.live.runner.environment_report", return_value={}),
            patch("vghks_sdk.live.registrations.run_network_checks"),
            patch("vghks_sdk.live.registrations.write_jsonl_line", side_effect=append),
            redirect_stdout(console), redirect_stderr(console),
        ):
            execution = execute_live_test(config, PortalCredentials("SYNTHETIC", "SYNTHETIC-ONLY"),
                                          output_dir=self.root, announce_archive=False)
        self.assertEqual((execution.status, execution.error_code), ("BOOTSTRAP_FAILED", "OUTPUT_WRITE_FAILED"))
        self.assertEqual(sdk.calls, list(MRNS[:2]))
        self.assertNotIn("OK registrations.patient_000002", console.getvalue())
        with BundleReader(execution.archive.archive_path) as reader:
            report = inspect_bundle(reader)[0]
            self.assertEqual(reader.json("run_summary.json")["steps"][-1]["name"], "registrations.patient_000001")
        batch = report["registration_batch"]
        self.assertEqual((batch["completed_patient_count"], batch["pending_patient_count"],
                          batch["verified_patient_count"]), (1, 2, 1))
        self.assertIn("REGISTRATION_RESULT_UNEXPECTED", {i["code"] for i in batch["issues"]})

    def test_old_journal_without_version_retains_the_same_offline_result(self):
        self.bundle()
        original = self.report()["registration_batch"]
        summary_path = "parsed/registrations/batch_summary.json"
        summary = self.read(summary_path)
        del summary["patient_journal_version"]
        self.write(summary_path, summary)
        journal = self.root / "parsed/registrations/patient_results.jsonl"
        entries = [json.loads(line) for line in journal.read_bytes().splitlines()]
        for entry in entries:
            del entry["registered_by_codes"]
        journal.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
        self.assertEqual(self.report()["registration_batch"], original)

    def test_standalone_replay_checks_patient_and_counts_only_safe_fields(self):
        for expected, state in ((MRNS[0], "PARSED"), (MRNS[1], "PARSE_ERROR")):
            result = replay_response("webmaas.registration_query", registration(MRNS[0], registered_by=SOURCE, notes_html=ICON).encode("cp950", errors="xmlcharrefreplace"),
                                     {"patno": expected}, mime="text/html; charset=big5")
            self.assertEqual(result["status"], state)
            self.assertNotIn(SOURCE, json.dumps(result))
            if state == "PARSED":
                self.assertEqual((result["no_show_count"], result["attendance_unknown_count"]), (1, 0))
            else:
                self.assertEqual(result["error_code"], "WEBMAAS_PATIENT_MISMATCH")


if __name__ == "__main__":
    unittest.main()
