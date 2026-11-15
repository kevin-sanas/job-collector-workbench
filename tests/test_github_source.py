import base64
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import requests

from app.collector import Cancelled
from app.discovery.batches import build_content_batch
from app.discovery.http_client import HttpResult, HttpStatusError
from app.discovery.models import ContentCriteria, DiscoveryRun, TechDictionary, TechTerm
from app.discovery.sources.github import GithubSource
from app.discovery.storage import DiscoveryRepository


def term(key, name, aliases, mode='token'):
    return TechTerm(
        tech_key=key,
        display_name=name,
        aliases=aliases,
        category='test',
        enabled=True,
        match_mode=mode,
    )


def repo(name='example', url='https://github.com/acme/example'):
    return {
        'id': 42,
        'name': name,
        'full_name': f'acme/{name}',
        'html_url': url,
        'description': 'Python RAG learning project',
        'owner': {'login': 'acme'},
        'created_at': '2026-10-01T00:00:00Z',
        'pushed_at': '2026-10-25T00:00:00Z',
        'updated_at': '2026-10-25T00:00:00Z',
        'language': 'Python',
        'topics': ['rag', 'python', 'ai', 'unknown-topic'],
        'license': {'spdx_id': 'MIT'},
        'stargazers_count': 123,
        'forks_count': 8,
        'archived': False,
    }


def readme(name='example', content='# Example\n\nA Python RAG learning project.\n\n## Quick Start\n\n`python main.py`'):
    return HttpResult(
        data={
            'encoding': 'base64',
            'content': base64.b64encode(content.encode('utf-8')).decode('ascii'),
            'size': len(content.encode('utf-8')),
            'sha': 'a' * 40,
            'html_url': f'https://github.com/acme/{name}/blob/main/README.md',
        },
        headers={'X-RateLimit-Remaining': '58'},
        url=f'https://api.github.com/repos/acme/{name}/readme',
        status_code=200,
    )


def search(*items, remaining='9'):
    return HttpResult(
        data={'items': list(items), 'incomplete_results': False},
        headers={'X-RateLimit-Remaining': remaining},
        url='https://api.github.com/search/repositories',
        status_code=200,
    )


