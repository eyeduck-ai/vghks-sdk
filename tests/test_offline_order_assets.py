from __future__ import annotations

import json
import socket
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from vghks_sdk.local_io import write_json_atomic
from vghks_sdk.models import BinaryAsset, to_jsonable
from vghks_sdk.offline.analyze import inspect_bundle
from vghks_sdk.offline.assets import ORDER_WORKFLOW, verify_order_assets
from vghks_sdk.offline.bundle import BundleReader

PDF = b"%PDF-1.4\nsynthetic attachment\n%%EOF"
JPEG = b"\xff\xd8synthetic image\xff\xd9"
SECRET = "PRIVATE_REFERENCE_MUST_NOT_APPEAR"


class OfflineOrderAssetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        write_json_atomic(self.root / "run_summary.json", {
            "schema_version": 6, "status": "OK", "profile": "dbr", "steps": [
                {"name": "dbr.pdf", "operation": "prq.pdf_attachment", "status": "OK"},
            ],
        })
        (self.root / "capture_manifest.jsonl").write_text("", encoding="utf-8")
        write_json_atomic(self.root / "run_config.json", {"profile": "dbr"})
        write_json_atomic(self.root / ORDER_WORKFLOW / "manifest.json", {
            "status": "OK", "counts": {"pdf_files": 1}, "coverage": {"DBR": {"selected": 1}},
        })
        self.files = self.root / ORDER_WORKFLOW / "stage3_assets"

    def asset(self, *, content=PDF, operation="prq.pdf_attachment", number=1, audit_hash=True):
        asset = BinaryAsset(content, "application/pdf" if operation.endswith("pdf_attachment") else "image/jpeg")
        stem = f"{number:04d}-{operation.removeprefix('prq.')}"
        path = self.files / (stem + (".pdf" if asset.media_type == "application/pdf" else ".jpg"))
        metadata = self.files / (stem + ".json")
        write_json_atomic(metadata, asset)
        path.write_bytes(content)
        audit = {
            "operation": operation, "status": "OK", "ref": SECRET, "query": SECRET,
            "asset": "stage3_assets/" + path.name, "byte_count": asset.size,
        }
        if audit_hash:
            audit["sha256"] = asset.sha256
        outcome = self.files / (stem + ".outcome.json")
        write_json_atomic(outcome, audit)
        return path, metadata, outcome

    def inspect(self):
        with patch.object(socket, "socket", side_effect=AssertionError("offline socket")), BundleReader(self.root) as reader:
            report, config = inspect_bundle(reader)
        self.assertNotIn(SECRET, json.dumps(report))
        return report, config

    def test_original_hashless_audit_and_new_pdf_jpeg_audits_verify_in_zip(self):
        self.asset(audit_hash=False)
        self.asset(content=JPEG, operation="prq.pacs_image", number=2)
        _, metadata, outcome = self.asset(number=3)
        # BinaryAsset accepts an explicitly supplied uppercase SHA-256 too.
        payload = json.loads(metadata.read_text(encoding="utf-8"))
        payload["sha256"] = payload["sha256"].upper()
        write_json_atomic(metadata, payload)
        payload = json.loads(outcome.read_text(encoding="utf-8"))
        payload["sha256"] = payload["sha256"].upper()
        write_json_atomic(outcome, payload)
        archive = self.root.parent / (self.root.name + ".zip")
        self.addCleanup(archive.unlink, missing_ok=True)
        with zipfile.ZipFile(archive, "w") as output:
            for path in self.root.rglob("*"):
                if path.is_file():
                    output.write(path, path.relative_to(self.root).as_posix())
        with patch.object(socket, "socket", side_effect=AssertionError("offline socket")), BundleReader(archive) as reader:
            result = verify_order_assets(reader)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["checked_files"], 3)
        self.assertEqual(result["pdf_files"], 2)
        self.assertEqual(result["jpg_files"], 1)
        self.assertEqual(result["total_bytes"], len(PDF) * 2 + len(JPEG))
        self.assertNotIn(SECRET, json.dumps(result))

    def test_changed_saved_file_is_error_without_rewriting_original_live_result(self):
        path, _, _ = self.asset()
        path.write_bytes(PDF.replace(b"synthetic", b"corrupted"))
        report, config = self.inspect()
        self.assertEqual(report["bundle_status"], "OK")
        self.assertEqual(report["analysis_status"], "NEEDS_ATTENTION")
        self.assertEqual(report["root_cause"]["code"], "ORDER_ASSET_HASH_MISMATCH")
        pdf = next(row for row in report["operations"] if row["operation"] == "prq.pdf_attachment")
        self.assertEqual(pdf["live_status"], "VERIFIED")
        self.assertEqual(config["profile"], "dbr")
        self.assertEqual(report["order_assets"]["pdf_files"], 0)

    def test_missing_truncated_and_invalid_format_are_distinct(self):
        path, metadata, _ = self.asset()
        for code, action in (
            ("ORDER_ASSET_MISSING", lambda: path.unlink()),
            ("ORDER_ASSET_SIZE_MISMATCH", lambda: path.write_bytes(PDF[:-1])),
            ("ORDER_ASSET_FORMAT_INVALID", lambda: (
                path.write_bytes(b"invalid boundary"),
                write_json_atomic(metadata, BinaryAsset(b"invalid boundary", "application/pdf")),
                write_json_atomic(metadata.with_name(metadata.stem + ".outcome.json"), {
                    "operation": "prq.pdf_attachment", "status": "OK", "asset": "stage3_assets/" + path.name,
                    "byte_count": len(b"invalid boundary"),
                }),
            )),
        ):
            with self.subTest(code=code):
                self.asset()
                action()
                report, _ = self.inspect()
                self.assertEqual(report["root_cause"]["code"], code)

    def test_metadata_missing_or_inconsistent_never_counts_as_verified_file(self):
        _, metadata, outcome = self.asset()
        for code, action in (
            ("ORDER_ASSET_METADATA_MISSING", lambda: metadata.unlink()),
            ("ORDER_ASSET_METADATA_INVALID", lambda: metadata.write_text("{", encoding="utf-8")),
            ("ORDER_ASSET_METADATA_INVALID", lambda: write_json_atomic(metadata, {
                **to_jsonable(BinaryAsset(PDF, "application/pdf")), "sha256": SECRET,
            })),
            ("ORDER_ASSET_METADATA_INVALID", lambda: write_json_atomic(outcome, {
                "operation": "prq.pdf_attachment", "status": "OK", "asset": "stage3_assets/0001-pdf_attachment.pdf",
                "byte_count": len(PDF), "sha256": "0" * 64,
            })),
        ):
            with self.subTest(code=code):
                self.asset()
                action()
                report, _ = self.inspect()
                self.assertEqual(report["root_cause"]["code"], code)
                self.assertEqual(report["order_assets"]["pdf_files"], 0)

    def test_audit_cannot_redirect_validation_to_another_file(self):
        _, _, outcome = self.asset()
        write_json_atomic(outcome, {
            "operation": "prq.pdf_attachment", "status": "OK", "asset": "../../" + SECRET,
            "byte_count": len(PDF),
        })
        report, _ = self.inspect()
        self.assertEqual(report["root_cause"]["code"], "ORDER_ASSET_PATH_INVALID")

    def test_missing_outcome_is_not_disguised_as_no_attachments(self):
        _, _, outcome = self.asset()
        outcome.unlink()
        report, _ = self.inspect()
        self.assertEqual(report["root_cause"]["code"], "ORDER_ASSET_OUTCOME_MISSING")
        # Successful live steps also require evidence when all asset files
        # disappear; a NO_ASSETS label must not hide the missing output.
        for path in self.files.iterdir():
            path.unlink()
        write_json_atomic(self.root / "run_summary.json", {
            "schema_version": 6, "status": "OK", "profile": "dbr", "steps": [],
        })
        write_json_atomic(self.root / "step_results.json", [{
            "operation": "prq.pdf_attachment", "status": "OK",
            "output": ORDER_WORKFLOW + "stage3_assets/0001-pdf_attachment.json",
        }])
        report, _ = self.inspect()
        self.assertEqual(report["root_cause"]["code"], "ORDER_ASSET_OUTCOME_MISSING")

    def test_failed_download_has_no_saved_file_requirement_or_synthetic_success(self):
        _, _, outcome = self.asset()
        write_json_atomic(outcome, {
            "operation": "prq.pdf_attachment", "status": "ERROR", "ref": SECRET,
        })
        with BundleReader(self.root) as reader:
            result = verify_order_assets(reader)
        self.assertEqual(result["status"], "NO_ASSETS")
        self.assertEqual(result["checked_files"], 0)
        self.assertEqual(result["issues"], [])


if __name__ == "__main__":
    unittest.main()
