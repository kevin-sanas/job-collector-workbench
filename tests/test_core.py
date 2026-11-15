import json
import tempfile
import unittest
from pathlib import Path

from app.models import Job, Filters, parse_salary, redact_text, canonical_url
from app.storage import Repository
from app.config import AppPaths


class ModelTests(unittest.TestCase):
    def test_salary_preserves_period_and_month_count(self):
        self.assertEqual(parse_salary('15-25K·14薪'), (15000, 25000, '月', 14))
        self.assertEqual(parse_salary('200元/天'), (200, 200, '天', None))
        self.assertEqual(parse_salary('30-50万/年'), (300000, 500000, '年', None))
        self.assertEqual(parse_salary('面议'), (None, None, '', None))

    def test_privacy_filters_contacts_without_removing_salary(self):
        value = redact_text('薪资 15000 联系 13812345678 hr@example.com 微信：hire_123')
        self.assertIn('15000', value)
        self.assertNotIn('13812345678', value)
        self.assertNotIn('hr@example.com', value)
        self.assertNotIn('hire_123', value)

    def test_url_retains_identity_query(self):
        self.assertEqual(canonical_url('https://example.cn/jobs?id=12&utm_source=x#top'), 'https://example.cn/jobs?id=12')

    def test_url_drops_sensitive_query_values_at_job_entry(self):
        job = Job.from_mapping({
            'title': '开发',
            'company_name': '示例公司',
            'source_url': (
                'https://example.com/job?id=42&access_token=SYNTHETIC_TEST_ONLY'
                '&api_key=SYNTHETIC_KEY&session_id=SYNTHETIC_SESSION'
            ),
        })

        self.assertEqual(job.source_url, 'https://example.com/job?id=42')
        self.assertNotIn('SYNTHETIC', job.source_url)

    def test_job_requires_title_and_company(self):
        with self.assertRaises(ValueError):
            Job.from_mapping({'title': 'Python'})

    def test_id_stays_stable_when_salary_changes(self):
        one = Job.from_mapping({'title': '开发', 'company_name': '示例公司', 'source': 'boss', 'source_job_id': '123', 'salary_raw': '10-15K'})
        two = Job.from_mapping({'title': '开发', 'company_name': '示例公司', 'source': 'boss', 'source_job_id': '123', 'salary_raw': '20-25K'})
        self.assertEqual(one.record_key, two.record_key)
        self.assertNotEqual(one.content_hash, two.content_hash)

    def test_numeric_job_identifier_is_not_a_contact_field(self):
        job = Job.from_mapping({'title': '开发', 'company_name': '示例公司', 'source_job_id': '13812345678', 'source_url': 'https://example.cn/jobs/13812345678'})
        self.assertEqual(job.source_job_id, '13812345678')
        self.assertEqual(job.source_url, 'https://example.cn/jobs/13812345678')


class SourceConfigTests(unittest.TestCase):
    def test_github_network_override_is_local_and_optional(self):
        base = Path(__file__).resolve().parent.parent / 'data' / 'test-tmp'
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as directory:
            paths = AppPaths(Path(directory))
            config = {'sources': [{'id': 'github', 'status': 'trial', 'kind': 'repository',
                'base_url': 'https://api.github.com', 'allowed_domains': ['api.github.com'],
                'network_mode': 'system', 'proxy_url': '', 'max_items': 20}]}
            (paths.discovery_config / 'content_sources.json').write_text(json.dumps(config), encoding='utf-8')
            self.assertEqual(paths.github_content_source()['network_mode'], 'system')
            (paths.discovery_config / 'content_sources.local.json').write_text(
                json.dumps({'network_mode': 'local_proxy', 'proxy_url': 'http://127.0.0.1:7890'}), encoding='utf-8')
            self.assertEqual(paths.github_content_source()['proxy_url'], 'http://127.0.0.1:7890')
            self.assertEqual(paths.github_content_source()['network_mode'], 'local_proxy')


class StorageTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parent.parent / 'data' / 'test-tmp'
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.repo = Repository(Path(self.temp.name) / 'jobs.sqlite3')

    def job(self, **values):
        data = {'source': 'boss', 'source_job_id': '1', 'title': 'Python 开发', 'company_name': '示例科技', 'city': '北京', 'salary_raw': '15-25K'}
        return Job.from_mapping(data | values)

    def test_upsert_updates_preserving_first_seen(self):
        first = self.job(first_seen_at='2026-01-01T00:00:00+08:00')
        self.assertTrue(self.repo.upsert(first))
        self.assertFalse(self.repo.upsert(self.job(salary_raw='20-30K')))
        rows = self.repo.query(Filters())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].first_seen_at, first.first_seen_at)
        self.assertEqual(rows[0].salary_min, 20000)

    def test_cross_source_is_not_merged(self):
        self.repo.upsert(self.job())
        self.repo.upsert(self.job(source='51job'))
        self.assertEqual(len(self.repo.query(Filters())), 2)

    def test_listing_refresh_does_not_erase_saved_details(self):
        self.repo.upsert(self.job(description='已经读取的完整职责'))
        self.repo.upsert(self.job(description='', salary_raw='20-30K'))
        row = self.repo.query(Filters())[0]
        self.assertEqual(row.description, '已经读取的完整职责')
        self.assertEqual(row.salary_min, 20000)

    def test_monthly_salary_filter_does_not_include_daily_rate(self):
        self.repo.upsert(self.job())
        self.repo.upsert(self.job(source_job_id='2', salary_raw='20000元/天'))
        rows = self.repo.query(Filters(salary_min=10000, include_unknown=False))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].salary_period, '月')

    def test_query_injection_is_data(self):
        self.repo.upsert(self.job())
        self.assertEqual(self.repo.query(Filters(keyword="' OR 1=1 --")), [])
        self.assertEqual(self.repo.count(), 1)

    def test_delete_only_requested_keys(self):
        first, second = self.job(), self.job(source_job_id='2')
        self.repo.upsert(first)
        self.repo.upsert(second)
        self.repo.delete([first.record_key])
        self.assertEqual([r.record_key for r in self.repo.query(Filters())], [second.record_key])


if __name__ == '__main__':
    unittest.main()
