import hashlib
import json
import re
from datetime import datetime, timezone

import requests
from lxml import html
from lxml.etree import ParserError

from app.browser import BrowserSession
from app.collector import Cancelled
from app.discovery.http_client import HttpResult, HttpStatusError
from app.discovery.models import (
    ContentCandidate,
    ContentCollectionResult,
    JobExtractionStats,
    SourceIssue,
    TechDictionary,
)
from app.models import canonical_url, now


class ManualMarketSource:
    extractor_version = 'market-curation.v1'
    FIELDS = {
        'source_name', 'source_url', 'title', 'source_published_at',
        'source_evidence_excerpt', 'proposed_summary',
        'proposed_learning_action', 'tech_keys', 'description',
    }

    def __init__(self, dictionary: TechDictionary):
        self.dictionary = dictionary

    def collect(self, values: dict, control=None, notify=None) -> ContentCollectionResult:
        if control:
            control.check()
        if not isinstance(values, dict) or set(values) != self.FIELDS:
            raise ValueError('市场案例表单字段不完整或含有未定义字段')
        if not isinstance(values['tech_keys'], (list, tuple)):
            raise ValueError('关联技术必须是词典中的技术键数组')
        known_keys = {term.tech_key for term in self.dictionary.terms}
        if any(not isinstance(key, str) or key not in known_keys for key in values['tech_keys']):
            raise ValueError('市场案例关联技术不在本次技术词典中')
        for name in ('proposed_summary', 'proposed_learning_action'):
            value = values[name]
            if not isinstance(value, str) or (value.strip() and not re.search(r'[\u3400-\u9fff]', value)):
                raise ValueError('案例拟审核摘要和实践方向须人工整理为中文')
        source_url = canonical_url(values['source_url']) if isinstance(values['source_url'], str) else ''
        if not source_url.startswith('https://'):
            raise ValueError('市场案例来源必须是 HTTPS 链接')
        candidate = ContentCandidate(
            candidate_key=hashlib.sha256(
                ('market_case\n' + source_url).encode('utf-8')
            ).hexdigest(),
            content_type='market_case',
            source_name=values['source_name'],
            source_url=source_url,
            title=values['title'],
            source_published_at=values['source_published_at'],
            fetched_at=now(),
            source_evidence_excerpt=values['source_evidence_excerpt'],
            proposed_summary=values['proposed_summary'],
            proposed_learning_action=values['proposed_learning_action'],
            tech_keys=tuple(values['tech_keys']),
            extractor_version=self.extractor_version,
            description=values['description'],
        )
        if control:
            control.check()
        return ContentCollectionResult(
            candidates=(candidate,),
            snapshots=(),
            issues=(SourceIssue(
                'market_case', 'manual_source_unverified',
                '案例由人工录入，采集器未在线核对来源原文；发布前须人工复核',
                candidate.candidate_key,
            ),),
            stats=JobExtractionStats(content_candidates=1),
        )


class TencentBrowserClient:
    def __init__(self, browser: BrowserSession):
        self.browser = browser
        self.source = {
            'id': 'tencent_market',
            'domains': ['cloud.tencent.com'],
            'home': 'https://cloud.tencent.com/developer/column/109050',
        }

    def get_html(self, url: str) -> HttpResult:
        if self.browser.page is None:
            self.browser.open(self.source)
        if self.browser.page.url != url:
            self.browser.navigate(url, self.source)
        if self.browser.status in {403, 429}:
            raise HttpStatusError(self.browser.status, {})
        return HttpResult(self.browser.page.html, {}, self.browser.page.url, self.browser.status or 200)


