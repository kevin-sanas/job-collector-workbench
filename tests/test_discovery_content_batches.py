import hashlib
import json
import unittest
from dataclasses import replace

from jsonschema import Draft7Validator, FormatChecker
from pathlib import Path

from app.discovery.batches import build_content_batch, parse_content_batch, serialize_batch
from app.discovery.models import (
    ContentCandidate,
    ContentCriteria,
    DiscoveryRun,
    JobExtractionStats,
    RepositorySnapshot,
    ReadmeEvidence,
    SourceIssue,
    TechDictionary,
    TechTerm,
)


class DiscoveryContentBatchTests(unittest.TestCase):
    def setUp(self):
        self.dictionary = TechDictionary('test-dictionary.v1', (
            TechTerm('python', 'Python', ('Python',), 'language', True, 'token'),
        ))
        run = DiscoveryRun.start(
            'content', self.dictionary.dictionary_version, 'github-rest.v1', (),
            search_criteria=ContentCriteria('Python', 1, 'stars', '2026-01-01'),
        )
        self.run = replace(
            run, status='completed', completed_at='2026-10-26T12:00:00+08:00',
            stats=JobExtractionStats(content_candidates=1),
        )
        url = 'https://github.com/example/sample'
        self.candidate = ContentCandidate(
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
            extractor_version='github-rest.v1',
            platform='github',
            repository_url=url,
            owner='example',
            repository_name='sample',
        )
        self.snapshot = RepositorySnapshot(
            self.candidate.candidate_key, self.candidate.fetched_at, 10, 2, '', False,
        )

    def test_content_run_uses_same_envelope_and_stable_snapshot(self):
        issue = SourceIssue('github', 'missing_readme', '无 README', self.candidate.candidate_key)

        batch = build_content_batch(self.run, [self.candidate], [self.snapshot], [issue])
        payload = batch.to_dict()

        self.assertEqual(payload['schema_version'], 'discovery-batch.v0.1')
        self.assertEqual(payload['job_mentions'], [])
        self.assertEqual(payload['stats']['content_candidates'], 1)
        self.assertEqual(payload['content_candidates'][0]['source_url'], self.candidate.source_url)
        self.assertEqual(payload['content_candidates'][0]['repository_snapshot']['stars'], 10)
        self.assertEqual(payload['issues'][0]['issue_code'], 'missing_readme')
        self.assertEqual(payload['search_criteria'], {
            'query': 'Python', 'limit': 1, 'sort': 'stars', 'created_after': '2026-01-01',
        })
        self.assertEqual(
            serialize_batch(batch, self.dictionary),
            serialize_batch(batch, self.dictionary),
        )

    def test_rejects_failed_run_and_other_candidate_snapshot(self):
        failed = replace(self.run, status='failed')
        with self.assertRaises(ValueError):
            build_content_batch(failed, [self.candidate], [self.snapshot], [])
        wrong = replace(self.snapshot, candidate_key='a' * 64)
        with self.assertRaises(ValueError):
            build_content_batch(self.run, [self.candidate], [wrong], [])

    def test_import_new_readme_and_reexport_same_batch(self):
        run = replace(self.run, extractor_version='github-rest.v2')
        candidate = replace(
            self.candidate,
            extractor_version='github-rest.v2',
            readme=ReadmeEvidence(
                status='fetched',
                source_url='https://github.com/example/sample/blob/main/README.md',
                fetched_at='2026-10-26T12:00:00+08:00',
                sha='a' * 40,
                content='# Sample\n项目用途与快速开始',
                size_bytes=1024,
            ),
        )
        original = build_content_batch(run, [candidate], [self.snapshot], [])

        parsed_run, candidates, snapshots, issues, batch = parse_content_batch(
            serialize_batch(original, self.dictionary), self.dictionary,
        )

        self.assertEqual(parsed_run.run_id, run.run_id)
        self.assertEqual(candidates, [candidate])
        self.assertEqual(snapshots, [self.snapshot])
        self.assertEqual(issues, [])
        self.assertEqual(
            serialize_batch(batch, self.dictionary),
            serialize_batch(original, self.dictionary),
        )
        schema_path = Path(__file__).resolve().parents[1] / 'config' / 'discovery' / 'discovery-content.v0.1.schema.json'
        schema = json.loads(schema_path.read_text(encoding='utf-8'))
        Draft7Validator(schema, format_checker=FormatChecker()).validate(
            json.loads(serialize_batch(batch, self.dictionary))
        )

    def test_import_old_candidate_without_readme_or_criteria(self):
        document = build_content_batch(self.run, [self.candidate], [self.snapshot], []).to_dict()
        del document['search_criteria']
        del document['content_candidates'][0]['readme']

        run, candidates, snapshots, issues, _ = parse_content_batch(
            json.dumps(document).encode('utf-8'), self.dictionary,
        )

        self.assertIsNone(run.search_criteria)
        self.assertIsNone(candidates[0].readme)
        self.assertEqual(snapshots, [self.snapshot])
        self.assertEqual(issues, [])

    def test_import_rejects_bad_shape_and_oversized_file(self):
        valid = build_content_batch(self.run, [self.candidate], [self.snapshot], []).to_dict()
        altered = []
        for changes in (
            {'schema_version': 'wrong'},
            {'job_mentions': [{}]},
            {'content_candidates': 'wrong'},
            {'run_id': None},
            {'run_id': 42},
        ):
            altered.append(valid | changes)
        wrong_readme = json.loads(json.dumps(valid))
        wrong_readme['content_candidates'][0]['readme'] = {'status': 'fetched'}
        altered.append(wrong_readme)
        for item in altered:
            with self.subTest(item=item), self.assertRaises(ValueError):
                parse_content_batch(json.dumps(item).encode('utf-8'), self.dictionary)
        for payload in (b'not json', b'x' * (10 * 1024 * 1024 + 1)):
            with self.assertRaises(ValueError):
                parse_content_batch(payload)

    def test_legacy_empty_dictionary_is_preview_only(self):
        document = build_content_batch(self.run, [self.candidate], [self.snapshot], []).to_dict()
        document['dictionary_version'] = ''
        run, candidates, snapshots, issues, batch = parse_content_batch(
            json.dumps(document).encode(), self.dictionary,
        )
        self.assertEqual(run.dictionary_version, '')
        self.assertEqual(len(candidates), 1)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(issues, [])
        with self.assertRaisesRegex(ValueError, '词典版本'):
            serialize_batch(batch, self.dictionary)

    def test_dictionary_membership_and_historical_version_preview(self):
        document = build_content_batch(self.run, [self.candidate], [self.snapshot], []).to_dict()
        document['content_candidates'][0]['tech_keys'] = ['unlisted-technology']
        with self.assertRaisesRegex(ValueError, '技术键不在声明词典'):
            parse_content_batch(json.dumps(document).encode(), self.dictionary)

        document['content_candidates'][0]['tech_keys'] = ['python']
        other = TechDictionary('another-version', self.dictionary.terms)
        _run, _candidates, _snapshots, _issues, imported = parse_content_batch(
            json.dumps(document).encode(), other,
        )
        self.assertTrue(imported.dictionary_unverified)
        with self.assertRaisesRegex(ValueError, '词典版本'):
            serialize_batch(imported, other)

    def test_strict_contract_rejects_extra_fields_and_bad_semantics(self):
        original = build_content_batch(self.run, [self.candidate], [self.snapshot], []).to_dict()
        malformed = []
        changed = json.loads(json.dumps(original))
        changed['published'] = True
        malformed.append(changed)
        changed = json.loads(json.dumps(original))
        changed['content_candidates'][0]['review_state'] = 'approved'
        malformed.append(changed)
        changed = json.loads(json.dumps(original))
        changed['stats']['content_candidates'] = 2
        malformed.append(changed)
        changed = json.loads(json.dumps(original))
        changed['content_candidates'][0]['repository_snapshot'] = None
        malformed.append(changed)
        changed = json.loads(json.dumps(original))
        changed['content_candidates'][0]['source_url'] += '#readme'
        malformed.append(changed)
        changed = json.loads(json.dumps(original))
        changed['content_candidates'][0]['repository_snapshot']['candidate_key'] = 'a' * 64
        malformed.append(changed)
        for document in malformed:
            with self.subTest(document=document), self.assertRaises(ValueError):
                parse_content_batch(json.dumps(document).encode(), self.dictionary)

    def test_export_rejects_bad_stats_and_missing_snapshot(self):
        with self.assertRaises(ValueError):
            build_content_batch(replace(self.run, stats=JobExtractionStats(
                scanned_jobs=1, content_candidates=1,
            )), [self.candidate], [self.snapshot], [])
        with self.assertRaises(ValueError):
            build_content_batch(self.run, [self.candidate], [], [])

    def test_market_case_uses_same_content_branch_without_repository_snapshot(self):
        url = 'https://example.com/ai/case-study'
        run = replace(self.run, extractor_version='market-curation.v1', search_criteria=None)
        candidate = ContentCandidate(
            candidate_key=hashlib.sha256(('market_case\n' + url).encode()).hexdigest(),
            content_type='market_case',
            source_name='示例企业技术博客',
            source_url=url,
            title='服务支持中的 AI 应用',
            source_published_at='2026-10-20T09:00:00+08:00',
            fetched_at='2026-10-26T12:00:00+08:00',
            source_evidence_excerpt='原文明确描述业务问题与 AI 使用方式。',
            proposed_summary='来源描述了服务支持中的 AI 应用，结果需人工核对。',
            proposed_learning_action='',
            tech_keys=('python',),
            extractor_version='market-curation.v1',
        )
        batch = build_content_batch(run, [candidate], [], [])
        imported = parse_content_batch(
            serialize_batch(batch, self.dictionary), self.dictionary,
        )
        self.assertEqual(imported[1], [candidate])
        self.assertEqual(imported[2], [])
        self.assertIsNone(batch.to_dict()['content_candidates'][0]['repository_snapshot'])


if __name__ == '__main__':
    unittest.main()
