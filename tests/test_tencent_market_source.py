import hashlib
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from app.collector import TaskControl
from app.discovery.batches import build_content_batch, parse_content_batch, serialize_batch
from app.discovery.content_service import ContentDiscoveryService
from app.discovery.dictionary import load_dictionary
from app.discovery.http_client import HttpResult, HttpStatusError
from app.discovery.sources.market import TencentBrowserClient, TencentMarketSource
from app.discovery.storage import DiscoveryRepository


def page(fallback):
    payload = {'props': {'pageProps': {'fallback': fallback}}}
    return '<html><script id="__NEXT_DATA__" type="application/json">' + json.dumps(
        payload, ensure_ascii=False,
    ) + '</script></html>'


def result(markup, url='https://cloud.tencent.com/developer/column/109050'):
    return HttpResult(markup, {}, url, 200)


def listing():
    cases = [
        (101, '2026/10/10 10:00:00', 12, 'WorkBuddy场景案例｜食品营销'),
        (102, '2026/10/11 10:00:00', 3, 'WorkBuddy场景案例｜广告文案'),
        (103, '2026/10/12 10:00:00', 999, 'WorkBuddy 产品发布新闻'),
    ]
    entries = [
        {
            'articleId': article_id, 'createTime': created, 'readNum': views,
            'title': title, 'columnId': 109050,
            'author': {'uid': 1188435},
        }
        for article_id, created, views, title in cases
    ]
    return result(page({'$inf$#url:"/api/column/getArticlesByColumnId"': [{'list': entries, 'total': 3}]}))


def article(article_id, title, published_epoch):
    def paragraph(value):
        return {'type': 'paragraph', 'content': [{'type': 'text', 'text': value}]}

    def heading(value):
        return {'type': 'heading', 'content': [{'type': 'text', 'text': value}]}

    body = {
        'type': 'doc', 'content': [
            paragraph('真实客户案例｜某食品公司｜市场人员'),
            heading('业务背景'),
            paragraph('某食品公司需要处理多渠道的营销材料、销售订单分析与客户反馈，存在大量重复任务。'),
            heading('真实痛点'),
            paragraph('营销素材产出重复，历史品牌资料难以复用，人工整理耗时且容易遗漏客户要求。'),
            heading('WorkBuddy提效方案'),
            paragraph('针对多渠道营销内容和重复修改的问题，WorkBuddy 自动依据品牌知识库生成不同平台的文案草稿，再由员工按客户要求审核与修改。'),
        ],
    }
    data = {'#url:"/api/article/detail"': {
        'publishTime': published_epoch,
        'articleInfo': {
            'articleId': article_id, 'uid': 1188435, 'title': title,
            'content': json.dumps(body, ensure_ascii=False),
            'userSummary': '原文称借助知识库生成多渠道文案。',
        },
    }}
    return result(page(data), f'https://cloud.tencent.com/developer/article/{article_id}')


class TencentMarketSourceTests(unittest.TestCase):
    def setUp(self):
        self.dictionary = load_dictionary(Path('config/discovery/tech_dictionary.json'))
        self.client = Mock()
        self.source = TencentMarketSource(self.client, self.dictionary)

    def test_browser_client_uses_existing_direct_browser_page(self):
        browser = Mock()
        browser.page.url = 'https://cloud.tencent.com/developer/column/109050'
        browser.page.html = '<html>公开案例</html>'
        browser.status = 200
        client = TencentBrowserClient(browser)

        response = client.get_html('https://cloud.tencent.com/developer/article/101')

        browser.navigate.assert_called_once_with(
            'https://cloud.tencent.com/developer/article/101', client.source,
        )
        self.assertEqual(response.data, '<html>公开案例</html>')

    def test_latest_collects_original_case_with_epoch_time_and_chinese_evidence(self):
        epoch = 1791712800
        self.client.get_html.side_effect = [
            listing(), article(102, 'WorkBuddy场景案例｜广告文案', epoch),
        ]

        collected = self.source.collect({'mode': 'latest', 'limit': 1})

        self.assertEqual(collected.status, 'completed')
        self.assertEqual(len(collected.candidates), 1)
        case = collected.candidates[0]
        self.assertEqual(case.source_url, 'https://cloud.tencent.com/developer/article/102')
        self.assertEqual(case.candidate_key, hashlib.sha256(
            ('market_case\n' + case.source_url).encode('utf-8'),
        ).hexdigest())
        self.assertEqual(case.source_published_at, datetime.fromtimestamp(
            epoch, timezone.utc,
        ).isoformat(timespec='seconds'))
        self.assertIn('某食品公司', case.proposed_summary)
        self.assertIn('WorkBuddy', case.source_evidence_excerpt)
        self.assertEqual(case.proposed_learning_action, '')
        self.assertEqual(case.tech_keys, ('artificial-intelligence',))
        self.assertEqual(case.review_state, 'pending')
        self.assertIn('vendor_claim_unverified', {issue.issue_code for issue in collected.issues})
        self.assertIn('bounded_source_ranking', {issue.issue_code for issue in collected.issues})
        self.assertEqual(self.client.get_html.call_count, 2)

    def test_views_rank_only_case_titles_within_public_first_page(self):
        self.client.get_html.side_effect = [
            listing(), article(101, 'WorkBuddy场景案例｜食品营销', 1791626400),
        ]

        collected = self.source.collect({'mode': 'views', 'limit': 1})

        self.assertEqual(collected.candidates[0].title, 'WorkBuddy场景案例｜食品营销')
        self.assertEqual(self.client.get_html.call_args_list[1].args[0],
                         'https://cloud.tencent.com/developer/article/101')

    def test_rate_limit_and_cancel_stop_without_fabricating_candidate(self):
        self.client.get_html.side_effect = HttpStatusError(429, {})
        limited = self.source.collect({'mode': 'latest', 'limit': 1})
        self.assertEqual(limited.status, 'failed')
        self.assertEqual(limited.issues[0].issue_code, 'rate_limited')
        self.assertEqual(limited.candidates, ())

        self.client.reset_mock()
        control = TaskControl()
        control.cancel()
        cancelled = self.source.collect({'mode': 'latest', 'limit': 1}, control)
        self.assertEqual(cancelled.status, 'cancelled')
        self.client.get_html.assert_not_called()

    def test_saved_batch_roundtrips_under_target_schema(self):
        self.client.get_html.side_effect = [
            listing(), article(102, 'WorkBuddy场景案例｜广告文案', 1791712800),
        ]
        with TemporaryDirectory(dir=Path.cwd()) as folder:
            repository = DiscoveryRepository(Path(folder) / 'discovery.sqlite3')
            run = ContentDiscoveryService(repository, self.source).collect({'mode': 'latest', 'limit': 1})
            self.assertEqual(run.status, 'completed')
            batch = build_content_batch(
                run, repository.content_candidates(run.run_id),
                repository.content_snapshots(run.run_id), repository.content_issues(run.run_id),
            )
            encoded = serialize_batch(batch, dictionary=self.dictionary)
            parsed = parse_content_batch(encoded, dictionary=self.dictionary)
            self.assertEqual(len(parsed[1]), 1)
            self.assertEqual(parsed[1][0].candidate_key, repository.content_candidates(run.run_id)[0].candidate_key)


if __name__ == '__main__':
    unittest.main()