class TencentMarketSource:
    """Collect vendor-published case claims from one public Tencent Cloud column page."""

    extractor_version = 'market-tencent-column.v1'
    column_url = 'https://cloud.tencent.com/developer/column/109050'
    author_uid = 1188435

    def __init__(self, client: TencentBrowserClient, dictionary: TechDictionary):
        self.client = client
        self.dictionary = dictionary
        if 'artificial-intelligence' not in {term.tech_key for term in dictionary.terms}:
            raise ValueError('技术词典缺少人工智能技术键')

    @staticmethod
    def _page_data(markup: str) -> dict:
        document = html.fromstring(markup)
        nodes = document.xpath('//script[@id="__NEXT_DATA__"]/text()')
        if len(nodes) != 1 or len(nodes[0]) > 500_000:
            raise ValueError('腾讯云页面缺少有界的公开数据')
        payload = json.loads(nodes[0])
        props = payload.get('props') if isinstance(payload, dict) else None
        page_props = props.get('pageProps') if isinstance(props, dict) else None
        fallback = page_props.get('fallback') if isinstance(page_props, dict) else None
        if not isinstance(fallback, dict):
            raise ValueError('腾讯云页面数据结构无效')
        return fallback

    @staticmethod
    def _node_text(node: dict) -> str:
        pieces = []
        stack = [node]
        while stack:
            current = stack.pop()
            if not isinstance(current, dict):
                continue
            if current.get('type') == 'text' and isinstance(current.get('text'), str):
                pieces.append(current['text'])
            children = current.get('content')
            if isinstance(children, list):
                stack.extend(reversed(children))
        return re.sub(r'\s+', ' ', ''.join(pieces)).strip()

    def collect(self, values: dict, control=None, notify=None) -> ContentCollectionResult:
        if not isinstance(values, dict) or set(values) != {'mode', 'limit'}:
            raise ValueError('案例自动采集条件无效')
        mode, limit = values['mode'], values['limit']
        if mode not in {'latest', 'views'} or type(limit) is not int or not 1 <= limit <= 5:
            raise ValueError('案例排序或单次上限无效')
        candidates, issues = [], []
        skipped = failed = 0
        status = 'completed'
        message = (
            '腾讯云认证作者案例专栏首屏，按发布时间排序'
            if mode == 'latest' else '腾讯云认证作者案例专栏首屏，按站内阅读量排序'
        )
        try:
            if control:
                control.check()
            listing = self.client.get_html(self.column_url)
            listing_data = self._page_data(listing.data)
            lists = [
                value for key, value in listing_data.items()
                if 'getArticlesByColumnId' in key and isinstance(value, list)
            ]
            if len(lists) != 1 or len(lists[0]) != 1 or not isinstance(lists[0][0], dict):
                raise ValueError('腾讯云案例列表结构无效')
            entries = lists[0][0].get('list')
            if not isinstance(entries, list) or len(entries) > 100:
                raise ValueError('腾讯云案例列表无效或超过上限')
            pool = []
            for entry in entries:
                if not isinstance(entry, dict) or entry.get('columnId') != 109050:
                    continue
                title = entry.get('title')
                article_id = entry.get('articleId')
                published = entry.get('createTime')
                author = entry.get('author')
                if (
                    not isinstance(title, str) or not title.startswith('WorkBuddy场景案例')
                    or type(article_id) is not int or article_id < 1
                    or not isinstance(published, str)
                    or not re.fullmatch(r'\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}', published)
                    or not isinstance(author, dict) or author.get('uid') != self.author_uid
                ):
                    continue
                if mode == 'views' and (type(entry.get('readNum')) is not int or entry['readNum'] < 0):
                    continue
                pool.append(entry)
            pool.sort(
                key=(lambda item: item['createTime']) if mode == 'latest'
                else (lambda item: (item['readNum'], item['createTime'])),
                reverse=True,
            )
            issues.append(SourceIssue(
                'market_case', 'bounded_source_ranking',
                f'本次仅按腾讯云专栏首屏 {len(entries)} 篇中的合格案例排序；'
                + ('依据列表时间' if mode == 'latest' else '依据该站公开阅读量')
                + '，不代表全网榜单',
            ))
            for entry in pool:
                if len(candidates) >= limit:
                    break
                if control:
                    control.check()
                article_url = f"https://cloud.tencent.com/developer/article/{entry['articleId']}"
                detail = self.client.get_html(article_url)
                detail_data = self._page_data(detail.data)
                matches = [
                    value for key, value in detail_data.items()
                    if '/api/article/detail' in key and isinstance(value, dict)
                ]
                if len(matches) != 1:
                    skipped += 1
                    issues.append(SourceIssue('market_case', 'invalid_article', '案例详情数据结构无效'))
                    continue
                source = matches[0]
                article = source.get('articleInfo')
                published_epoch = source.get('publishTime')
                if (
                    not isinstance(article, dict)
                    or article.get('articleId') != entry['articleId']
                    or article.get('uid') != self.author_uid
                    or article.get('title') != entry['title']
                    or type(published_epoch) is not int or not 0 < published_epoch < 4_102_444_800
                    or not isinstance(article.get('content'), str)
                ):
                    skipped += 1
                    issues.append(SourceIssue('market_case', 'invalid_article', '案例详情身份或发布时间无法核对'))
                    continue
                body = json.loads(article['content'])
                nodes = body.get('content') if isinstance(body, dict) else None
                if not isinstance(nodes, list) or len(nodes) > 500:
                    skipped += 1
                    issues.append(SourceIssue('market_case', 'invalid_article', '案例正文结构无效'))
                    continue
                paragraphs = [self._node_text(node) for node in nodes]
                if not any('真实客户案例' in item for item in paragraphs):
                    skipped += 1
                    issues.append(SourceIssue('market_case', 'not_business_case', '原文未标为实际客户场景'))
                    continue
                headings = {
                    self._node_text(node): index for index, node in enumerate(nodes)
                    if isinstance(node, dict) and node.get('type') == 'heading'
                }
                background_at = next((i for name, i in headings.items() if '业务背景' in name), -1)
                pain_at = next((i for name, i in headings.items() if '真实痛点' in name), -1)
                solution_at = next((i for name, i in headings.items() if '提效方案' in name), -1)
                if not 0 <= background_at < pain_at < solution_at:
                    skipped += 1
                    issues.append(SourceIssue('market_case', 'not_business_case', '原文缺少业务背景、痛点或 AI 方案'))
                    continue
                client_line = next(item for item in paragraphs if '真实客户案例' in item)
                background = next((item for item in paragraphs[background_at + 1:pain_at] if len(item) >= 30), '')
                pain = next((item for item in paragraphs[pain_at + 1:solution_at] if len(item) >= 25), '')
                solution = next((item for item in paragraphs[solution_at + 1:] if len(item) >= 50 and ('WorkBuddy' in item or 'CodeBuddy' in item)), '')
                if not background or not pain or not solution:
                    skipped += 1
                    issues.append(SourceIssue('market_case', 'not_business_case', '原文场景、问题或 AI 采用方式不足'))
                    continue
                source_url = canonical_url(article_url)
                proposed_summary = (
                    f'原文案例：{client_line[:75]}；业务场景：{background[:130]}；'
                    f'问题：{pain[:115]}；AI 采用：{solution[:180]}'
                )[:600]
                evidence = f'{client_line[:55]}；{pain[:75]}；{solution[:95]}'[:240]
                try:
                    candidate = ContentCandidate(
                        candidate_key=hashlib.sha256(('market_case\n' + source_url).encode('utf-8')).hexdigest(),
                        content_type='market_case',
                        source_name='腾讯云开发者社区 · 鹅厂AI研习社',
                        source_url=source_url,
                        title=entry['title'],
                        source_published_at=datetime.fromtimestamp(published_epoch, timezone.utc).isoformat(timespec='seconds'),
                        fetched_at=now(),
                        source_evidence_excerpt=evidence,
                        proposed_summary=proposed_summary,
                        proposed_learning_action='',
                        tech_keys=('artificial-intelligence',),
                        extractor_version=self.extractor_version,
                        description=article.get('userSummary') if isinstance(article.get('userSummary'), str) else '',
                    )
                except ValueError:
                    skipped += 1
                    issues.append(SourceIssue('market_case', 'invalid_article', '案例详情无法通过候选协议校验'))
                    continue
                candidates.append(candidate)
                issues.append(SourceIssue(
                    'market_case', 'vendor_claim_unverified',
                    '案例内容与效果为来源方自述；学习建议和独立效果核验待人工整理', candidate.candidate_key,
                ))
                if notify:
                    notify({'source': '腾讯云案例专栏', 'processed': len(candidates), 'limit': limit})
        except Cancelled:
            status, message = 'cancelled', '用户取消'
        except HttpStatusError as exc:
            status = 'failed'
            code = 'rate_limited' if exc.status_code in {403, 429} else 'http_error'
            message = f'腾讯云公开案例页面返回 HTTP {exc.status_code}，本次采集已停止'
            issues.append(SourceIssue('market_case', code, message))
            failed += 1
        except (requests.RequestException, ValueError, ParserError):
            status, message = 'failed', '腾讯云案例页面请求或结构解析失败'
            issues.append(SourceIssue('market_case', 'request_or_parse_failed', message))
            failed += 1
        if status == 'completed' and not candidates:
            issues.append(SourceIssue('market_case', 'no_verified_cases', '本次专栏首屏没有可按协议保存的案例'))
        return ContentCollectionResult(
            candidates=tuple(candidates), snapshots=(), issues=tuple(issues),
            stats=JobExtractionStats(content_candidates=len(candidates), skipped=skipped, failed=failed),
            status=status, message=message,
        )
