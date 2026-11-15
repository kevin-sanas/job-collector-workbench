"""Collect public Gitee repositories from keyword search or the recommendation page."""

import base64
import binascii
import hashlib
import re
from dataclasses import replace
from datetime import datetime
from urllib.parse import urljoin, urlsplit

import requests
from lxml import html
from lxml.etree import ParserError

from app.collector import Cancelled
from app.discovery.http_client import HttpClient, HttpStatusError
from app.discovery.models import (
    ContentCandidate,
    ContentCollectionResult,
    ContentCriteria,
    JobExtractionStats,
    ReadmeEvidence,
    RepositorySnapshot,
    SourceIssue,
    TechDictionary,
    normalize_alias,
)
from app.discovery.readme_text import clean_readme
from app.models import canonical_url, redact_text


MAX_README_DECODED_BYTES = 128 * 1024
README_TRUNCATION_MARKER = '[内容已截断]'
EXTRACTOR_VERSION = 'gitee-rest.v1'
_REPOSITORY_PART = re.compile(r'[A-Za-z0-9_.-]{1,100}')


class GiteeSource:
    extractor_version = EXTRACTOR_VERSION

    def __init__(self, client: HttpClient, dictionary: TechDictionary, *, recommendation_browser=None, verification_timeout=1200):
        self.client = client
        self.dictionary = dictionary
        self.recommendation_browser = recommendation_browser
        self.verification_timeout = verification_timeout
        self.dictionary_version = dictionary.dictionary_version
        self.aliases = {
            normalize_alias(alias): term.tech_key
            for term in dictionary.terms
            if term.enabled and term.match_mode != 'review'
            for alias in term.aliases
        }

    def collect(self, repository_url: str | ContentCriteria | dict, control=None, notify=None) -> ContentCollectionResult:
        if isinstance(repository_url, ContentCriteria):
            return self.collect_search(repository_url, control, notify)
        if isinstance(repository_url, dict):
            return self.collect_recommended(repository_url, control, notify)
        # Do not send arbitrary paths, credentials, or query strings to the API.
        if not isinstance(repository_url, str):
            raise ValueError('Gitee 仓库主页链接无效')
        supplied = repository_url.strip()
        parsed = urlsplit(supplied)
        parts = parsed.path.strip('/').split('/')
        if (
            parsed.scheme != 'https'
            or parsed.netloc != 'gitee.com'
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or len(parts) != 2
        ):
            raise ValueError('请输入 https://gitee.com/拥有者/仓库名 格式的仓库主页')
        owner, repository_name = parts
        if repository_name.endswith('.git'):
            repository_name = repository_name[:-4]
        if (
            not _REPOSITORY_PART.fullmatch(owner)
            or not _REPOSITORY_PART.fullmatch(repository_name)
            or owner in {'.', '..'}
            or repository_name in {'.', '..'}
        ):
            raise ValueError('Gitee 仓库主页路径无效')
        repository_path = f'/api/v5/repos/{owner}/{repository_name}'
        readme_path = repository_path + '/readme'

        try:
            if control:
                control.check()
        except Cancelled:
            return ContentCollectionResult((), (), (), JobExtractionStats(), 'cancelled', '用户取消')

        try:
            response = self.client.get_json(repository_path)
        except HttpStatusError as exc:
            rate_limited = exc.status_code in {403, 429}
            code = 'rate_limited' if rate_limited else (
                'repository_not_found' if exc.status_code == 404 else 'repository_http_error'
            )
            reason = f'Gitee 仓库接口返回 HTTP {exc.status_code}，本次采集已停止'
            return ContentCollectionResult(
                (), (), (SourceIssue('gitee', code, reason),),
                JobExtractionStats(failed=1), 'failed', reason,
            )
        except (requests.RequestException, ValueError) as exc:
            reason = f'Gitee 仓库接口请求或响应失败（{type(exc).__name__}）'
            return ContentCollectionResult(
                (), (), (SourceIssue('gitee', 'request_failed', reason),),
                JobExtractionStats(failed=1), 'failed', reason,
            )

        item = response.data
        if not isinstance(item, dict):
            return ContentCollectionResult(
                (), (), (SourceIssue('gitee', 'invalid_response', 'Gitee 仓库详情结构无效'),),
                JobExtractionStats(failed=1), 'failed', 'Gitee 仓库详情结构无效',
            )
        full_name = item.get('full_name')
        if (
            not isinstance(full_name, str)
            or full_name.casefold() != f'{owner}/{repository_name}'.casefold()
            or item.get('private') is True
            or item.get('public') is False
        ):
            return ContentCollectionResult(
                (), (), (SourceIssue('gitee', 'repository_unavailable', 'Gitee 仓库身份不符或不是公开仓库'),),
                JobExtractionStats(failed=1), 'failed', 'Gitee 仓库不可采集',
            )
        # The API has returned html_url values ending in .git. The stable source
        # identity is the public homepage without that suffix.
        api_parts = full_name.split('/')
        if (
            len(api_parts) != 2
            or not all(_REPOSITORY_PART.fullmatch(part) and part not in {'.', '..'} for part in api_parts)
        ):
            return ContentCollectionResult(
                (), (), (SourceIssue('gitee', 'invalid_response', 'Gitee 仓库名称格式无效'),),
                JobExtractionStats(failed=1), 'failed', 'Gitee 仓库详情结构无效',
            )
        owner, repository_name = api_parts
        source_url = canonical_url(f'https://gitee.com/{owner}/{repository_name}')
        repository_path = f'/api/v5/repos/{owner}/{repository_name}'
        readme_path = repository_path + '/readme'
        readme_api_url = f'https://gitee.com{readme_path}'
        fetched_at = datetime.now().astimezone().isoformat(timespec='microseconds')
        description = redact_text(item.get('description'))[:500]
        primary_language = item.get('language') if isinstance(item.get('language'), str) else ''
        primary_language = primary_language[:100]
        raw_topics = item.get('topics')
        topics = tuple(dict.fromkeys(
            topic.strip()[:100] for topic in raw_topics
            if isinstance(topic, str) and topic.strip()
        ))[:100] if isinstance(raw_topics, list) else ()
        tech_keys = tuple(sorted({
            self.aliases[normalized]
            for name in (primary_language, *topics)
            for variant in (name, name.replace('-', ' '))
            if (normalized := normalize_alias(variant)) in self.aliases
        }))[:100]
        license_value = item.get('license')
        license_spdx = (
            license_value.get('spdx_id', '')
            if isinstance(license_value, dict) and isinstance(license_value.get('spdx_id'), str)
            else ''
        )
        if license_spdx == 'NOASSERTION':
            license_spdx = ''
        candidate_key = hashlib.sha256(
            ('open_source_project\n' + source_url).encode('utf-8')
        ).hexdigest()
        stars, forks = item.get('stargazers_count'), item.get('forks_count')
        try:
            candidate = ContentCandidate(
                candidate_key=candidate_key,
                content_type='open_source_project',
                source_name='Gitee',
                source_url=source_url,
                title=f'{owner}/{repository_name}',
                source_published_at=item.get('created_at') if isinstance(item.get('created_at'), str) else '',
                fetched_at=fetched_at,
                source_evidence_excerpt=description[:240],
                proposed_summary='',
                proposed_learning_action='',
                tech_keys=tech_keys,
                extractor_version=EXTRACTOR_VERSION,
                review_state='pending',
                platform='gitee',
                repository_url=source_url,
                owner=owner,
                repository_name=repository_name,
                description=description,
                primary_language=primary_language,
                topics=topics,
                license_spdx=license_spdx[:100],
            )
            snapshot = RepositorySnapshot(
                candidate_key=candidate_key,
                fetched_at=fetched_at,
                stars=stars if type(stars) is int and stars >= 0 else None,
                forks=forks if type(forks) is int and forks >= 0 else None,
                pushed_at=item.get('pushed_at') if isinstance(item.get('pushed_at'), str) else '',
                archived=item.get('archived') if type(item.get('archived')) is bool else None,
            )
        except ValueError:
            return ContentCollectionResult(
                (), (), (SourceIssue('gitee', 'invalid_response', 'Gitee 仓库字段无法通过候选校验'),),
                JobExtractionStats(failed=1), 'failed', 'Gitee 仓库详情结构无效',
            )

        if notify:
            notify({'source': 'Gitee', 'stage': 'repository', 'processed': 1, 'limit': 1})
        try:
            if control:
                control.check()
        except Cancelled:
            return ContentCollectionResult(
                (candidate,), (snapshot,), (), JobExtractionStats(content_candidates=1),
                'cancelled', '用户取消',
            )

        remaining = next(
            (value for key, value in response.headers.items() if key.casefold() == 'x-ratelimit-remaining'),
            '未知',
        )
        if remaining == '0':
            attempted_at = datetime.now().astimezone().isoformat(timespec='microseconds')
            reason = 'Gitee 仓库请求后剩余额度为零，已停止 README 请求'
            readme = ReadmeEvidence('rate_limited', readme_api_url, attempted_at, reason=reason)
            candidate = replace(candidate, readme=readme)
            return ContentCollectionResult(
                (candidate,), (snapshot,), (SourceIssue('gitee', 'rate_limit_exhausted', reason, candidate_key),),
                JobExtractionStats(content_candidates=1, failed=1), 'failed', reason,
            )

        attempted_at = datetime.now().astimezone().isoformat(timespec='microseconds')
        detail_remaining = '未知'
        try:
            detail = self.client.get_json(readme_path)
        except HttpStatusError as exc:
            detail_remaining = next(
                (value for key, value in exc.headers.items() if key.casefold() == 'x-ratelimit-remaining'),
                '未知',
            )
            if exc.status_code in {403, 429}:
                readme_status, issue_code = 'rate_limited', 'readme_rate_limited'
            elif exc.status_code == 404:
                readme_status, issue_code = 'not_found', 'readme_not_found'
            else:
                readme_status, issue_code = 'failed', 'readme_http_error'
            reason = f'Gitee README 接口返回 HTTP {exc.status_code}'
            readme = ReadmeEvidence(readme_status, readme_api_url, attempted_at, reason=reason)
        except (requests.RequestException, ValueError) as exc:
            too_large = isinstance(exc, ValueError) and '超过大小限制' in str(exc)
            readme_status = 'too_large' if too_large else 'failed'
            issue_code = 'readme_too_large' if too_large else 'readme_request_failed'
            reason = 'Gitee README 接口响应超过 2 MB 上限' if too_large else 'Gitee README 接口请求或响应失败'
            readme = ReadmeEvidence(readme_status, readme_api_url, attempted_at, reason=reason)
        else:
            detail_remaining = next(
                (value for key, value in detail.headers.items() if key.casefold() == 'x-ratelimit-remaining'),
                '未知',
            )
            try:
                payload = detail.data
                if not isinstance(payload, dict) or payload.get('encoding') != 'base64':
                    raise ValueError('README 未提供 Base64 正文')
                encoded = payload.get('content')
                if not isinstance(encoded, str):
                    raise ValueError('README 正文格式无效')
                raw = base64.b64decode(''.join(encoded.split()), validate=True)
                reported_size = payload.get('size')
                size_bytes = reported_size if type(reported_size) is int and reported_size >= 0 else None
                if not raw and size_bytes:
                    readme = ReadmeEvidence(
                        'too_large', readme_api_url, attempted_at, size_bytes=size_bytes,
                        reason='README 未返回可保留的正文',
                    )
                    issue_code = 'readme_too_large'
                else:
                    byte_truncated = len(raw) > MAX_README_DECODED_BYTES
                    selected = raw[:MAX_README_DECODED_BYTES].decode('utf-8', errors='ignore')
                    cleaned, char_truncated = clean_readme(
                        selected,
                        max_chars=12000 - len(README_TRUNCATION_MARKER) - 1 if byte_truncated else 12000,
                    )
                    if byte_truncated and not char_truncated:
                        cleaned += '\n' + README_TRUNCATION_MARKER
                    readme = ReadmeEvidence(
                        status='fetched',
                        source_url=payload.get('html_url') or readme_api_url,
                        fetched_at=attempted_at,
                        sha=payload.get('sha', ''),
                        content=cleaned,
                        size_bytes=size_bytes,
                        truncated=byte_truncated or char_truncated,
                    )
                    # The candidate performs the platform/repository ownership check.
                    replace(candidate, readme=readme)
                    issue_code = ''
            except (ValueError, TypeError, binascii.Error):
                issue_code = 'readme_invalid_response'
                readme = ReadmeEvidence(
                    'failed', readme_api_url, attempted_at,
                    reason='Gitee README 响应字段无效或来源与仓库不一致',
                )

        candidate = replace(candidate, readme=readme)
        issues = (SourceIssue('gitee', issue_code, readme.reason, candidate_key),) if issue_code else ()
        status = 'failed' if readme.status == 'rate_limited' else 'completed'
        message = 'Gitee README 接口限流' if status == 'failed' else ''
        if status == 'completed' and detail_remaining == '0':
            issues += (SourceIssue(
                'gitee', 'rate_limit_exhausted',
                'Gitee README 请求后该接口剩余额度为零，停止后续仓库请求', candidate_key,
            ),)
            status, message = 'failed', 'Gitee 接口额度耗尽'
        if notify:
            notify({'source': 'Gitee', 'stage': 'readme', 'processed': 1, 'limit': 1, 'remaining': detail_remaining})
        try:
            if control:
                control.check()
        except Cancelled:
            status, message = 'cancelled', '用户取消'
        return ContentCollectionResult(
            (candidate,), (snapshot,), issues,
            JobExtractionStats(content_candidates=1, failed=1 if status == 'failed' else 0),
            status, message,
        )

    def collect_search(self, criteria: ContentCriteria, control=None, notify=None) -> ContentCollectionResult:
        """Sort only the first 100 official keyword hits; Gitee has no global sort API."""
        if criteria.created_after:
            raise ValueError('Gitee 关键词搜索不支持创建日期筛选')
        try:
            if control:
                control.check()
        except Cancelled:
            return ContentCollectionResult((), (), (), JobExtractionStats(), 'cancelled', '用户取消')

        try:
            response = self.client.get_json(
                '/api/v5/search/repos',
                params={'q': criteria.query, 'size': 100, 'from': 0},
            )
        except HttpStatusError as exc:
            code = 'rate_limited' if exc.status_code in {403, 429} else 'search_http_error'
            message = f'Gitee 搜索接口返回 HTTP {exc.status_code}，本次采集已停止'
            return ContentCollectionResult(
                (), (), (SourceIssue('gitee', code, message),),
                JobExtractionStats(failed=1), 'failed', message,
            )
        except (requests.RequestException, ValueError) as exc:
            message = f'Gitee 搜索接口请求或响应失败（{type(exc).__name__}）'
            return ContentCollectionResult(
                (), (), (SourceIssue('gitee', 'request_failed', message),),
                JobExtractionStats(failed=1), 'failed', message,
            )
        payload = response.data
        if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
            message = 'Gitee 关键词搜索结果结构无效'
            return ContentCollectionResult(
                (), (), (SourceIssue('gitee', 'invalid_response', message),),
                JobExtractionStats(failed=1), 'failed', message,
            )
        remaining = next(
            (value for key, value in response.headers.items() if key.casefold() == 'x-ratelimit-remaining'),
            '未知',
        )
        if remaining == '0':
            message = 'Gitee 搜索请求后剩余额度为零，已停止仓库详情请求'
            return ContentCollectionResult(
                (), (), (SourceIssue('gitee', 'rate_limit_exhausted', message),),
                JobExtractionStats(failed=1), 'failed', message,
            )

        items = payload['data']
        if not items:
            total = payload.get('total_count')
            if type(total) is int and total > 0:
                message = 'Gitee 关键词搜索总数与返回列表不一致'
                return ContentCollectionResult(
                    (), (), (SourceIssue('gitee', 'invalid_response', message),),
                    JobExtractionStats(failed=1), 'failed', message,
                )
            return ContentCollectionResult(
                (), (), (SourceIssue('gitee', 'no_results', 'Gitee 本次关键词搜索未返回仓库'),),
                JobExtractionStats(), 'completed', 'Gitee 本次关键词搜索未返回仓库',
            )
        total = payload.get('total_count')
        bounded = type(total) is not int or total > len(items)
        sort_label = 'Star' if criteria.sort == 'stars' else '仓库更新时间'
        pool_note = f'仅在 Gitee 本次返回的前 {len(items)} 条中按{sort_label}排序，非全站榜单'
        issues = [SourceIssue('gitee', 'bounded_search_pool', pool_note)] if bounded else []
        if criteria.sort == 'stars':
            ordered = sorted(
                items,
                key=lambda item: (
                    item.get('stars') if type(item.get('stars')) is int and item['stars'] >= 0 else -1
                ) if isinstance(item, dict) else -1,
                reverse=True,
            )
        else:
            ordered = sorted(
                items,
                key=lambda item: (
                    item.get('updated_at') if isinstance(item.get('updated_at'), str)
                    and re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', item['updated_at']) else ''
                ) if isinstance(item, dict) else '',
                reverse=True,
            )

        candidates = []
        snapshots = []
        seen_urls = set()
        skipped = failed = 0
        status, message = 'completed', pool_note
        for item in ordered[:criteria.limit]:
            try:
                if control:
                    control.check()
            except Cancelled:
                status, message = 'cancelled', '用户取消'
                break
            url = item.get('url') if isinstance(item, dict) else None
            if not isinstance(url, str) or url in seen_urls:
                skipped += 1
                issues.append(SourceIssue('gitee', 'invalid_search_item', '搜索结果缺少有效仓库链接或重复'))
                continue
            seen_urls.add(url)
            try:
                result = self.collect(url, control, notify)
            except ValueError:
                skipped += 1
                issues.append(SourceIssue('gitee', 'invalid_search_item', '搜索结果仓库链接格式无效'))
                continue
            candidates.extend(result.candidates)
            snapshots.extend(result.snapshots)
            issues.extend(result.issues)
            failed += result.stats.failed
            skipped += result.stats.skipped
            if result.status != 'completed':
                status, message = result.status, result.message
                break
        return ContentCollectionResult(
            tuple(candidates), tuple(snapshots), tuple(issues),
            JobExtractionStats(content_candidates=len(candidates), skipped=skipped, failed=failed),
            status, message,
        )

    def collect_recommended(self, criteria: dict, control=None, notify=None) -> ContentCollectionResult:
        if set(criteria) != {'mode', 'limit'} or criteria['mode'] != 'recommended':
            raise ValueError('Gitee 推荐项目采集条件无效')
        limit = criteria['limit']
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError('Gitee 推荐项目数量必须为 1–20')
        issues = []
        try:
            if control:
                control.check()
            if self.recommendation_browser is None:
                raise ValueError('未配置 Gitee 推荐页专用浏览器')
            source = {
                'id': 'gitee_recommended', 'domains': ['gitee.com'],
                'home': 'https://gitee.com/explore/all',
            }
            browser = self.recommendation_browser
            browser.open(source)
            if control:
                control.check()
            if browser.status in {403, 429}:
                raise HttpStatusError(browser.status, {})
            page = browser.ensure_page(source)
            markup = page.html
            verification = (
                'BIOC_CONFIG_SCRIPT' in markup or '安全验证码' in page.title
                or '安全验证' in page.title or '滑动验证' in page.title
            )
            if verification:
                issues.append(SourceIssue('gitee', 'manual_verification', '推荐页要求人工完成安全验证'))
                if not control or not notify:
                    message = 'Gitee 推荐页需要人工验证，已停止'
                    return ContentCollectionResult(
                        (), (), tuple(issues), JobExtractionStats(failed=1), 'failed', message,
                    )
                notify({'source': 'Gitee', 'stage': 'waiting_verification'})
                control.wait_for_user(timeout=self.verification_timeout)
                page = browser.ensure_page(source)
                if browser.status in {403, 429}:
                    raise HttpStatusError(browser.status, {})
                markup = page.html
                if (
                    'BIOC_CONFIG_SCRIPT' in markup or '安全验证码' in page.title
                    or '安全验证' in page.title or '滑动验证' in page.title
                ):
                    message = 'Gitee 验证尚未完成，已停止'
                    issues.append(SourceIssue('gitee', 'verification_incomplete', message))
                    return ContentCollectionResult(
                        (), (), tuple(issues), JobExtractionStats(failed=1), 'failed', message,
                    )
            elif browser.status == 405:
                raise HttpStatusError(405, {})
            location = urlsplit(page.url)
            if (
                location.scheme != 'https' or location.hostname != 'gitee.com'
                or location.username is not None or location.password is not None
                or location.path.rstrip('/') != '/explore/all'
            ):
                raise ValueError('Gitee 推荐页面已跳转')
            document = html.fromstring(markup)
            links = document.xpath(
                '//a[contains(concat(" ", normalize-space(@class), " "), " project-namespace-path ")]/@href'
            )
        except Cancelled:
            return ContentCollectionResult((), (), tuple(issues), JobExtractionStats(), 'cancelled', '用户取消')
        except HttpStatusError as exc:
            restricted = exc.status_code in {403, 405, 429}
            code = 'access_restricted' if restricted else 'recommendation_http_error'
            message = f'Gitee 推荐页返回 HTTP {exc.status_code}，已停止；请按网站提示处理'
            return ContentCollectionResult(
                (), (), tuple(issues) + (SourceIssue('gitee', code, message),),
                JobExtractionStats(failed=1), 'failed', message,
            )
        except (requests.RequestException, ValueError, ParserError):
            message = 'Gitee 推荐页请求失败或页面结构无效，已停止'
            return ContentCollectionResult(
                (), (), tuple(issues) + (SourceIssue('gitee', 'recommendation_request_failed', message),),
                JobExtractionStats(failed=1), 'failed', message,
            )
        if not links:
            message = 'Gitee 推荐页未找到项目；可能需要人工验证或页面结构已变化，已停止'
            return ContentCollectionResult(
                (), (), tuple(issues) + (SourceIssue('gitee', 'recommendation_page_unavailable', message),),
                JobExtractionStats(failed=1), 'failed', message,
            )
        candidates, snapshots = [], []
        issues.append(SourceIssue(
            'gitee', 'recommended_listing',
            f'来源 https://gitee.com/explore/all；按当前页顺序采集，上限 {limit} 条',
        ))
        seen_urls = set()
        skipped = failed = 0
        status, message = 'completed', 'Gitee 推荐项目：按推荐页当前顺序'
        for href in links:
            if len(candidates) >= limit:
                break
            try:
                if control:
                    control.check()
                url = urljoin('https://gitee.com/', href)
                if url in seen_urls:
                    skipped += 1
                    continue
                seen_urls.add(url)
                result = self.collect(url, control, notify)
            except Cancelled:
                status, message = 'cancelled', '用户取消'
                break
            except ValueError:
                skipped += 1
                issues.append(SourceIssue('gitee', 'invalid_recommendation', '推荐页包含无效仓库链接，已跳过'))
                continue
            candidates.extend(result.candidates)
            snapshots.extend(result.snapshots)
            issues.extend(result.issues)
            skipped += result.stats.skipped
            failed += result.stats.failed
            if result.status != 'completed':
                status, message = result.status, result.message
                break
        if status == 'completed' and not candidates:
            status, message = 'failed', '推荐页没有可采集的有效仓库'
            failed += 1
            issues.append(SourceIssue('gitee', 'no_valid_recommendations', message))
        return ContentCollectionResult(
            tuple(candidates), tuple(snapshots), tuple(issues),
            JobExtractionStats(content_candidates=len(candidates), skipped=skipped, failed=failed),
            status, message,
        )
