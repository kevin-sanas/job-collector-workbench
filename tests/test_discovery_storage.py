import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from app.discovery.models import (
    DiscoveryRun,
    ExtractionIssue,
    JobExtractionResult,
    JobExtractionStats,
    JobIdentity,
    JobTechMention,
    TechDictionary,
    TechTerm,
)
from app.discovery.storage import DiscoveryRepository


class DiscoveryStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / 'discovery.sqlite3'
        self.repository = DiscoveryRepository(self.path)
        self.dictionary = TechDictionary(
            'tech-dict.test.1',
            (TechTerm('python', 'Python', ('Python',), '语言', True, 'token'),),
        )

    def make_run(self) -> DiscoveryRun:
        return DiscoveryRun.start('job_tech', self.dictionary.dictionary_version, 'job-tech-rule.v1', ('job-b', 'job-a'))

    def make_result(self, run: DiscoveryRun, record_key: str = 'job-a') -> JobExtractionResult:
        identity = JobIdentity(
            record_key=record_key,
            source='test',
            source_job_id=record_key,
            source_url=f'https://example.com/{record_key}',
            title='Python 工程师',
            company_name='示例公司',
            city='北京',
        )
        mention = JobTechMention(
            run_id=run.run_id,
            record_key=record_key,
            tech_key='python',
            source_fields=('title',),
            evidence_excerpt='Python 工程师',
            job_content_hash='a' * 64,
            extractor_version=run.extractor_version,
            dictionary_version=run.dictionary_version,
            review_state='pending',
            job_identity=identity,
        )
        issue = ExtractionIssue(
            run_id=run.run_id,
            record_key='job-b',
            issue_code='review_required',
            matched_alias='AI',
            candidate_tech_keys=('artificial-intelligence',),
            evidence_excerpt='AI 应用',
        )
        return JobExtractionResult(
            mentions=(mention,),
            issues=(issue,),
            stats=JobExtractionStats(scanned_jobs=2, matched_jobs=1, job_mentions=1),
        )

    def test_creates_only_discovery_tables(self):
        with closing(sqlite3.connect(self.path)) as connection:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}

        self.assertTrue({
            'discovery_runs', 'dictionary_snapshots', 'job_tech_mentions',
            'extraction_issues', 'batch_exports',
        }.issubset(tables))
        self.assertNotIn('jobs', tables)

    def test_existing_discovery_runs_gain_optional_search_criteria_column(self):
        legacy_path = Path(self.temp_dir.name) / 'legacy.sqlite3'
        with closing(sqlite3.connect(legacy_path)) as connection:
            connection.execute('''
                CREATE TABLE discovery_runs (
                    run_id TEXT PRIMARY KEY, run_type TEXT NOT NULL, status TEXT NOT NULL,
                    started_at TEXT NOT NULL, completed_at TEXT NOT NULL,
                    dictionary_version TEXT NOT NULL, extractor_version TEXT NOT NULL,
                    record_keys_json TEXT NOT NULL, stats_json TEXT NOT NULL,
                    message TEXT NOT NULL
                )
            ''')
            connection.execute(
                'INSERT INTO discovery_runs VALUES (?,?,?,?,?,?,?,?,?,?)',
                (
                    'legacy-run', 'job_tech', 'completed',
                    '2026-10-26T10:00:00+08:00', '2026-10-26T10:01:00+08:00',
                    'tech-dict.test.1', 'job-tech-rule.v1', '["job-a"]', '{}', '',
                ),
            )
            connection.commit()

        migrated = DiscoveryRepository(legacy_path)

        run = migrated.latest_completed_run('job_tech')
        self.assertEqual(run.run_id, 'legacy-run')
        self.assertIsNone(run.search_criteria)
        with migrated.connect() as connection:
            columns = {row[1] for row in connection.execute('PRAGMA table_info(discovery_runs)')}
        self.assertIn('search_criteria_json', columns)

    def test_finishes_one_immutable_transaction_and_reads_stable_results(self):
        run = self.make_run()
        result = self.make_result(run)
        self.repository.start_run(run)

        self.repository.finish_job_run(run, self.dictionary, result)

        latest = self.repository.latest_completed_run('job_tech')
        self.assertEqual(latest.run_id, run.run_id)
        self.assertEqual(latest.status, 'completed')
        self.assertTrue(latest.completed_at)
        self.assertEqual(self.repository.mentions(run.run_id), list(result.mentions))
        self.assertEqual(self.repository.issues(run.run_id), list(result.issues))
        with self.assertRaises(ValueError):
            self.repository.finish_job_run(run, self.dictionary, result)
        with self.assertRaises(ValueError):
            self.repository.start_run(run)

    def test_duplicate_result_rolls_back_without_partial_rows(self):
        run = self.make_run()
        result = self.make_result(run)
        duplicate_result = replace(result, mentions=(result.mentions[0], result.mentions[0]))
        self.repository.start_run(run)

        with self.assertRaises(sqlite3.IntegrityError):
            self.repository.finish_job_run(run, self.dictionary, duplicate_result)

        self.assertEqual(self.repository.mentions(run.run_id), [])
        self.assertIsNone(self.repository.latest_completed_run('job_tech'))

    def test_failed_and_cancelled_runs_do_not_replace_latest_completed(self):
        completed = self.make_run()
        self.repository.start_run(completed)
        self.repository.finish_job_run(completed, self.dictionary, self.make_result(completed))

        failed = self.make_run()
        self.repository.start_run(failed)
        message = "x'); DROP TABLE discovery_runs; --"
        self.repository.fail_run(failed.run_id, 'failed', message)
        cancelled = self.make_run()
        self.repository.start_run(cancelled)
        self.repository.fail_run(cancelled.run_id, 'cancelled', '用户取消')

        self.assertEqual(self.repository.latest_completed_run('job_tech').run_id, completed.run_id)
        with closing(sqlite3.connect(self.path)) as connection:
            stored = connection.execute(
                'SELECT message FROM discovery_runs WHERE run_id=?', (failed.run_id,)
            ).fetchone()[0]
            count = connection.execute('SELECT COUNT(*) FROM discovery_runs').fetchone()[0]
        self.assertEqual(stored, message)
        self.assertEqual(count, 3)

    def test_latest_completed_prefers_most_recent_completion_and_records_export(self):
        first = self.make_run()
        self.repository.start_run(first)
        self.repository.finish_job_run(first, self.dictionary, self.make_result(first))
        second = self.make_run()
        self.repository.start_run(second)
        self.repository.finish_job_run(second, self.dictionary, self.make_result(second))

        self.assertEqual(self.repository.latest_completed_run('job_tech').run_id, second.run_id)
        export_path = Path(self.temp_dir.name) / 'batch.json'
        self.repository.record_export(
            second.run_id, 'discovery-batch.v0.1', export_path, 'f' * 64,
            '2026-10-26T12:00:00+08:00',
        )
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute(
                'SELECT schema_version, path, sha256 FROM batch_exports WHERE run_id=?',
                (second.run_id,),
            ).fetchone()
        self.assertEqual(row, ('discovery-batch.v0.1', str(export_path), 'f' * 64))


if __name__ == '__main__':
    unittest.main()
