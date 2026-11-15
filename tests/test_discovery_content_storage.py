import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from app.discovery.models import (
    ContentCandidate,
    ContentCollectionResult,
    DiscoveryRun,
    JobExtractionStats,
    RepositorySnapshot,
    SourceIssue,
)
from app.discovery.storage import DiscoveryRepository


class DiscoveryContentStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp_dir.cleanup)
        self.repo = DiscoveryRepository(Path(self.temp_dir.name) / 'discovery.sqlite3')

    def candidate(self, title='sample', fetched_at='2026-10-26T12:00:00+08:00'):
        url = 'https://github.com/example/sample'
        return ContentCandidate(
            candidate_key=hashlib.sha256(('open_source_project\n' + url).encode()).hexdigest(),
            content_type='open_source_project',
            source_name='GitHub',
            source_url=url,
            title=title,
            source_published_at='',
            fetched_at=fetched_at,
            source_evidence_excerpt='公开仓库描述',
            proposed_summary='',
            proposed_learning_action='',
            tech_keys=('python',),
            extractor_version='github-rest.v1',
            platform='github',
            repository_url=url,
            owner='example',
            repository_name='sample',
        )

    def result(self, candidate, stars=10):
        return ContentCollectionResult(
            candidates=(candidate,),
            snapshots=(RepositorySnapshot(
                candidate.candidate_key, candidate.fetched_at, stars, 2, '', False,
            ),),
            issues=(SourceIssue('github', 'missing_readme', '未提供 README', candidate.candidate_key),),
            stats=JobExtractionStats(content_candidates=1),
        )

    def make_run(self):
        return DiscoveryRun.start('content', '', 'github-rest.v1', ())

    def test_completed_runs_keep_immutable_candidate_and_metrics_snapshots(self):
        first = self.make_run()
        self.repo.start_run(first)
        initial = self.candidate()
        self.repo.finish_content_run(first, self.result(initial, stars=10))

        second = self.make_run()
        self.repo.start_run(second)
        changed = self.candidate('sample updated', '2026-10-26T13:00:00+08:00')
        self.repo.finish_content_run(second, self.result(changed, stars=12))

        self.assertEqual(self.repo.content_run(first.run_id).status, 'completed')
        self.assertEqual(self.repo.content_candidates(first.run_id), [initial])
        self.assertEqual(self.repo.content_candidates(second.run_id), [changed])
        self.assertEqual(self.repo.content_candidates(), [changed])
        self.assertEqual(self.repo.content_snapshots(first.run_id)[0].stars, 10)
        self.assertEqual(self.repo.content_snapshots(second.run_id)[0].stars, 12)
        self.assertEqual(
            [item.stars for item in self.repo.candidate_snapshots(initial.candidate_key)],
            [10, 12],
        )
        self.assertEqual(len(self.repo.content_issues(first.run_id)), 1)

    def test_cancelled_partial_run_does_not_promote_latest_result(self):
        completed = self.make_run()
        self.repo.start_run(completed)
        initial = self.candidate()
        self.repo.finish_content_run(completed, self.result(initial))

        partial = self.make_run()
        self.repo.start_run(partial)
        changed = self.candidate('partial', '2026-10-26T13:00:00+08:00')
        self.repo.finish_content_run(partial, self.result(changed, stars=20), status='cancelled')

        self.assertEqual(self.repo.content_run(partial.run_id).status, 'cancelled')
        self.assertEqual(self.repo.content_candidates(partial.run_id), [changed])
        self.assertEqual(self.repo.content_candidates(), [initial])
        self.assertEqual(len(self.repo.candidate_snapshots(initial.candidate_key)), 1)
        self.assertEqual(self.repo.latest_completed_run('content').run_id, completed.run_id)

    def test_rejects_mismatched_snapshot_and_double_completion(self):
        run = self.make_run()
        self.repo.start_run(run)
        candidate = self.candidate()
        wrong = RepositorySnapshot('a' * 64, candidate.fetched_at, 1, 1, '', False)
        result = replace(self.result(candidate), snapshots=(wrong,))
        with self.assertRaises(ValueError):
            self.repo.finish_content_run(run, result)
        self.assertEqual(self.repo.content_candidates(run.run_id), [])

        self.repo.finish_content_run(run, self.result(candidate))
        with self.assertRaises(ValueError):
            self.repo.finish_content_run(run, self.result(candidate))


if __name__ == '__main__':
    unittest.main()
