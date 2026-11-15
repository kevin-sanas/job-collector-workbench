import json
import unittest
from unittest.mock import Mock, patch

import requests

from app.discovery.http_client import HttpClient, HttpStatusError


class FakeResponse:
    def __init__(self, url, payload, status_code=200, headers=None):
        self.url = url
        self.status_code = status_code
        self.headers = headers or {}
        self.body = payload if isinstance(payload, bytes) else json.dumps(payload).encode('utf-8')
        self.closed = False

    def iter_content(self, chunk_size=8192):
        yield self.body

    def close(self):
        self.closed = True


class HttpClientTests(unittest.TestCase):
    def test_proxy_mode_is_per_session_and_request_has_no_credentials(self):
        session = Mock()
        session.get.return_value = FakeResponse('https://api.github.com/search/repositories', {'items': []})
        direct = HttpClient(
            'https://api.github.com', ('api.github.com',), 'direct', session=session,
        )

        response = direct.get_json('/search/repositories', {'q': 'python', 'per_page': 1})

        self.assertFalse(session.trust_env)
        self.assertEqual(response.data, {'items': []})
        self.assertEqual(session.get.call_count, 1)
        args, kwargs = session.get.call_args
        self.assertEqual(args[0], 'https://api.github.com/search/repositories')
        self.assertEqual(kwargs['params']['q'], 'python')
        self.assertFalse(kwargs['allow_redirects'])
        self.assertTrue(kwargs['stream'])
        self.assertNotIn('Authorization', kwargs['headers'])
        self.assertTrue(callable(kwargs['auth']))
        with patch('requests.sessions.get_netrc_auth', return_value=('synthetic', 'synthetic-secret')) as netrc:
            prepared = requests.Session().prepare_request(
                requests.Request('GET', args[0], auth=kwargs['auth'])
            )
        netrc.assert_not_called()
        self.assertNotIn('Authorization', prepared.headers)
        self.assertTrue(session.get.return_value.closed)

        system_session = Mock()
        HttpClient('https://api.github.com', ('api.github.com',), 'system', session=system_session)
        self.assertTrue(system_session.trust_env)

    def test_rejects_redirect_to_unlisted_host_before_request(self):
        session = Mock()
        session.get.return_value = FakeResponse(
            'https://api.github.com/search/repositories',
            b'',
            status_code=302,
            headers={'Location': 'https://elsewhere.example/private'},
        )
        client = HttpClient('https://api.github.com', ('api.github.com',), session=session)

        with self.assertRaises(ValueError):
            client.get_json('/search/repositories')

        self.assertEqual(session.get.call_count, 1)
        self.assertTrue(session.get.return_value.closed)

    def test_follows_valid_redirect_after_checking_target(self):
        session = Mock()
        first = FakeResponse(
            'https://api.github.com/old',
            b'',
            status_code=302,
            headers={'Location': '/search/repositories'},
        )
        second = FakeResponse('https://api.github.com/search/repositories', {'items': []})
        session.get.side_effect = [first, second]
        client = HttpClient('https://api.github.com', ('api.github.com',), session=session)

        response = client.get_json('/old')

        self.assertEqual(response.data, {'items': []})
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(session.get.call_args.args[0], 'https://api.github.com/search/repositories')
        self.assertTrue(first.closed and second.closed)

    def test_rejects_oversized_response_without_parsing(self):
        session = Mock()
        session.get.return_value = FakeResponse(
            'https://api.github.com/search/repositories', b'x' * 12,
        )
        client = HttpClient('https://api.github.com', ('api.github.com',), session=session, max_bytes=10)

        with self.assertRaises(ValueError):
            client.get_json('/search/repositories')

        self.assertTrue(session.get.return_value.closed)

    def test_local_proxy_uses_only_explicit_loopback_proxy(self):
        session = Mock()
        session.get.return_value = FakeResponse('https://api.github.com/search/repositories', {'items': []})
        client = HttpClient(
            'https://api.github.com',
            ('api.github.com',),
            'local_proxy',
            proxy_url='http://127.0.0.1:7890',
            session=session,
        )

        client.get_json('/search/repositories')

        self.assertFalse(session.trust_env)
        self.assertEqual(
            session.get.call_args.kwargs['proxies'],
            {'http': 'http://127.0.0.1:7890', 'https': 'http://127.0.0.1:7890'},
        )

    def test_local_proxy_rejects_remote_https_or_credentialed_address(self):
        for proxy_url in (
            '',
            'https://127.0.0.1:7890',
            'http://192.168.1.4:7890',
            'http://user:secret@127.0.0.1:7890',
            'http://127.0.0.1:0',
            'http://127.0.0.1:65536',
            'http://127.0.0.1',
            'http://127.0.0.1:7890/path',
            None,
        ):
            with self.subTest(proxy_url=proxy_url), self.assertRaises(ValueError):
                HttpClient(
                    'https://api.github.com',
                    ('api.github.com',),
                    'local_proxy',
                    proxy_url=proxy_url,
                    session=Mock(),
                )

    def test_rate_limit_response_does_not_retry_or_include_body(self):
        session = Mock()
        session.get.return_value = FakeResponse(
            'https://api.github.com/search/repositories',
            {'message': 'private downstream detail'},
            status_code=429,
            headers={'X-RateLimit-Remaining': '0'},
        )
        client = HttpClient('https://api.github.com', ('api.github.com',), session=session)

        with self.assertRaises(HttpStatusError) as context:
            client.get_json('/search/repositories')

        self.assertEqual(context.exception.status_code, 429)
        self.assertEqual(context.exception.headers['X-RateLimit-Remaining'], '0')
        self.assertNotIn('private downstream detail', str(context.exception))
        self.assertEqual(session.get.call_count, 1)

    def test_readme_json_requests_official_github_media_type(self):
        session = Mock()
        session.get.return_value = FakeResponse(
            'https://api.github.com/repos/acme/example/readme',
            {'encoding': 'base64', 'content': 'IyBFeGFtcGxl', 'sha': 'a' * 40},
        )
        client = HttpClient('https://api.github.com', ('api.github.com',), session=session)

        result = client.get_json(
            '/repos/acme/example/readme', accept='application/vnd.github+json',
        )

        self.assertEqual(result.data['encoding'], 'base64')
        self.assertEqual(session.get.call_args.kwargs['headers']['Accept'], 'application/vnd.github+json')
        with self.assertRaises(ValueError):
            client.get_json('/repos/acme/example/readme', accept='text/plain')
        self.assertEqual(session.get.call_count, 1)


if __name__ == '__main__':
    unittest.main()
