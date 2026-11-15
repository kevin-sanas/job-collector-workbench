import base64
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import requests

from app.collector import Cancelled
from app.discovery.batches import build_content_batch, parse_content_batch, serialize_batch
from app.discovery.content_service import ContentDiscoveryService
from app.discovery.http_client import HttpResult, HttpStatusError
from app.discovery.models import ContentCriteria, TechDictionary, TechTerm
from app.discovery.sources.gitee import GiteeSource
from app.discovery.storage import DiscoveryRepository


def repository(**changes):
    data = {
        'full_name': 'acme/student-lab',
        'html_url': 'https://gitee.com/acme/student-lab.git',
        'description': 'Python service learning project',
        'language': 'Python',
        'stargazers_count': 12,
        'forks_count': 3,
        'pushed_at': '2026-10-25T00:00:00+08:00',
        'created_at': '2026-10-01T00:00:00+08:00',
        'private': False,
        'public': True,
        'license': None,
    }
    data.update(changes)
    return HttpResult(
        data, {'X-RateLimit-Remaining': '58'},
        'https://gitee.com/api/v5/repos/acme/student-lab', 200,
    )


def readme(content='# Student Lab\n\n## Quick Start\n\n```sh\npython main.py\n```', **changes):
    raw = content.encode('utf-8')
    data = {
        'encoding': 'base64',
        'content': base64.b64encode(raw).decode('ascii'),
        'size': len(raw),
        'sha': 'a' * 40,
        'html_url': 'https://gitee.com/acme/student-lab/blob/master/README.md',
    }
    data.update(changes)
    return HttpResult(
        data, {'X-RateLimit-Remaining': '57'},
        'https://gitee.com/api/v5/repos/acme/student-lab/readme', 200,
    )


