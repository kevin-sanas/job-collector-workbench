import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from app.collector import Cancelled, TaskControl
from app.discovery.content_service import ContentDiscoveryService
from app.discovery.dictionary import load_dictionary
from app.discovery.models import (
    ContentCandidate,
    ContentCollectionResult,
    ContentCriteria,
    JobExtractionStats,
    RepositorySnapshot,
    SourceIssue,
)
from app.discovery.storage import DiscoveryRepository


class FakeSource:
    extractor_version = 'github-rest.v2'

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []
        self.dictionary = load_dictionary(Path('config/discovery/tech_dictionary.json'))

    def collect(self, criteria, control=None, notify=None):
        self.calls.append(criteria)
        if self.error:
            raise self.error
        return self.result


class DiscoveryContentServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp_dir.cleanup)
        self.repo = DiscoveryRepository(Path(self.temp_dir.name) / 'discovery.sqlite3')
        self.criteria = ContentCriteria('Python', 3, 'stars')

    def result(self, status='completed'):
        url = 'https://github.com/example/sample'
        candidate = ContentCandidate(
            candidate_key=hashlib.sha256(('open_source_project\n' + url).encode()).hexdigest(),
            content_type='open_source_project',
            source_name='GitHub',
            source_url=url,
            title='sample',
            source_published_at='',
            fetched_at='2026-10-26T12:00:00+08:00',
            source_evidence_excerpt='Python 示例',
            proposed_summary='',
            proposed_learning_action='',
            tech_keys=('python',),
            extractor_version='github-rest.v2',
            platform='github',
            repository_url=url,
            owner='example',
            repository_name='sample',
        )
        return ContentCollectionResult(
            candidates=(candidate,),
            snapshots=(RepositorySnapshot(
                candidate.candidate_key, candidate.fetched_at, 10, 2, '', False,
            ),),
            issues=(SourceIssue('github', 'test_issue', '测试问题'),),
            stats=JobExtractionStats(content_candidates=1),
            status=status,
            message='来源访问受限' if status == 'failed' else '',
        )

    def test_completed_run_is_stored_for_export(self):
        source = FakeSource(self.result())
        service = ContentDiscoveryService(self.repo, source)

        run = service.collect(self.criteria)

        self.assertEqual(run.status, 'completed')
        self.assertEqual(run.extractor_version, 'github-rest.v2')
        self.assertEqual(run.dictionary_version, source.dictionary.dictionary_version)
        self.assertEqual(
            self.repo.dictionary_snapshot_for_sync(run.dictionary_version),
            source.dictionary.to_dict(),
        )
        self.assertEqual(run.stats.content_candidates, 1)
        self.assertEqual(run.search_criteria, self.criteria)
        self.assertEqual(self.repo.content_candidates(run.run_id)[0].title, 'sample')
        self.assertEqual(source.calls, [self.criteria])

    def test_rate_limited_partial_run_is_not_promoted(self):
        source = FakeSource(self.result('failed'))

        run = ContentDiscoveryService(self.repo, source).collect(self.criteria)

        self.assertEqual(run.status, 'failed')
        self.assertEqual(len(self.repo.content_candidates(run.run_id)), 1)
        self.assertEqual(self.repo.content_candidates(), [])
        self.assertIsNone(self.repo.latest_completed_run('content'))

    def test_cancelled_before_source_result_is_recorded(self):
        source = FakeSource(error=Cancelled())
        control = TaskControl()
        run = ContentDiscoveryService(self.repo, source).collect(self.criteria, control)

        self.assertEqual(run.status, 'cancelled')
        self.assertEqual(run.stats.content_candidates, 0)
        self.assertIsNone(self.repo.latest_completed_run('content'))

    def test_invalid_source_result_marks_run_failed(self):
        result = self.result()
        bad = replace(result.snapshots[0], candidate_key='a' * 64)
        source = FakeSource(replace(result, snapshots=(bad,)))

        with self.assertRaises(ValueError):
            ContentDiscoveryService(self.repo, source).collect(self.criteria)

        with self.repo.connect() as connection:
            status = connection.execute(
                'SELECT status FROM discovery_runs ORDER BY rowid DESC LIMIT 1'
            ).fetchone()[0]
        self.assertEqual(status, 'failed')


if __name__ == '__main__':
    unittest.main()
