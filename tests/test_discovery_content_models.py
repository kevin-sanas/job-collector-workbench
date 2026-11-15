import hashlib
import unittest
from dataclasses import asdict, replace

from app.discovery.models import (
    ContentCandidate,
    ContentCollectionResult,
    ContentCriteria,
    JobExtractionStats,
    RepositorySnapshot,
    ReadmeEvidence,
    SourceIssue,
)


class DiscoveryContentModelTests(unittest.TestCase):
    def candidate(self, **changes) -> ContentCandidate:
        values = {
            'content_type': 'open_source_project',
            'source_name': 'GitHub',
            'source_url': 'https://github.com/example/sample',
            'title': 'sample',
            'source_published_at': '2026-10-01T12:00:00+08:00',
            'fetched_at': '2026-10-26T12:00:00+08:00',
            'source_evidence_excerpt': 'Python example repository',
            'proposed_summary': '',
            'proposed_learning_action': '',
            'tech_keys': ('python',),
            'extractor_version': 'github-rest.v1',
            'review_state': 'pending',
            'platform': 'github',
            'repository_url': 'https://github.com/example/sample',
            'owner': 'example',
            'repository_name': 'sample',
            'description': 'Python example repository',
            'primary_language': 'Python',
            'topics': ('python', 'example'),
            'license_spdx': 'MIT',
        }
        values.update(changes)
        values['candidate_key'] = hashlib.sha256(
            (values['content_type'] + '\n' + values['source_url']).encode('utf-8')
        ).hexdigest()
        return ContentCandidate(**values)

    def test_valid_candidate_and_snapshot_keep_typed_source_data(self):
        candidate = self.candidate()
        snapshot = RepositorySnapshot(
            candidate_key=candidate.candidate_key,
            fetched_at=candidate.fetched_at,
            stars=10,
            forks=2,
            pushed_at='2026-10-25T12:00:00+08:00',
            archived=False,
        )
        result = ContentCollectionResult(
            candidates=(candidate,),
            snapshots=(snapshot,),
            issues=(SourceIssue('github', 'missing_readme', 'README 不存在', candidate.candidate_key),),
            stats=JobExtractionStats(content_candidates=1),
        )

        self.assertEqual(candidate.tech_keys, ('python',))
        self.assertEqual(snapshot.stars, 10)
        self.assertEqual(result.candidates, (candidate,))

    def test_rejects_non_https_and_wrong_identity(self):
        with self.assertRaises(ValueError):
            self.candidate(source_url='http://github.com/example/sample')
        with self.assertRaises(ValueError):
            replace(self.candidate(), candidate_key='0' * 64)
        with self.assertRaises(ValueError):
            self.candidate(source_url='https://user:pass@github.com/example/sample')

    def test_market_case_requires_summary_tech_and_source_date(self):
        values = {
            'content_type': 'market_case',
            'source_name': '官方案例',
            'source_url': 'https://example.com/case',
            'title': 'AI 客服案例',
            'platform': '',
            'repository_url': '',
            'owner': '',
            'repository_name': '',
        }
        with self.assertRaises(ValueError):
            self.candidate(**values, proposed_summary='', tech_keys=('python',))
        with self.assertRaises(ValueError):
            self.candidate(**values, proposed_summary='使用 AI 辅助客服', tech_keys=())
        with self.assertRaises(ValueError):
            self.candidate(**values, proposed_summary='使用 AI 辅助客服', tech_keys=('python',), source_published_at='')
        candidate = self.candidate(
            **values, proposed_summary='使用 AI 辅助客服',
            tech_keys=('python',),
        )
        self.assertEqual(candidate.content_type, 'market_case')

    def test_candidate_length_and_datetime_limits(self):
        with self.assertRaises(ValueError):
            self.candidate(proposed_summary='x' * 601)
        with self.assertRaises(ValueError):
            self.candidate(proposed_learning_action='x' * 201)
        with self.assertRaises(ValueError):
            self.candidate(fetched_at='2026-10-26T12:00:00')
        with self.assertRaises(ValueError):
            self.candidate(tech_keys=('INVALID KEY',))
        with self.assertRaises(ValueError):
            RepositorySnapshot('a' * 64, '2026-10-26T12:00:00+08:00', -1, 0, '', False)

    def test_criteria_enforces_small_manual_search(self):
        self.assertEqual(ContentCriteria('Python', 3, 'stars').limit, 3)
        for criteria in (
            ('', 3, 'stars'),
            ('Python', 0, 'stars'),
            ('Python', 21, 'stars'),
            ('Python', 3, 'created'),
        ):
            with self.assertRaises(ValueError):
                ContentCriteria(*criteria)
        for created_after in ('20261026', '2026-W43-1', '2026-02-30'):
            with self.subTest(created_after=created_after), self.assertRaises(ValueError):
                ContentCriteria('Python', 3, 'stars', created_after)

    def test_readme_round_trip_and_legacy_candidate(self):
        readme = ReadmeEvidence(
            status='fetched',
            source_url='https://github.com/example/sample/blob/main/README.md',
            fetched_at='2026-10-26T12:00:00+08:00',
            sha='a' * 40,
            content='# Sample\n项目用途与快速开始',
            size_bytes=2048,
        )
        candidate = self.candidate(readme=readme)
        self.assertEqual(ContentCandidate.from_mapping(asdict(candidate)).readme, readme)
        old_payload = asdict(candidate)
        del old_payload['readme']
        self.assertIsNone(ContentCandidate.from_mapping(old_payload).readme)
        unrelated = replace(
            readme, source_url='https://github.com/other/repository/blob/main/README.md',
        )
        with self.assertRaises(ValueError):
            self.candidate(readme=unrelated)

    def test_readme_rejects_invalid_source_sha_and_oversized_text(self):
        values = {
            'status': 'fetched',
            'source_url': 'https://github.com/example/sample/blob/main/README.md',
            'fetched_at': '2026-10-26T12:00:00+08:00',
            'sha': 'a' * 40,
        }
        for change in (
            {'source_url': 'https://elsewhere.example/README.md'},
            {'sha': ''},
            {'content': 'x' * 12001},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                ReadmeEvidence(**(values | change))
        redacted = ReadmeEvidence(**(values | {'content': 'a@b.co ' * 1700}))
        self.assertTrue(redacted.truncated)
        self.assertLessEqual(len(redacted.content), 12000)
        self.assertTrue(redacted.content.endswith('[内容已截断]'))

    def test_gitee_readme_must_belong_to_same_gitee_repository(self):
        readme = ReadmeEvidence(
            status='fetched',
            source_url='https://gitee.com/example/sample/blob/master/README.md',
            fetched_at='2026-10-26T12:00:00+08:00',
            sha='a' * 40,
            content='项目用途和运行步骤',
        )
        values = dict(
            source_name='Gitee', source_url='https://gitee.com/example/sample',
            platform='gitee', repository_url='https://gitee.com/example/sample',
            readme=readme,
        )
        self.assertEqual(self.candidate(**values).readme, readme)
        for wrong_url in (
            'https://github.com/example/sample/blob/main/README.md',
            'https://gitee.com/other/sample/blob/master/README.md',
            'https://gitee.com/api/v5/repos/example/other/readme',
            'https://elsewhere.example/example/sample/README.md',
        ):
            with self.subTest(wrong_url=wrong_url), self.assertRaises(ValueError):
                self.candidate(**(values | {'readme': replace(readme, source_url=wrong_url)}))
        with self.assertRaises(ValueError):
            self.candidate(**(values | {'source_url': 'https://github.com/example/sample'}))

    def test_market_case_rejects_repo_fields_or_readme_and_missing_evidence(self):
        values = dict(
            content_type='market_case', source_name='企业技术博客',
            source_url='https://example.com/case', title='AI 客服案例',
            proposed_summary='企业在客服问题处理中采用 AI',
            tech_keys=('python',), platform='', repository_url='',
            owner='', repository_name='',
        )
        with self.assertRaises(ValueError):
            self.candidate(**(values | {'source_evidence_excerpt': ''}))
        with self.assertRaises(ValueError):
            self.candidate(**(values | {'platform': 'github'}))


if __name__ == '__main__':
    unittest.main()
