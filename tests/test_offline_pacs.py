"""Synthetic PACS target, missing-reference and saved-byte regression evidence."""

from __future__ import annotations

import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_pacs_img_url import image_url

from vghks_sdk.contracts.order_assets import ORDER_WORKFLOW_PATH
from vghks_sdk.local_io import write_json_atomic
from vghks_sdk.models import BinaryAsset, PacsImageRef, to_jsonable
from vghks_sdk.offline.analyze import _markdown, inspect_bundle
from vghks_sdk.offline.bundle import BundleReader
from vghks_sdk.offline.pacs import pacs_test_summary

MRN = 'SYNTHETIC'
IMG = PacsImageRef(MRN, 'SYNTHETIC-REQUEST', img_url=image_url())
UID = PacsImageRef(MRN, 'SYNTHETIC-REQUEST', 'SERIES', 'STUDY', '1.2.3')
SECRET = 'PRIVATE_INPUT_MUST_NOT_LEAVE_BUNDLE'
JPEG = b'\xff\xd8synthetic image\xff\xd9'


class OfflinePacsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workflow = self.root / ORDER_WORKFLOW_PATH
        self.steps = [
            {'name': 'image', 'operation': 'prq.pacs_image', 'status': 'OK'},
            {'name': 'pacs.uid_sample', 'operation': 'prq.pacs_image', 'status': 'NO_SAMPLE'},
        ]
        self.save(self.root / 'run_summary.json', {
            'schema_version': 6, 'profile': 'pacs', 'status': 'COMPLETED_WITH_GAPS', 'steps': self.steps, 'test_mrn': MRN,
        })
        self.save(self.root / 'run_config.json', {'profile': 'pacs', 'download_assets': True})
        self.save(self.root / 'pacs_account.json', {
            'hospital': 'union', 'expected_mode': 'imgUrl', 'unrecognized': SECRET,
        })
        (self.root / 'capture_manifest.jsonl').write_text('', encoding='utf-8')
        self.references([IMG])
        self.asset(IMG)
        self.summary()

    def save(self, path, value):
        write_json_atomic(path, value)

    def references(self, refs):
        self.save(self.workflow / 'stage2_orders/0001.json', {'image_refs': to_jsonable(refs), 'unused': SECRET})

    def asset(self, ref, number=1, *, status='OK'):
        stem = self.workflow / f'stage3_assets/{number:04d}-pacs_image'
        asset = BinaryAsset(JPEG, 'image/jpeg')
        self.save(stem.with_suffix('.json'), asset)
        stem.with_suffix('.jpg').write_bytes(JPEG)
        outcome = stem.with_suffix('.outcome.json')
        self.save(outcome, {
            'operation': 'prq.pacs_image', 'status': status, 'ref': to_jsonable(ref),
            'asset': 'stage3_assets/' + stem.name + '.jpg', 'byte_count': asset.size, 'sha256': asset.sha256,
        })
        return stem.with_suffix('.jpg'), outcome

    def summary(self, *, modes=None, download_assets=True):
        self.save(self.workflow / 'pacs_verification.json', {
            'schema_version': 1, 'download_assets': download_assets,
            'modes': modes or {
                'imgUrl': {'status': 'OK', 'references': 1, 'downloaded_files': 1,
                           'download_errors': 0, 'downloaded_bytes': len(JPEG)},
                'uid': {'status': 'NO_SAMPLE', 'references': 0, 'downloaded_files': 0,
                        'download_errors': 0, 'downloaded_bytes': 0},
            },
        })

    def inspect(self):
        with patch.object(socket, 'socket', side_effect=AssertionError('offline socket')), BundleReader(self.root) as reader:
            report, _ = inspect_bundle(reader)
        self.assertNotIn(SECRET, json.dumps(report))
        self.assertNotIn(image_url(), json.dumps(report))
        return report

    def codes(self, report):
        return {row['code'] for row in report['problems']}

    def test_original_gaps_and_missing_mode_remain_distinct_from_expected_success(self):
        # The same reference discovered twice still requires one download.
        self.references([IMG, IMG])
        report = self.inspect()
        self.assertEqual(report['bundle_status'], 'COMPLETED_WITH_GAPS')
        self.assertEqual(report['analysis_status'], 'COMPLETED_WITH_GAPS')
        self.assertIsNone(report['root_cause'])
        pacs = report['pacs_test']
        self.assertEqual(pacs['expected_mode_status'], 'OK')
        self.assertEqual(pacs['modes']['imgUrl']['verified_files'], 1)
        self.assertEqual(pacs['modes']['uid']['status'], 'NO_SAMPLE')
        self.assertEqual(report['no_sample_steps'][0]['step'], 'pacs.uid_sample')
        self.assertEqual(report['no_sample_steps'][0]['reason_code'], 'NO_PACS_UID_REFERENCE')
        self.assertIn('union.imgUrl = OK', _markdown(report))

    def test_both_modes_are_audited_independently_with_uid_target(self):
        self.references([IMG, UID])
        self.asset(UID, 2)
        self.steps[1]['status'] = 'OK'
        self.save(self.root / 'run_summary.json', {'schema_version': 6, 'profile': 'pacs',
                  'status': 'COMPLETED_WITH_GAPS', 'steps': self.steps})
        modes = {mode: {'status': 'OK', 'references': 1, 'downloaded_files': 1,
                        'download_errors': 0, 'downloaded_bytes': len(JPEG)} for mode in ('imgUrl', 'uid')}
        self.summary(modes=modes)
        self.save(self.root / 'pacs_account.json', {'hospital': 'vghks', 'expected_mode': 'uid'})
        report = self.inspect()
        self.assertEqual(report['pacs_test']['expected_mode_status'], 'OK')
        self.assertEqual(report['pacs_test']['modes']['uid']['verified_bytes'], len(JPEG))
        path, _ = self.asset(IMG)
        path.write_bytes(JPEG.replace(b'synthetic', b'corrupted'))
        report = self.inspect()
        self.assertEqual(report['pacs_test']['modes']['uid']['status'], 'OK')
        self.assertEqual(report['pacs_test']['modes']['imgUrl']['status'], 'ERROR')
        self.assertEqual(report['pacs_test']['expected_mode_status'], 'ERROR')
        self.assertEqual(report['bundle_status'], 'COMPLETED_WITH_GAPS')
        self.assertEqual(report['analysis_status'], 'NEEDS_ATTENTION')

    def test_lost_outcome_cannot_hide_an_undownloaded_reference(self):
        _, outcome = self.asset(IMG)
        for path in outcome.parent.iterdir():
            path.unlink()
        report = self.inspect()
        self.assertIn('PACS_DOWNLOAD_MISSING', self.codes(report))
        self.assertEqual(report['pacs_test']['modes']['imgUrl']['missing_downloads'], 1)
        self.assertEqual(report['pacs_test']['expected_mode_status'], 'ERROR')

    def test_missing_saved_file_and_changed_bytes_are_not_verified(self):
        for action, code in (
            (lambda p: p.unlink(), 'ORDER_ASSET_MISSING'),
            (lambda p: p.write_bytes(JPEG[:-1]), 'ORDER_ASSET_SIZE_MISMATCH'),
            (lambda p: p.write_bytes(JPEG.replace(b'synthetic', b'corrupted')), 'ORDER_ASSET_HASH_MISMATCH'),
        ):
            with self.subTest(code=code):
                path, _ = self.asset(IMG)
                action(path)
                report = self.inspect()
                self.assertIn(code, self.codes(report))
                self.assertEqual(report['pacs_test']['modes']['imgUrl']['verified_files'], 0)

    def test_forged_summary_missing_summary_and_invalid_metadata_require_attention(self):
        path = self.workflow / 'pacs_verification.json'
        for data, code in (
            ({'download_assets': True, 'modes': {'imgUrl': {'status': 'OK', 'references': 999}}}, 'PACS_SUMMARY_MISMATCH'),
            ({'modes': [SECRET]}, 'PACS_SUMMARY_INVALID'),
            ({'download_assets': True, 'modes': {'imgUrl': {'status': [SECRET]}}}, 'PACS_SUMMARY_MISMATCH'),
            (None, 'PACS_SUMMARY_MISSING'),
        ):
            with self.subTest(code=code):
                if data is None:
                    path.unlink()
                else:
                    self.save(path, data)
                report = self.inspect()
                self.assertIn(code, self.codes(report))
                self.assertEqual(report['pacs_test']['modes']['imgUrl']['references'], 1)
                self.assertEqual(report['pacs_test']['expected_mode_status'], 'ERROR')

    def test_duplicate_unexpected_and_invalid_references_are_reported_without_input_values(self):
        self.asset(IMG, 2)
        self.asset(UID, 3)
        self.save(self.workflow / 'stage2_orders/0002.json', {'image_refs': [{'mrn': SECRET}]})
        report = self.inspect()
        self.assertTrue({'PACS_DOWNLOAD_DUPLICATE', 'PACS_DOWNLOAD_UNEXPECTED', 'PACS_REFERENCES_INVALID'} <= self.codes(report))
        self.assertEqual(report['pacs_test']['expected_mode_status'], 'ERROR')

    def test_bad_account_metadata_cannot_assign_a_successful_target(self):
        for account in ({'hospital': SECRET, 'expected_mode': 'imgUrl'},
                        {'hospital': 'union', 'expected_mode': 'uid'}, {'hospital': []}):
            with self.subTest(account=account):
                self.save(self.root / 'pacs_account.json', account)
                report = self.inspect()
                self.assertIn('PACS_ACCOUNT_INVALID', self.codes(report))
                self.assertEqual(report['pacs_test']['expected_mode_status'], 'NOT_ASSIGNED')

    def test_original_single_account_run_has_no_invented_hospital_target(self):
        (self.root / 'pacs_account.json').unlink()
        report = self.inspect()
        self.assertEqual(report['pacs_test']['expected_mode_status'], 'NOT_ASSIGNED')
        self.assertEqual(report['pacs_test']['modes']['imgUrl']['status'], 'OK')
        self.assertIsNone(report['root_cause'])

    def test_account_labels_must_match_the_original_run_summary(self):
        self.save(self.root / 'run_summary.json', {
            'schema_version': 6, 'profile': 'pacs', 'status': 'COMPLETED_WITH_GAPS', 'steps': self.steps,
            'pacs_account': {'hospital': 'vghks', 'expected_mode': 'uid', 'unused': SECRET},
        })
        report = self.inspect()
        self.assertIn('PACS_ACCOUNT_MISMATCH', self.codes(report))
        self.assertEqual(report['pacs_test']['expected_mode_status'], 'ERROR')

    def test_images_for_a_different_patient_never_verify_the_run_target(self):
        other = PacsImageRef('OTHER-SYNTHETIC', IMG.request_no, img_url=image_url())
        self.references([other])
        self.asset(other)
        report = self.inspect()
        self.assertIn('PACS_REFERENCE_PATIENT_MISMATCH', self.codes(report))
        self.assertEqual(report['pacs_test']['expected_mode_status'], 'ERROR')

    def test_missing_reference_with_upstream_failure_is_blocked(self):
        self.references([])
        for p in (self.workflow / 'stage3_assets').iterdir():
            p.unlink()
        self.steps.append({'name': 'history', 'operation': 'prq.order_history', 'status': 'ERROR',
                           'issue': {'code': 'HISTORY_FAILED', 'category': 'PARSE'}})
        self.save(self.root / 'run_summary.json', {'schema_version': 6, 'profile': 'pacs',
                  'status': 'COMPLETED_WITH_ERRORS', 'steps': self.steps})
        self.summary(modes={mode: {'status': 'BLOCKED', 'references': 0, 'downloaded_files': 0,
                                 'download_errors': 0, 'downloaded_bytes': 0} for mode in ('imgUrl', 'uid')})
        report = self.inspect()
        self.assertEqual(report['pacs_test']['expected_mode_status'], 'BLOCKED')
        self.assertEqual(report['bundle_status'], 'COMPLETED_WITH_ERRORS')
        self.assertEqual(report['analysis_status'], 'NEEDS_ATTENTION')

    def test_reference_only_run_is_skipped_and_does_not_claim_download_success(self):
        for p in (self.workflow / 'stage3_assets').iterdir():
            p.unlink()
        self.save(self.root / 'run_config.json', {'profile': 'pacs', 'download_assets': False})
        self.summary(download_assets=False, modes={
            'imgUrl': {'status': 'SKIPPED', 'references': 1, 'downloaded_files': 0,
                       'download_errors': 0, 'downloaded_bytes': 0},
            'uid': {'status': 'NO_SAMPLE', 'references': 0, 'downloaded_files': 0,
                    'download_errors': 0, 'downloaded_bytes': 0},
        })
        report = self.inspect()
        self.assertEqual(report['pacs_test']['expected_mode_status'], 'SKIPPED')
        self.assertEqual(report['pacs_test']['issues'], [])

    def test_non_pacs_profile_does_not_require_pacs_summary(self):
        (self.workflow / 'pacs_verification.json').unlink()
        self.save(self.root / 'run_summary.json', {'schema_version': 6, 'profile': 'dbr', 'status': 'OK', 'steps': []})
        with BundleReader(self.root) as reader:
            result = pacs_test_summary(reader, [])
        self.assertEqual(result, {'status': 'NOT_RUN', 'issues': []})

    def test_recorded_partial_text_retains_a_gap_without_revoking_valid_images(self):
        steps = [{'name': 'report', 'operation': 'prq.order_report', 'status': 'OK',
                  'details': {'acquisition_status': 'PARTIAL', 'parsing_issues': ['JS_DOCUMENT_WRITE_UNSUPPORTED']}}]
        self.save(self.root / 'run_summary.json', {'schema_version': 6, 'profile': 'pacs',
                  'status': 'OK', 'steps': steps})
        report = self.inspect()
        self.assertEqual(report['bundle_status'], 'OK')
        self.assertEqual(report['analysis_status'], 'COMPLETED_WITH_GAPS')
        self.assertEqual(report['pacs_test']['expected_mode_status'], 'OK')
        self.assertEqual(len(report['data_quality']['recorded_partial_steps']), 1)
        self.assertIsNone(report['root_cause'])


if __name__ == '__main__':
    unittest.main()