class GiteeSourceTests(unittest.TestCase):
    def setUp(self):
        dictionary = TechDictionary(
            dictionary_version='test-dictionary.2026-10-26',
            terms=(TechTerm('python', 'Python', ('Python',), 'language', True, 'token'),),
        )
        self.client = Mock()
        self.browser = Mock()
        self.browser.status = 200
        self.browser.page = SimpleNamespace(url='https://gitee.com/explore/all', title='推荐项目', html='')
        self.browser.ensure_page.return_value = self.browser.page
        self.source = GiteeSource(self.client, dictionary, recommendation_browser=self.browser)

    def test_collects_repository_and_readme_with_canonical_identity(self):
        self.client.get_json.side_effect = [repository(), readme()]
        updates = []

        result = self.source.collect('https://gitee.com/acme/student-lab.git', notify=updates.append)

        self.assertEqual(result.status, 'completed')
        self.assertEqual(result.stats.content_candidates, 1)
        self.assertEqual(result.issues, ())
        self.assertEqual(self.source.dictionary_version, 'test-dictionary.2026-10-26')
        self.assertEqual(self.source.extractor_version, 'gitee-rest.v1')
        self.assertEqual(self.client.get_json.call_args_list[0].args[0], '/api/v5/repos/acme/student-lab')
        self.assertEqual(self.client.get_json.call_args_list[1].args[0], '/api/v5/repos/acme/student-lab/readme')
        candidate = result.candidates[0]
        self.assertEqual(candidate.source_url, 'https://gitee.com/acme/student-lab')
        self.assertEqual(candidate.repository_url, candidate.source_url)
        self.assertEqual(candidate.candidate_key, hashlib.sha256(
            b'open_source_project\nhttps://gitee.com/acme/student-lab'
        ).hexdigest())
        self.assertEqual(candidate.platform, 'gitee')
        self.assertEqual(candidate.tech_keys, ('python',))
        self.assertEqual(candidate.license_spdx, '')
        self.assertEqual(candidate.proposed_summary, '')
        self.assertEqual(candidate.proposed_learning_action, '')
        self.assertEqual(candidate.review_state, 'pending')
        self.assertEqual(candidate.readme.status, 'fetched')
        self.assertEqual(candidate.readme.sha, 'a' * 40)
        self.assertEqual(candidate.readme.source_url, 'https://gitee.com/acme/student-lab/blob/master/README.md')
        self.assertIn('python main.py', candidate.readme.content)
        self.assertEqual(result.snapshots[0].candidate_key, candidate.candidate_key)
        self.assertEqual(result.snapshots[0].stars, 12)
        self.assertEqual(result.snapshots[0].forks, 3)
        self.assertEqual(result.snapshots[0].archived, None)
        self.assertEqual(updates[-1]['stage'], 'readme')

    def test_keyword_search_sorts_only_returned_pool_and_collects_readme(self):
        search_result = HttpResult(
            {
                'total_count': 140,
                'data': [
                    {'url': 'https://gitee.com/acme/older', 'stars': 400,
                     'updated_at': '2024-01-01 00:00:00'},
                    {'url': 'https://gitee.com/acme/student-lab', 'stars': 12,
                     'updated_at': '2026-10-25 12:00:00'},
                ],
            },
            {'X-RateLimit-Remaining': '59'},
            'https://gitee.com/api/v5/search/repos?q=python&size=100&from=0', 200,
        )
        self.client.get_json.side_effect = [search_result, repository(), readme()]

        result = self.source.collect(ContentCriteria('python', 1, 'updated'))

        self.assertEqual(result.status, 'completed')
        self.assertEqual(result.candidates[0].title, 'acme/student-lab')
        self.assertEqual(result.candidates[0].readme.status, 'fetched')
        self.assertEqual(result.issues[0].issue_code, 'bounded_search_pool')
        args, kwargs = self.client.get_json.call_args_list[0]
        self.assertEqual(args, ('/api/v5/search/repos',))
        self.assertEqual(kwargs['params'], {'q': 'python', 'size': 100, 'from': 0})
        self.assertEqual(self.client.get_json.call_count, 3)

    def test_keyword_search_handles_no_results_rate_limit_and_cancel(self):
        criteria = ContentCriteria('no-match', 2, 'stars')
        self.client.get_json.return_value = HttpResult(
            {'total_count': 0, 'data': []}, {'X-RateLimit-Remaining': '59'},
            'https://gitee.com/api/v5/search/repos', 200,
        )
        empty = self.source.collect(criteria)
        self.assertEqual(empty.status, 'completed')
        self.assertEqual(empty.candidates, ())
        self.assertEqual(empty.issues[0].issue_code, 'no_results')
        self.assertEqual(self.client.get_json.call_count, 1)

        self.client.reset_mock()
        self.client.get_json.side_effect = HttpStatusError(429, {'X-RateLimit-Remaining': '0'})
        limited = self.source.collect(criteria)
        self.assertEqual(limited.status, 'failed')
        self.assertEqual(limited.issues[0].issue_code, 'rate_limited')
        self.assertEqual(self.client.get_json.call_count, 1)

        self.client.reset_mock()
        control = Mock()
        control.check.side_effect = Cancelled()
        cancelled = self.source.collect(criteria, control=control)
        self.assertEqual(cancelled.status, 'cancelled')
        self.client.get_json.assert_not_called()

    def test_request_failures_preserve_exception_type_without_sensitive_details(self):
        for criteria in ('https://gitee.com/acme/student-lab', ContentCriteria('python', 1, 'stars')):
            for error in (requests.ConnectTimeout('SYNTHETIC_SECRET'),
                          requests.ReadTimeout('SYNTHETIC_SECRET'),
                          requests.exceptions.SSLError('SYNTHETIC_SECRET'),
                          ValueError('SYNTHETIC_SECRET')):
                with self.subTest(criteria=type(criteria).__name__, error=type(error).__name__):
                    self.client.reset_mock()
                    self.client.get_json.side_effect = error
                    result = self.source.collect(criteria)
                    self.assertEqual(result.status, 'failed')
                    self.assertEqual(result.issues[0].issue_code, 'request_failed')
                    self.assertIn(type(error).__name__, result.message)
                    self.assertEqual(result.issues[0].message, result.message)
                    self.assertNotIn('SYNTHETIC_SECRET', result.message)
                    self.assertEqual(self.client.get_json.call_count, 1)

    def test_keyword_batch_stops_when_readme_uses_last_request(self):
        search_result = HttpResult(
            {'total_count': 2, 'data': [
                {'url': 'https://gitee.com/acme/student-lab', 'stars': 12},
                {'url': 'https://gitee.com/acme/second', 'stars': 1},
            ]},
            {'X-RateLimit-Remaining': '2'},
            'https://gitee.com/api/v5/search/repos', 200,
        )
        last_readme = readme()
        last_readme.headers['X-RateLimit-Remaining'] = '0'
        self.client.get_json.side_effect = [search_result, repository(), last_readme]

        result = self.source.collect(ContentCriteria('student', 2, 'stars'))

        self.assertEqual(result.status, 'failed')
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.issues[0].issue_code, 'rate_limit_exhausted')
        self.assertEqual(self.client.get_json.call_count, 3)

    def test_keyword_search_criteria_survives_storage_and_json_round_trip(self):
        search_result = HttpResult(
            {'total_count': 1, 'data': [
                {'url': 'https://gitee.com/acme/student-lab', 'stars': 12,
                 'updated_at': '2026-10-25 12:00:00'},
            ]},
            {'X-RateLimit-Remaining': '59'},
            'https://gitee.com/api/v5/search/repos', 200,
        )
        self.client.get_json.side_effect = [search_result, repository(), readme()]
        criteria = ContentCriteria('python', 1, 'stars')
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            storage = DiscoveryRepository(Path(directory) / 'discovery.sqlite3')
            run = ContentDiscoveryService(storage, self.source).collect(criteria)
            batch = build_content_batch(
                run, storage.content_candidates(run.run_id),
                storage.content_snapshots(run.run_id), storage.content_issues(run.run_id),
            )
            encoded = serialize_batch(batch, self.source.dictionary)
            restored, candidates, snapshots, _, _ = parse_content_batch(
                encoded, self.source.dictionary,
            )
        self.assertEqual(restored.search_criteria, criteria)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(len(snapshots), 1)

    def test_recommended_page_collects_in_order_and_records_provenance(self):
        self.browser.page.url = 'https://gitee.com/explore/all?svcp_stk=SYNTHETIC_ONLY'
        self.browser.page.html = (
            '<html><a class="title project-namespace-path" href="/acme/student-lab">'
            'Student Lab</a><a class="title project-namespace-path" href="/acme/second">'
            'Second</a></html>'
        )
        self.client.get_json.side_effect = [repository(), readme()]
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            storage = DiscoveryRepository(Path(directory) / 'discovery.sqlite3')
            run = ContentDiscoveryService(storage, self.source).collect({'mode': 'recommended', 'limit': 1})
            batch = build_content_batch(
                run, storage.content_candidates(run.run_id),
                storage.content_snapshots(run.run_id), storage.content_issues(run.run_id),
            )
            encoded = serialize_batch(batch, self.source.dictionary)
            restored, candidates, snapshots, issues, _ = parse_content_batch(encoded, self.source.dictionary)
        self.assertEqual(run.status, 'completed')
        self.assertIsNone(restored.search_criteria)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].repository_url, 'https://gitee.com/acme/student-lab')
        self.assertEqual(candidates[0].readme.status, 'fetched')
        self.assertEqual(snapshots[0].candidate_key, candidates[0].candidate_key)
        self.assertEqual(issues[0].issue_code, 'recommended_listing')
        self.assertIn('https://gitee.com/explore/all', issues[0].message)
        self.assertNotIn(b'SYNTHETIC_ONLY', encoded)
        self.browser.open.assert_called_once()
        self.assertEqual(self.client.get_json.call_count, 2)

    def test_recommended_access_restriction_and_unrecognized_page_stop(self):
        for status, markup, code in (
            (403, '', 'access_restricted'),
            (429, '', 'access_restricted'),
            (405, '', 'access_restricted'),
            (200, '<html><title>其他页面</title></html>', 'recommendation_page_unavailable'),
        ):
            with self.subTest(code=code):
                self.client.reset_mock()
                self.browser.reset_mock()
                self.browser.status = status
                self.browser.page.html = markup
                result = self.source.collect({'mode': 'recommended', 'limit': 1})
                self.assertEqual(result.status, 'failed')
                self.assertEqual(result.issues[0].issue_code, code)
                self.client.get_json.assert_not_called()
                self.browser.open.assert_called_once()

    def test_recommended_cancellation_and_detail_limit_stop(self):
        self.browser.page.html = (
            '<a class="title project-namespace-path" href="/acme/student-lab">A</a>'
            '<a class="title project-namespace-path" href="/acme/second">B</a>'
        )
        control = Mock()
        control.check.side_effect = [None, None, Cancelled()]
        cancelled = self.source.collect({'mode': 'recommended', 'limit': 2}, control)
        self.assertEqual(cancelled.status, 'cancelled')
        self.client.get_json.assert_not_called()
        self.client.get_json.side_effect = [repository(), HttpStatusError(429, {})]
        limited = self.source.collect({'mode': 'recommended', 'limit': 2})
        self.assertEqual(limited.status, 'failed')
        self.assertEqual(len(limited.candidates), 1)
        self.assertEqual(limited.candidates[0].readme.status, 'rate_limited')
        self.assertEqual(self.client.get_json.call_count, 2)

    def test_recommended_verification_waits_for_user_before_reading_repositories(self):
        self.browser.status = 405
        self.browser.page.title = '安全验证码-独立验证'
        self.browser.page.html = '<script id="BIOC_CONFIG_SCRIPT"></script>'
        control = Mock()
        def manually_resolved(timeout):
            self.client.get_json.assert_not_called()
            self.browser.page.title = '推荐项目'
            self.browser.page.html = '<a class="title project-namespace-path" href="/acme/student-lab">A</a>'
        control.wait_for_user.side_effect = manually_resolved
        self.client.get_json.side_effect = [repository(), readme()]
        notices = []

        result = self.source.collect({'mode': 'recommended', 'limit': 1}, control, notices.append)

        self.assertEqual(result.status, 'completed')
        self.assertIn('manual_verification', {issue.issue_code for issue in result.issues})
        self.assertEqual(notices[0]['stage'], 'waiting_verification')
        self.assertEqual(control.wait_for_user.call_count, 1)
        self.browser.open.assert_called_once()
        self.browser.navigate.assert_not_called()

    def test_cancel_while_waiting_for_recommended_page_verification(self):
        self.browser.status = 405
        self.browser.page.title = '安全验证码-独立验证'
        self.browser.page.html = '<script id="BIOC_CONFIG_SCRIPT"></script>'
        control = Mock()
        control.wait_for_user.side_effect = Cancelled()
        result = self.source.collect({'mode': 'recommended', 'limit': 1}, control, lambda update: None)
        self.assertEqual(result.status, 'cancelled')
        self.assertEqual(result.issues[0].issue_code, 'manual_verification')
        self.client.get_json.assert_not_called()

    def test_missing_readme_retains_repository_candidate(self):
        self.client.get_json.side_effect = [repository(), HttpStatusError(404, {})]

        result = self.source.collect('https://gitee.com/acme/student-lab')

        self.assertEqual(result.status, 'completed')
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(len(result.snapshots), 1)
        self.assertEqual(result.candidates[0].readme.status, 'not_found')
        self.assertEqual(result.candidates[0].readme.source_url,
                         'https://gitee.com/api/v5/repos/acme/student-lab/readme')
        self.assertEqual(result.issues[0].issue_code, 'readme_not_found')
        self.assertEqual(result.issues[0].candidate_key, result.candidates[0].candidate_key)

    def test_readme_rate_limit_stops_without_retry_but_retains_candidate(self):
        self.client.get_json.side_effect = [repository(), HttpStatusError(429, {'X-RateLimit-Remaining': '0'})]

        result = self.source.collect('https://gitee.com/acme/student-lab')

        self.assertEqual(result.status, 'failed')
        self.assertEqual(result.stats.failed, 1)
        self.assertEqual(result.candidates[0].readme.status, 'rate_limited')
        self.assertEqual(result.issues[0].issue_code, 'readme_rate_limited')
        self.assertEqual(self.client.get_json.call_count, 2)

    def test_exhausted_quota_after_repository_stops_before_readme(self):
        result_data = repository()
        result_data.headers['X-RateLimit-Remaining'] = '0'
        self.client.get_json.return_value = result_data

        result = self.source.collect('https://gitee.com/acme/student-lab')

        self.assertEqual(result.status, 'failed')
        self.assertEqual(result.candidates[0].readme.status, 'rate_limited')
        self.assertEqual(result.issues[0].issue_code, 'rate_limit_exhausted')
        self.assertEqual(self.client.get_json.call_count, 1)

    def test_cancel_after_repository_skips_readme_request(self):
        self.client.get_json.return_value = repository()
        control = Mock()
        control.check.side_effect = [None, Cancelled()]

        result = self.source.collect('https://gitee.com/acme/student-lab', control=control)

        self.assertEqual(result.status, 'cancelled')
        self.assertEqual(len(result.candidates), 1)
        self.assertIsNone(result.candidates[0].readme)
        self.assertEqual(self.client.get_json.call_count, 1)

    def test_repository_rate_limit_and_request_failure_return_failed_without_candidate(self):
        for error, code in (
            (HttpStatusError(403, {'X-RateLimit-Remaining': '0'}), 'rate_limited'),
            (requests.ConnectionError('synthetic'), 'request_failed'),
        ):
            with self.subTest(code=code):
                self.client.reset_mock()
                self.client.get_json.side_effect = error
                result = self.source.collect('https://gitee.com/acme/student-lab')
                self.assertEqual(result.status, 'failed')
                self.assertEqual(result.issues[0].issue_code, code)
                self.assertEqual(result.candidates, ())
                self.assertEqual(self.client.get_json.call_count, 1)
                self.assertNotIn('synthetic', result.message)

    def test_readme_request_failure_and_wrong_repository_url_keep_candidate(self):
        for detail, code in (
            (requests.ConnectionError('synthetic'), 'readme_request_failed'),
            (readme(html_url='https://gitee.com/other/repo/blob/master/README.md'),
             'readme_invalid_response'),
            (readme(sha='a' * 41), 'readme_invalid_response'),
        ):
            with self.subTest(code=code):
                self.client.reset_mock()
                self.client.get_json.side_effect = [repository(), detail]
                result = self.source.collect('https://gitee.com/acme/student-lab')
                self.assertEqual(result.status, 'completed')
                self.assertEqual(result.candidates[0].readme.status, 'failed')
                self.assertEqual(result.issues[0].issue_code, code)
                self.assertEqual(self.client.get_json.call_count, 2)

    def test_unknown_metrics_are_null_and_does_not_guess_license_or_topics(self):
        self.client.get_json.side_effect = [
            repository(stargazers_count=None, forks_count='0', pushed_at=None,
                       archived=None, license=None, language=None, topics=None),
            HttpStatusError(404, {}),
        ]

        result = self.source.collect('https://gitee.com/acme/student-lab')

        self.assertEqual(result.status, 'completed')
        self.assertIsNone(result.snapshots[0].stars)
        self.assertIsNone(result.snapshots[0].forks)
        self.assertEqual(result.snapshots[0].pushed_at, '')
        self.assertIsNone(result.snapshots[0].archived)
        self.assertEqual(result.candidates[0].license_spdx, '')
        self.assertEqual(result.candidates[0].topics, ())
        self.assertEqual(result.candidates[0].tech_keys, ())

    def test_rejects_non_repository_urls_without_network_request(self):
        invalid = (
            'http://gitee.com/acme/student-lab',
            'https://evil.example/acme/student-lab',
            'https://user:secret@gitee.com/acme/student-lab',
            'https://gitee.com/acme/student-lab?token=secret',
            'https://gitee.com/acme/student-lab/issues',
            'https://gitee.com/acme/%2e%2e',
            'https://gitee.com/../student-lab',
            'https://gitee.com/acme/..',
        )
        for url in invalid:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    self.source.collect(url)
        self.client.get_json.assert_not_called()


if __name__ == '__main__':
    unittest.main()
