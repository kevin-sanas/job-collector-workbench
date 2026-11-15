import tempfile
import unittest
from pathlib import Path

from app.collector import Cancelled, TaskControl
from app.discovery.content_service import ContentDiscoveryService
from app.discovery.dictionary import load_dictionary
from app.discovery.sources.market import ManualMarketSource
from app.discovery.storage import DiscoveryRepository


class ManualMarketSourceTests(unittest.TestCase):
    def setUp(self):
        dictionary = load_dictionary(Path('config/discovery/tech_dictionary.json'))
        self.source = ManualMarketSource(dictionary)
        self.values = {
            'source_name': '企业技术博客',
            'source_url': 'https://example.com/ai/case?utm_source=test',
            'title': '客服场景中的 AI 试点',
            'source_published_at': '2026-10-20T09:00:00+08:00',
            'source_evidence_excerpt': '原文称试点用于客服问题分类。',
            'proposed_summary': '原文描述客服问题分类场景，提到试点采用 AI；未披露效果数据。',
            'proposed_learning_action': '根据公开问题分类示例建立本地样例，比较人工与模型标注差异。',
            'tech_keys': ['python'],
            'description': '人工整理的来源描述，待核对。',
        }

    def test_manual_candidate_keeps_evidence_pending_without_claiming_verification(self):
        result = self.source.collect(self.values)
        candidate = result.candidates[0]
        self.assertEqual(candidate.content_type, 'market_case')
        self.assertEqual(candidate.source_url, 'https://example.com/ai/case')
        self.assertEqual(candidate.review_state, 'pending')
        self.assertEqual(result.snapshots, ())
        self.assertEqual(result.stats.content_candidates, 1)
        self.assertEqual(result.issues[0].issue_code, 'manual_source_unverified')

    def test_service_saves_manual_case_and_actual_dictionary_snapshot(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            repo = DiscoveryRepository(Path(folder) / 'discovery.sqlite3')
            run = ContentDiscoveryService(repo, self.source).collect(self.values)
            self.assertEqual(run.status, 'completed')
            self.assertEqual(run.extractor_version, 'market-curation.v1')
            self.assertEqual(run.dictionary_version, self.source.dictionary.dictionary_version)
            self.assertEqual(len(repo.content_candidates(run.run_id)), 1)
            self.assertEqual(repo.content_snapshots(run.run_id), [])
            self.assertEqual(
                repo.dictionary_snapshot_for_sync(run.dictionary_version),
                self.source.dictionary.to_dict(),
            )

    def test_rejects_unknown_technology_and_incomplete_evidence(self):
        for change in (
            {'tech_keys': ['made-up-tech']},
            {'source_evidence_excerpt': ''},
            {'source_published_at': ''},
            {'proposed_summary': ''},
            {'source_url': 'http://example.com/case'},
            {'proposed_summary': 'An AI marketing case study'},
            {'proposed_learning_action': 'Build a campaign prototype'},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.source.collect(self.values | change)

    def test_cancel_stops_before_candidate_creation(self):
        control = TaskControl()
        control.cancel()
        with self.assertRaises(Cancelled):
            self.source.collect(self.values, control)


if __name__ == '__main__':
    unittest.main()
