import base64
import binascii
import hashlib
import re
from dataclasses import replace
from datetime import datetime
from urllib.parse import urlsplit

import requests

from app.collector import Cancelled
from app.discovery.http_client import HttpClient, HttpStatusError
from app.discovery.readme_text import clean_readme
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
from app.models import canonical_url, redact_text


MAX_README_DECODED_BYTES = 128 * 1024
README_TRUNCATION_MARKER = '[内容已截断]'


class GithubSource:
    extractor_version = 'github-rest.v2'

    def __init__(self, client: HttpClient, dictionary: TechDictionary):
        self.client = client
        self.dictionary = dictionary
        self.aliases = {
            normalize_alias(alias): term.tech_key
            for term in dictionary.terms
            if term.enabled and term.match_mode != 'review'
            for alias in term.aliases
        }

    def collect(self, criteria: ContentCriteria, control=None, notify=None) -> ContentCollectionResult:
        try:
            if control:
                control.check()
        except Cancelled:
            return ContentCollectionResult(
                candidates=(), snapshots=(), issues=(),
                stats=JobExtractionStats(), status='cancelled', message='用户取消',
            )
        query = criteria.query
        if criteria.created_after:
            query += f' created:>={criteria.created_after}'
        try:
            response = self.client.get_json(
                '/search/repositories',
                params={
                    'q': query,
                    'sort': criteria.sort,
                    'order': 'desc',
                    'per_page': criteria.limit,
                },
            )
        except HttpStatusError as exc:
            if exc.status_code not in {403, 429}:
                raise
            return ContentCollectionResult(
                candidates=(),
                snapshots=(),
                issues=(SourceIssue(
                    source_id='github',
                    issue_code='rate_limited',
                    message=f'GitHub 接口返回 HTTP {exc.status_code}，本次采集已停止',
                ),),
                stats=JobExtractionStats(failed=1),
                status='failed',
                message='GitHub 接口限流',
            )
        except (requests.RequestException, ValueError):
            return ContentCollectionResult(
                candidates=(), snapshots=(),
                issues=(SourceIssue('github', 'request_failed', 'GitHub 接口请求或响应失败'),),
                stats=JobExtractionStats(failed=1),
                status='failed',
                message='GitHub 接口请求或响应失败',
            )
        if not isinstance(response.data, dict) or not isinstance(response.data.get('items'), list):
            return ContentCollectionResult(
                candidates=(), snapshots=(),
                issues=(SourceIssue('github', 'invalid_response', 'GitHub 仓库搜索结果结构无效'),),
                stats=JobExtractionStats(failed=1),
                status='failed',
                message='GitHub 仓库搜索结果结构无效',
            )

        fetched_at = datetime.now().astimezone().isoformat(timespec='microseconds')
        remaining = next(
            (value for key, value in response.headers.items() if key.casefold() == 'x-ratelimit-remaining'),
            '未知',
        )
        candidates = []
        snapshots = []
        issues = []
        seen = set()
        skipped = 0
        status = 'completed'
        message = ''
        for item in response.data['items'][:criteria.limit]:
            try:
                if control:
                    control.check()
            except Cancelled:
                status = 'cancelled'
                message = '用户取消'
                break
            if not isinstance(item, dict):
                skipped += 1
                issues.append(SourceIssue('github', 'invalid_item', '仓库结果不是对象'))
                continue
            raw_url = item.get('html_url')
            if not isinstance(raw_url, str):
                skipped += 1
                issues.append(SourceIssue('github', 'invalid_url', '仓库缺少公开主页链接'))
                continue
            source_url = canonical_url(raw_url)
            parsed = urlsplit(source_url)
            parts = parsed.path.strip('/').split('/')
            if (
                parsed.scheme != 'https'
                or parsed.hostname != 'github.com'
                or parsed.query
                or len(parts) != 2
                or any(not re.fullmatch(r'[A-Za-z0-9_.-]+', part) for part in parts)
            ):
                skipped += 1
                issues.append(SourceIssue('github', 'invalid_url', '仓库主页链接不符合 GitHub 格式'))
                continue
            if source_url in seen:
                skipped += 1
                continue
            owner, repository_name = parts
            description = redact_text(item.get('description'))[:500]
            raw_topics = item.get('topics')
            topics = tuple(
                topic.strip() for topic in raw_topics
                if isinstance(topic, str) and topic.strip()
            ) if isinstance(raw_topics, list) else ()
            primary_language = item.get('language') if isinstance(item.get('language'), str) else ''
            technology_names = (primary_language, *topics)
            tech_keys = tuple(sorted({
                self.aliases[normalized]
                for name in technology_names
                for variant in (name, name.replace('-', ' '))
                if (normalized := normalize_alias(variant)) in self.aliases
            }))
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
            stars = item.get('stargazers_count')
            forks = item.get('forks_count')
            try:
                candidate = ContentCandidate(
                    candidate_key=candidate_key,
                    content_type='open_source_project',
                    source_name='GitHub',
                    source_url=source_url,
                    title=f'{owner}/{repository_name}',
                    source_published_at=item.get('created_at') if isinstance(item.get('created_at'), str) else '',
                    fetched_at=fetched_at,
                    source_evidence_excerpt=description[:240],
                    proposed_summary='',
                    proposed_learning_action='',
                    tech_keys=tech_keys,
                    extractor_version=self.extractor_version,
                    review_state='pending',
                    platform='github',
                    repository_url=source_url,
                    owner=owner,
                    repository_name=repository_name,
                    description=description,
                    primary_language=primary_language,
                    topics=topics,
                    license_spdx=license_spdx,
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
                skipped += 1
                issues.append(SourceIssue('github', 'invalid_item', '仓库字段无法通过候选校验'))
                continue
            seen.add(source_url)
            snapshots.append(snapshot)
            candidates.append(candidate)
            if notify:
                notify({
                    'source': 'GitHub',
                    'processed': len(candidates),
                    'limit': criteria.limit,
                    'remaining': remaining,
                })

        if status == 'completed' and response.data.get('incomplete_results') is True:
            issues.append(SourceIssue('github', 'incomplete_results', 'GitHub 标记本次搜索结果不完整'))
            status = 'failed'
            message = 'GitHub 搜索结果不完整'
        if status == 'completed' and remaining == '0':
            issues.append(SourceIssue('github', 'rate_limit_exhausted', 'GitHub 仓库搜索请求后该接口剩余额度为零'))

        if status == 'completed':
            for index, candidate in enumerate(candidates):
                try:
                    if control:
                        control.check()
                except Cancelled:
                    status = 'cancelled'
                    message = '用户取消'
                    break

                readme_path = f'/repos/{candidate.owner}/{candidate.repository_name}/readme'
                readme_api_url = f'https://api.github.com{readme_path}'
                attempted_at = datetime.now().astimezone().isoformat(timespec='microseconds')
                detail_remaining = '未知'
                try:
                    detail = self.client.get_json(
                        readme_path, accept='application/vnd.github+json',
                    )
                except HttpStatusError as exc:
                    detail_remaining = next(
                        (value for key, value in exc.headers.items() if key.casefold() == 'x-ratelimit-remaining'),
                        '未知',
                    )
                    if exc.status_code in {403, 429}:
                        readme = ReadmeEvidence(
                            'rate_limited', readme_api_url, attempted_at,
                            reason=f'GitHub README 接口返回 HTTP {exc.status_code}',
                        )
                        issue_code = 'readme_rate_limited'
                        status = 'failed'
                        message = 'GitHub README 接口限流'
                    else:
                        readme = ReadmeEvidence(
                            'not_found' if exc.status_code == 404 else 'failed',
                            readme_api_url, attempted_at,
                            reason=f'GitHub README 接口返回 HTTP {exc.status_code}',
                        )
                        issue_code = 'readme_not_found' if exc.status_code == 404 else 'readme_http_error'
                except (requests.RequestException, ValueError) as exc:
                    too_large = isinstance(exc, ValueError) and '超过大小限制' in str(exc)
                    readme = ReadmeEvidence(
                        'too_large' if too_large else 'failed',
                        readme_api_url, attempted_at,
                        reason='README 接口响应超过 2 MB 上限' if too_large else 'README 接口请求或响应失败',
                    )
                    issue_code = 'readme_too_large' if too_large else 'readme_request_failed'
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
                        size_bytes = (
                            reported_size if type(reported_size) is int and reported_size >= 0
                            else len(raw)
                        )
                        if not raw and size_bytes > 0:
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
                                max_chars=12000 - len(README_TRUNCATION_MARKER) - 1
                                if byte_truncated else 12000,
                            )
                            if byte_truncated and not char_truncated:
                                cleaned += '\n' + README_TRUNCATION_MARKER
                            readme = ReadmeEvidence(
                                status='fetched',
                                source_url=payload.get('html_url') or readme_api_url,
                                fetched_at=attempted_at,
                                sha=payload.get('sha', ''),
                                content=cleaned,
                                size_bytes=max(size_bytes, len(raw)),
                                truncated=byte_truncated or char_truncated,
                            )
                            issue_code = ''
                    except (ValueError, TypeError, binascii.Error):
                        readme = ReadmeEvidence(
                            'failed', readme_api_url, attempted_at,
                            reason='GitHub README 响应字段无效',
                        )
                        issue_code = 'readme_invalid_response'

                candidates[index] = replace(candidate, readme=readme)
                if issue_code:
                    issues.append(SourceIssue('github', issue_code, readme.reason, candidate.candidate_key))
                if notify:
                    notify({
                        'source': 'GitHub', 'stage': 'readme',
                        'processed': index + 1, 'limit': len(candidates),
                        'remaining': detail_remaining,
                    })
                try:
                    if control:
                        control.check()
                except Cancelled:
                    status = 'cancelled'
                    message = '用户取消'
                    break
                if status == 'failed':
                    break
                if detail_remaining == '0':
                    issues.append(SourceIssue(
                        'github', 'rate_limit_exhausted',
                        'GitHub README 请求后剩余额度为零，已停止后续请求', candidate.candidate_key,
                    ))
                    if index < len(candidates) - 1:
                        status = 'failed'
                        message = 'GitHub README 接口剩余额度为零'
                    break
        return ContentCollectionResult(
            candidates=tuple(candidates),
            snapshots=tuple(snapshots),
            issues=tuple(issues),
            stats=JobExtractionStats(
                content_candidates=len(candidates),
                skipped=skipped,
                failed=1 if status == 'failed' else 0,
            ),
            status=status,
            message=message,
        )
