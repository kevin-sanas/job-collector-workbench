import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.discovery.batches import build_job_tech_batch, export_batch, serialize_batch
from app.discovery.models import (
    DiscoveryRun,
    ExtractionIssue,
    JobExtractionStats,
    JobIdentity,
    JobTechMention,
)


class DiscoveryBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        started = DiscoveryRun.start('job_tech', 'tech-dict.test.1', 'job-tech-rule.v1', ('job-b', 'job-a'))
        self.run = replace(
            started,
            status='completed',
            completed_at='2026-10-26T12:00:00+08:00',
            stats=JobExtractionStats(scanned_jobs=2, matched_jobs=2, job_mentions=2),
        )

    def mention(self, record_key: str, tech_key: str, evidence: str = '中文证据') -> JobTechMention:
        identity = JobIdentity(
            record_key=record_key,
            source='test',
            source_job_id=record_key,
            source_url=f'https://example.com/{record_key}',
            title='Python 工程师',
            company_name='示例公司',
            city='北京',
        )
        return JobTechMention(
            run_id=self.run.run_id,
            record_key=record_key,
            tech_key=tech_key,
            source_fields=('title', 'description'),
            evidence_excerpt=evidence,
            job_content_hash='a' * 64,
            extractor_version=self.run.extractor_version,
            dictionary_version=self.run.dictionary_version,
            review_state='pending',
            job_identity=identity,
        )

    def issue(self, record_key: str) -> ExtractionIssue:
        return ExtractionIssue(
            run_id=self.run.run_id,
            record_key=record_key,
            issue_code='review_required',
            matched_alias='AI',
            candidate_tech_keys=('artificial-intelligence',),
            evidence_excerpt='AI 应用',
        )

    def test_builds_required_contract_with_identity_and_stable_order(self):
        mentions = [
            self.mention('job-b', 'python', '=HYPERLINK("bad")'),
            self.mention('job-a', 'java'),
        ]
        issues = [self.issue('job-b'), self.issue('job-a')]

        batch = build_job_tech_batch(self.run, mentions, issues)
        payload = batch.to_dict()

        self.assertEqual(set(payload), {
            'schema_version', 'run_id', 'trigger', 'generated_at',
            'dictionary_version', 'extractor_version', 'stats',
            'job_mentions', 'content_candidates', 'issues',
        })
        self.assertEqual(payload['schema_version'], 'discovery-batch.v0.1')
        self.assertEqual(payload['trigger'], 'manual_desktop')
        self.assertEqual(payload['generated_at'], self.run.completed_at)
        self.assertEqual(payload['content_candidates'], [])
        self.assertEqual(
            [(item['record_key'], item['tech_key']) for item in payload['job_mentions']],
            [('job-a', 'java'), ('job-b', 'python')],
        )
        self.assertEqual(payload['job_mentions'][0]['job_identity']['company_name'], '示例公司')
        self.assertEqual([item['record_key'] for item in payload['issues']], ['job-a', 'job-b'])
        self.assertEqual(payload['job_mentions'][1]['evidence_excerpt'], '=HYPERLINK("bad")')

    def test_empty_completed_run_is_valid_and_serialization_is_deterministic(self):
        empty_run = replace(self.run, stats=JobExtractionStats(scanned_jobs=0))
        batch = build_job_tech_batch(empty_run, [], [])

        first = serialize_batch(batch)
        second = serialize_batch(batch)

        self.assertEqual(first, second)
        self.assertTrue(first.endswith(b'\n'))
        decoded = json.loads(first)
        self.assertEqual(decoded['job_mentions'], [])
        self.assertEqual(decoded['issues'], [])

    def test_serialization_keeps_utf8_chinese_text(self):
        batch = build_job_tech_batch(
            self.run, [self.mention('job-a', 'python', '中文证据')], []
        )

        serialized = serialize_batch(batch)

        self.assertIn('中文证据'.encode('utf-8'), serialized)

    def test_export_returns_sha256_and_failed_replace_preserves_existing_file(self):
        batch = build_job_tech_batch(self.run, [self.mention('job-a', 'python')], [])
        path = self.root / 'batch.json'

        digest = export_batch(batch, path)

        self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())
        previous = path.read_bytes()
        with patch.object(Path, 'replace', side_effect=OSError('simulated replace failure')):
            with self.assertRaises(OSError):
                export_batch(batch, path)
        self.assertEqual(path.read_bytes(), previous)
        self.assertEqual(list(self.root.glob('.*.tmp')), [])

    def test_rejects_running_or_mismatched_run_data(self):
        running = replace(self.run, status='running', completed_at='')
        with self.assertRaises(ValueError):
            build_job_tech_batch(running, [], [])
        wrong = replace(self.mention('job-a', 'python'), run_id='other-run')
        with self.assertRaises(ValueError):
            build_job_tech_batch(self.run, [wrong], [])


if __name__ == '__main__':
    unittest.main()
