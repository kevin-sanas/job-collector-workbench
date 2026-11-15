import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from app.collector import Cancelled, TaskControl
from app.discovery.service import JobDiscoveryService
from app.discovery.storage import DiscoveryRepository
from app.models import Job
from app.storage import Repository


class DiscoveryServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.job_repository = Repository(root / 'jobs.sqlite3')
        self.discovery_repository = DiscoveryRepository(root / 'discovery.sqlite3')
        self.dictionary_path = root / 'tech_dictionary.json'
        self.dictionary_path.write_text(json.dumps({
            'dictionary_version': 'tech-dict.test.1',
            'terms': [
                {
                    'tech_key': 'python', 'display_name': 'Python', 'aliases': ['Python'],
                    'category': '语言', 'enabled': True, 'match_mode': 'token',
                },
                {
                    'tech_key': 'artificial-intelligence', 'display_name': '人工智能',
                    'aliases': ['AI'], 'category': '方向', 'enabled': True,
                    'match_mode': 'review',
                },
            ],
        }, ensure_ascii=False), encoding='utf-8')
        self.service = JobDiscoveryService(
            self.job_repository, self.discovery_repository, self.dictionary_path
        )

    def add_job(self, source_job_id: str, title: str, description: str = '') -> Job:
        job = Job.from_mapping({
            'title': title,
            'company_name': '示例公司',
            'source': 'test',
            'source_job_id': source_job_id,
            'source_url': f'https://example.com/jobs/{source_job_id}',
            'description': description,
        })
        self.job_repository.upsert(job)
        return job

    def stored_run(self) -> tuple:
        with closing(sqlite3.connect(self.discovery_repository.path)) as connection:
            return connection.execute(
                'SELECT run_id,status,record_keys_json,stats_json,message FROM discovery_runs ORDER BY rowid DESC LIMIT 1'
            ).fetchone()

    def test_scan_freezes_all_jobs_and_saves_complete_result(self):
        python_job = self.add_job('python', 'Python 工程师')
        review_job = self.add_job('ai', 'AI 应用工程师')
        other_job = self.add_job('other', '产品经理')

        run = self.service.scan_all()

        self.assertEqual(run.status, 'completed')
        self.assertEqual(run.record_keys, tuple(sorted((
            python_job.record_key, review_job.record_key,
            other_job.record_key,
        ))))
        self.assertEqual(run.stats.scanned_jobs, 3)
        self.assertEqual(run.stats.matched_jobs, 1)
        self.assertEqual(run.stats.job_mentions, 1)
        self.assertEqual(len(self.discovery_repository.mentions(run.run_id)), 1)
        self.assertEqual(len(self.discovery_repository.issues(run.run_id)), 1)

    def test_empty_job_database_produces_valid_zero_snapshot(self):
        run = self.service.scan_all()

        self.assertEqual(run.status, 'completed')
        self.assertEqual(run.record_keys, ())
        self.assertEqual(run.stats.scanned_jobs, 0)
        self.assertEqual(self.discovery_repository.mentions(run.run_id), [])

    def test_cancelled_scan_records_terminal_state_without_mentions(self):
        self.add_job('python', 'Python 工程师')
        control = TaskControl()
        control.cancel()

        with self.assertRaises(Cancelled):
            self.service.scan_all(control)

        run_id, status, _, _, message = self.stored_run()
        self.assertEqual(status, 'cancelled')
        self.assertEqual(message, '用户取消')
        self.assertEqual(self.discovery_repository.mentions(run_id), [])
        self.assertIsNone(self.discovery_repository.latest_completed_run('job_tech'))

    def test_cancellation_is_checked_between_jobs_and_rolls_back_result(self):
        self.add_job('python-1', 'Python 工程师')
        self.add_job('python-2', 'Python 开发工程师')

        class CancelDuringExtraction:
            def __init__(self):
                self.checks = 0

            def check(self):
                self.checks += 1
                if self.checks == 3:
                    raise Cancelled()

        control = CancelDuringExtraction()
        with self.assertRaises(Cancelled):
            self.service.scan_all(control)

        run_id, status, _, _, _ = self.stored_run()
        self.assertGreaterEqual(control.checks, 3)
        self.assertEqual(status, 'cancelled')
        self.assertEqual(self.discovery_repository.mentions(run_id), [])

    def test_extraction_failure_is_recorded_with_redacted_message_and_reraised(self):
        self.add_job('python', 'Python 工程师')

        with patch(
            'app.discovery.service.extract_job_technologies',
            side_effect=ValueError('联系电话：13800138000 格式异常'),
        ):
            with self.assertRaises(ValueError):
                self.service.scan_all()

        run_id, status, _, _, message = self.stored_run()
        self.assertEqual(status, 'failed')
        self.assertIn('ValueError', message)
        self.assertNotIn('13800138000', message)
        self.assertEqual(self.discovery_repository.mentions(run_id), [])
        self.assertIsNone(self.discovery_repository.latest_completed_run('job_tech'))


if __name__ == '__main__':
    unittest.main()
