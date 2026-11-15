import re
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from app.config import AppPaths
from app.discovery.models import (
    DiscoveryRun,
    ExtractionIssue,
    JobExtractionResult,
    JobExtractionStats,
    JobIdentity,
    JobTechMention,
)
from app.models import Job


class DiscoveryModelTests(unittest.TestCase):
    def test_app_paths_create_isolated_discovery_locations(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            paths = AppPaths(Path(temp_dir))

            self.assertEqual(paths.discovery_database, paths.data / 'discovery.sqlite3')
            self.assertEqual(paths.discovery_config, paths.config / 'discovery')
            self.assertEqual(paths.discovery_exports, paths.exports / 'discovery')
            self.assertTrue(paths.discovery_config.is_dir())
            self.assertTrue(paths.discovery_exports.is_dir())

    def test_job_identity_copies_only_stable_identity_fields(self):
        job = Job.from_mapping({
            'title': 'Python 后端工程师',
            'company_name': '示例公司',
            'source': '51job',
            'source_job_id': 'job-42',
            'source_url': 'https://example.com/job/42?utm_source=test',
            'city': '北京',
            'description': '联系电话：13800138000',
        })

        identity = JobIdentity.from_job(job)

        self.assertEqual(identity.record_key, job.record_key)
        self.assertEqual(identity.source, '51job')
        self.assertEqual(identity.source_job_id, 'job-42')
        self.assertEqual(identity.source_url, 'https://example.com/job/42')
        self.assertEqual(identity.title, 'Python 后端工程师')
        self.assertEqual(identity.company_name, '示例公司')
        self.assertEqual(identity.city, '北京')
        self.assertFalse(hasattr(identity, 'description'))

    def test_job_identity_drops_sensitive_query_from_legacy_job(self):
        job = Job(
            title='Python 工程师',
            company_name='示例公司',
            source='legacy',
            source_job_id='42',
            source_url='https://example.com/job/42?id=42&access_token=SYNTHETIC_TEST_ONLY',
        )

        identity = JobIdentity.from_job(job)

        self.assertEqual(identity.source_url, 'https://example.com/job/42?id=42')

    def test_discovery_run_has_unique_id_and_timezone_aware_start(self):
        first = DiscoveryRun.start('job_tech', 'dict.v1', 'extractor.v1', ('b', 'a'))
        second = DiscoveryRun.start('job_tech', 'dict.v1', 'extractor.v1', ('a', 'b'))

        self.assertNotEqual(first.run_id, second.run_id)
        self.assertEqual(first.status, 'running')
        self.assertEqual(first.record_keys, ('a', 'b'))
        self.assertEqual(first.completed_at, '')
        parsed = datetime.fromisoformat(first.started_at)
        self.assertIsNotNone(parsed.tzinfo)

    def test_job_tech_mention_rejects_invalid_boundary_values(self):
        identity = JobIdentity(
            record_key='job-key', source='51job', source_job_id='42',
            source_url='https://example.com/job/42', title='Python 工程师',
            company_name='示例公司', city='北京',
        )
        valid = {
            'run_id': 'run-1',
            'record_key': 'job-key',
            'tech_key': 'python',
            'source_fields': ('title',),
            'evidence_excerpt': 'Python 工程师',
            'job_content_hash': 'a' * 64,
            'extractor_version': 'extractor.v1',
            'dictionary_version': 'dict.v1',
            'review_state': 'pending',
            'job_identity': identity,
        }

        mention = JobTechMention(**valid)
        self.assertEqual(mention.tech_key, 'python')
        self.assertTrue(re.fullmatch(r'[a-z0-9][a-z0-9-]{1,63}', mention.tech_key))

        invalid_values = (
            ('record_key', ''),
            ('tech_key', 'Python!'),
            ('source_fields', ()),
            ('source_fields', ('title', 'company_name')),
            ('review_state', 'published'),
        )
        for field_name, value in invalid_values:
            with self.subTest(field_name=field_name, value=value):
                values = dict(valid)
                values[field_name] = value
                with self.assertRaises(ValueError):
                    JobTechMention(**values)

    def test_extraction_stats_and_result_are_typed_and_non_negative(self):
        stats = JobExtractionStats(scanned_jobs=2, matched_jobs=1, job_mentions=1)
        issue = ExtractionIssue(
            run_id='run-1',
            record_key='job-key',
            issue_code='review_required',
            matched_alias='AI',
            candidate_tech_keys=('artificial-intelligence',),
            evidence_excerpt='AI 应用开发',
        )
        result = JobExtractionResult(mentions=(), issues=(issue,), stats=stats)

        self.assertEqual(result.stats.scanned_jobs, 2)
        self.assertEqual(result.issues[0].issue_code, 'review_required')
        with self.assertRaises(ValueError):
            JobExtractionStats(scanned_jobs=-1)


if __name__ == '__main__':
    unittest.main()