class GithubSourceTests(unittest.TestCase):
    def setUp(self):
        self.dictionary = TechDictionary(
            dictionary_version='test.1',
            terms=(
                term('python', 'Python', ('Python',)),
                term('rag', 'RAG', ('RAG',)),
                term('ai', 'AI', ('AI',), mode='review'),
            ),
        )
        self.client = Mock()
        self.source = GithubSource(self.client, self.dictionary)

    def test_collects_search_metadata_and_time_stamped_metrics(self):
        search = HttpResult(
            data={'items': [repo()], 'total_count': 1, 'incomplete_results': False},
            headers={'X-RateLimit-Remaining': '9'},
            url='https://api.github.com/search/repositories',
            status_code=200,
        )
        self.client.get_json.side_effect = [search, readme()]
        updates = []

        result = self.source.collect(
            ContentCriteria(query='rag', limit=3, sort='stars', created_after='2026-10-01'),
            notify=updates.append,
        )

        self.assertEqual(self.client.get_json.call_count, 2)
        args, kwargs = self.client.get_json.call_args_list[0]
        self.assertEqual(args[0], '/search/repositories')
        self.assertIn('created:>=2026-10-01', kwargs['params']['q'])
        self.assertEqual(kwargs['params']['sort'], 'stars')
        self.assertEqual(kwargs['params']['per_page'], 3)
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(candidate.content_type, 'open_source_project')
        self.assertEqual(candidate.source_url, 'https://github.com/acme/example')
        self.assertEqual(candidate.tech_keys, ('python', 'rag'))
        self.assertEqual(candidate.topics, ('rag', 'python', 'ai', 'unknown-topic'))
        self.assertEqual(candidate.license_spdx, 'MIT')
        self.assertEqual(candidate.proposed_summary, '')
        self.assertEqual(candidate.proposed_learning_action, '')
        self.assertEqual(candidate.readme.status, 'fetched')
        self.assertEqual(candidate.readme.sha, 'a' * 40)
        self.assertIn('Quick Start', candidate.readme.content)
        self.assertEqual(candidate.readme.source_url, 'https://github.com/acme/example/blob/main/README.md')
        self.assertEqual(
            self.client.get_json.call_args_list[1].kwargs['accept'],
            'application/vnd.github+json',
        )
        self.assertEqual(result.snapshots[0].candidate_key, candidate.candidate_key)
        self.assertEqual(result.snapshots[0].stars, 123)
        self.assertEqual(result.snapshots[0].forks, 8)
        self.assertTrue(result.snapshots[0].fetched_at)
        self.assertEqual(result.stats.content_candidates, 1)
        self.assertEqual(result.status, 'completed')
        self.assertEqual(updates[0]['remaining'], '9')
        self.assertNotIn('description', updates[0])

    def test_skips_invalid_and_duplicate_links_without_requesting_details(self):
        first = repo()
        duplicate = dict(first, id=43)
        invalid = repo('bad', 'https://evil.example/acme/bad')
        search = HttpResult(
            data={'items': [first, duplicate, invalid], 'incomplete_results': False},
            headers={},
            url='https://api.github.com/search/repositories',
            status_code=200,
        )
        self.client.get_json.side_effect = [search, HttpStatusError(404, {})]

        result = self.source.collect(ContentCriteria(query='rag', limit=3, sort='updated'))

        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.stats.skipped, 2)
        self.assertEqual(self.client.get_json.call_count, 2)
        self.assertEqual(self.client.get_json.call_args_list[1].args[0], '/repos/acme/example/readme')

    def test_rate_limited_request_stops_without_retry(self):
        self.client.get_json.side_effect = HttpStatusError(403, {'X-RateLimit-Remaining': '0'})

        result = self.source.collect(ContentCriteria(query='rag', limit=3, sort='stars'))

        self.assertEqual(result.candidates, ())
        self.assertEqual(result.stats.failed, 1)
        self.assertEqual(result.status, 'failed')
        self.assertEqual(result.issues[0].issue_code, 'rate_limited')
        self.assertEqual(self.client.get_json.call_count, 1)

    def test_zero_remaining_after_search_keeps_candidate_and_reads_readme(self):
        search_result = HttpResult(
            data={'items': [repo()], 'incomplete_results': False},
            headers={'x-ratelimit-remaining': '0'},
            url='https://api.github.com/search/repositories',
            status_code=200,
        )
        self.client.get_json.side_effect = [search_result, readme()]

        result = self.source.collect(ContentCriteria(query='rag', limit=3, sort='stars'))

        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.issues[0].issue_code, 'rate_limit_exhausted')
        self.assertEqual(result.status, 'completed')
        self.assertEqual(result.stats.failed, 0)
        self.assertEqual(result.candidates[0].readme.status, 'fetched')
        self.assertEqual(self.client.get_json.call_count, 2)
        with TemporaryDirectory(dir=Path.cwd()) as directory:
            repository = DiscoveryRepository(Path(directory) / 'discovery.sqlite3')
            run = DiscoveryRun.start('content', '', 'github-rest.v2', ())
            repository.start_run(run)
            repository.finish_content_run(run, result, status=result.status, message=result.message)
            completed = repository.content_run(run.run_id)
            self.assertEqual(len(repository.content_candidates(run.run_id)), 1)
            self.assertEqual(completed.status, 'completed')
            batch = build_content_batch(
                completed,
                repository.content_candidates(run.run_id),
                repository.content_snapshots(run.run_id),
                repository.content_issues(run.run_id),
            )
            self.assertEqual(batch.content_candidates[0]['readme']['status'], 'fetched')

    def test_same_second_runs_store_distinct_metric_snapshots(self):
        first_item = repo()
        second_item = dict(first_item, stargazers_count=124)
        self.client.get_json.side_effect = [
            HttpResult(
                data={'items': [first_item], 'incomplete_results': False}, headers={},
                url='https://api.github.com/search/repositories', status_code=200,
            ),
            HttpStatusError(404, {}),
            HttpResult(
                data={'items': [second_item], 'incomplete_results': False}, headers={},
                url='https://api.github.com/search/repositories', status_code=200,
            ),
            HttpStatusError(404, {}),
        ]
        with patch('app.discovery.sources.github.datetime', create=True) as clock:
            clock.now.side_effect = [
                datetime(2026, 10, 26, 12, 0, 0, 100, tzinfo=timezone.utc),
                datetime(2026, 10, 26, 12, 0, 0, 100, tzinfo=timezone.utc),
                datetime(2026, 10, 26, 12, 0, 0, 101, tzinfo=timezone.utc),
                datetime(2026, 10, 26, 12, 0, 0, 101, tzinfo=timezone.utc),
            ]
            criteria = ContentCriteria(query='rag', limit=1, sort='stars')
            first_result = self.source.collect(criteria)
            second_result = self.source.collect(criteria)

        first_time = first_result.snapshots[0].fetched_at
        second_time = second_result.snapshots[0].fetched_at
        self.assertNotEqual(first_time, second_time)
        self.assertIn('.000100', first_time)
        self.assertIn('.000101', second_time)
        with TemporaryDirectory(dir=Path.cwd()) as directory:
            repository = DiscoveryRepository(Path(directory) / 'discovery.sqlite3')
            for result in (first_result, second_result):
                run = DiscoveryRun.start('content', '', 'github-rest.v2', ())
                repository.start_run(run)
                repository.finish_content_run(run, result)
            snapshots = repository.candidate_snapshots(first_result.candidates[0].candidate_key)
        self.assertEqual([snapshot.stars for snapshot in snapshots], [123, 124])

    def test_cancel_during_items_returns_completed_partial_candidates(self):
        self.client.get_json.return_value = HttpResult(
            data={'items': [repo(), repo('second', 'https://github.com/acme/second')]},
            headers={'X-RateLimit-Remaining': '8'},
            url='https://api.github.com/search/repositories',
            status_code=200,
        )
        control = Mock()
        control.check.side_effect = [None, None, Cancelled()]

        result = self.source.collect(
            ContentCriteria(query='rag', limit=3, sort='stars'), control=control,
        )

        self.assertEqual(result.status, 'cancelled')
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.stats.content_candidates, 1)
        self.assertEqual(self.client.get_json.call_count, 1)

    def test_invalid_response_returns_failed_issue(self):
        self.client.get_json.return_value = HttpResult(
            data={'items': 'not a list'}, headers={},
            url='https://api.github.com/search/repositories', status_code=200,
        )

        result = self.source.collect(ContentCriteria(query='rag', limit=3, sort='stars'))

        self.assertEqual(result.status, 'failed')
        self.assertEqual(result.issues[0].issue_code, 'invalid_response')
        self.assertEqual(result.candidates, ())

    def test_long_description_is_trimmed_without_dropping_repository(self):
        item = repo()
        item['description'] = 'A' * 550
        self.client.get_json.return_value = HttpResult(
            data={'items': [item]}, headers={},
            url='https://api.github.com/search/repositories', status_code=200,
        )

        result = self.source.collect(ContentCriteria(query='rag', limit=1, sort='stars'))

        self.assertEqual(result.status, 'completed')
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(len(result.candidates[0].description), 500)

    def test_missing_readme_keeps_candidate_and_continues(self):
        second = repo('second', 'https://github.com/acme/second')
        self.client.get_json.side_effect = [
            search(repo(), second), HttpStatusError(404, {}), readme('second'),
        ]

        result = self.source.collect(ContentCriteria('rag', 2, 'stars'))

        self.assertEqual(result.status, 'completed')
        self.assertEqual(len(result.candidates), 2)
        self.assertEqual(result.candidates[0].readme.status, 'not_found')
        self.assertEqual(result.candidates[0].readme.sha, '')
        self.assertEqual(result.candidates[1].readme.status, 'fetched')
        self.assertEqual(result.issues[0].issue_code, 'readme_not_found')
        self.assertEqual(self.client.get_json.call_count, 3)

    def test_readme_network_failure_keeps_candidate_and_reports_reason(self):
        self.client.get_json.side_effect = [search(repo()), requests.ConnectionError('synthetic')]

        result = self.source.collect(ContentCriteria('rag', 1, 'stars'))

        self.assertEqual(result.status, 'completed')
        self.assertEqual(result.candidates[0].readme.status, 'failed')
        self.assertEqual(result.issues[0].issue_code, 'readme_request_failed')
        self.assertNotIn('synthetic', result.candidates[0].readme.reason)

    def test_readme_rate_limit_preserves_search_candidates_and_stops(self):
        self.client.get_json.side_effect = [
            search(repo(), repo('second', 'https://github.com/acme/second')),
            HttpStatusError(429, {'X-RateLimit-Remaining': '0'}),
        ]

        result = self.source.collect(ContentCriteria('rag', 2, 'stars'))

        self.assertEqual(result.status, 'failed')
        self.assertEqual(len(result.candidates), 2)
        self.assertEqual(result.candidates[0].readme.status, 'rate_limited')
        self.assertIsNone(result.candidates[1].readme)
        self.assertEqual(result.issues[0].issue_code, 'readme_rate_limited')
        self.assertEqual(self.client.get_json.call_count, 2)

    def test_zero_remaining_after_readme_retains_fetched_partial_result(self):
        first_readme = readme()
        first_readme = HttpResult(
            data=first_readme.data,
            headers={'X-RateLimit-Remaining': '0'},
            url=first_readme.url,
            status_code=200,
        )
        self.client.get_json.side_effect = [
            search(repo(), repo('second', 'https://github.com/acme/second')),
            first_readme,
        ]

        result = self.source.collect(ContentCriteria('rag', 2, 'stars'))

        self.assertEqual(result.status, 'failed')
        self.assertEqual(result.candidates[0].readme.status, 'fetched')
        self.assertIsNone(result.candidates[1].readme)
        self.assertEqual(result.issues[0].issue_code, 'rate_limit_exhausted')
        self.assertEqual(self.client.get_json.call_count, 2)

    def test_zero_remaining_after_last_readme_keeps_completed_result(self):
        last_readme = readme()
        self.client.get_json.side_effect = [
            search(repo()),
            HttpResult(
                data=last_readme.data,
                headers={'X-RateLimit-Remaining': '0'},
                url=last_readme.url,
                status_code=200,
            ),
        ]

        result = self.source.collect(ContentCriteria('rag', 1, 'stars'))

        self.assertEqual(result.status, 'completed')
        self.assertEqual(result.candidates[0].readme.status, 'fetched')
        self.assertEqual(result.issues[0].issue_code, 'rate_limit_exhausted')
        self.assertEqual(self.client.get_json.call_count, 2)

    def test_cancel_before_next_readme_keeps_prior_readme(self):
        self.client.get_json.side_effect = [
            search(repo(), repo('second', 'https://github.com/acme/second')),
            readme(),
        ]
        control = Mock()
        control.check.side_effect = [None, None, None, None, None, Cancelled()]

        result = self.source.collect(ContentCriteria('rag', 2, 'stars'), control=control)

        self.assertEqual(result.status, 'cancelled')
        self.assertEqual(result.candidates[0].readme.status, 'fetched')
        self.assertIsNone(result.candidates[1].readme)
        self.assertEqual(self.client.get_json.call_count, 2)

    def test_readme_body_is_bounded_and_marks_truncation(self):
        body = '# Example\n\n![badge](https://example.com/badge.svg)\n\n## Features\n\n' + 'useful purpose. ' * 10000
        self.client.get_json.side_effect = [search(repo()), readme(content=body)]

        result = self.source.collect(ContentCriteria('rag', 1, 'stars'))

        evidence = result.candidates[0].readme
        self.assertEqual(evidence.status, 'fetched')
        self.assertTrue(evidence.truncated)
        self.assertLessEqual(len(evidence.content), 12000)
        self.assertIn('[内容已截断]', evidence.content)
        self.assertNotIn('badge.svg', evidence.content)
        self.assertEqual(evidence.size_bytes, len(body.encode('utf-8')))

    def test_oversized_response_keeps_candidate_without_body(self):
        self.client.get_json.side_effect = [
            search(repo()), ValueError('来源响应超过大小限制'),
        ]

        result = self.source.collect(ContentCriteria('rag', 1, 'stars'))

        self.assertEqual(result.status, 'completed')
        self.assertEqual(result.candidates[0].readme.status, 'too_large')
        self.assertEqual(result.candidates[0].readme.content, '')
        self.assertEqual(result.issues[0].issue_code, 'readme_too_large')

    def test_invalid_readme_response_keeps_candidate_without_claimed_summary(self):
        bad = readme()
        payload = dict(bad.data, sha='')
        self.client.get_json.side_effect = [
            search(repo()),
            HttpResult(payload, bad.headers, bad.url, bad.status_code),
        ]

        result = self.source.collect(ContentCriteria('rag', 1, 'stars'))

        self.assertEqual(result.status, 'completed')
        self.assertEqual(result.candidates[0].readme.status, 'failed')
        self.assertEqual(result.issues[0].issue_code, 'readme_invalid_response')
        self.assertEqual(result.candidates[0].proposed_summary, '')


if __name__ == '__main__':
    unittest.main()
