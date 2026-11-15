import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class CollectorQuotaHookTests(unittest.TestCase):
    def test_rejected_items_do_not_consume_the_limit_and_saved_item_is_reported(self):
        from app.collector import Collector, Criteria, TaskControl
        from app.models import Job
        jobs = [Job(title=title, company_name='测试公司', source='boss', source_job_id=str(index),
                    source_url=f'https://www.zhipin.com/job_detail/{index}.html', description='开发软件功能')
                for index, title in enumerate(['课程销售', '软件开发工程师', '后端开发工程师'])]
        browser = MagicMock()
        browser.page.url = 'https://www.zhipin.com/'
        adapter = MagicMock()
        adapter.read_cards.return_value = jobs
        repository = MagicMock()
        repository.upsert.return_value = True
        events = []
        settings = SimpleNamespace(task_timeout_minutes=1, interval_seconds=0, page_limit=1)
        with patch('app.collector.detect_state', return_value='ready'):
            collector = Collector(browser, repository, settings, events.append, adapter_factory=lambda _: adapter,
                                  accept_job=lambda job: job.title != '课程销售')
            result = collector.run({'id': 'boss', 'name': 'BOSS', 'domains': ['zhipin.com']},
                                   Criteria(limit=2, current_page=True), TaskControl())
        self.assertEqual(result['count'], 2)
        self.assertEqual(repository.upsert.call_count, 2)
        self.assertEqual([event['item']['title'] for event in events if event.get('item')],
                         ['软件开发工程师', '后端开发工程师'])

    def test_browser_minimized_mode_is_optional_and_keeps_the_site_profile(self):
        from app.browser import launch_arguments
        from pathlib import Path
        foreground = launch_arguments('chrome.exe', Path('profile/boss'), 9999)
        background = launch_arguments('chrome.exe', Path('profile/boss'), 9999, 'BACKGROUND')
        self.assertNotIn('--start-minimized', foreground)
        self.assertIn('--start-minimized', background)
        self.assertEqual([arg for arg in foreground if arg.startswith('--user-data-dir=')],
                         [arg for arg in background if arg.startswith('--user-data-dir=')])

    def test_detail_failure_reports_the_actual_failed_item_without_counting_it(self):
        from app.collector import Collector, Criteria, TaskControl
        from app.models import Job
        browser = MagicMock()
        browser.page.url = 'https://www.zhipin.com/'
        adapter = MagicMock()
        adapter.read_cards.return_value = [Job(title='软件开发工程师', company_name='测试公司', source='boss', source_job_id='failed-item')]
        adapter.read_job_detail.side_effect = ValueError('岗位详情没有成功读取')
        events = []
        with patch('app.collector.detect_state', return_value='ready'):
            result = Collector(browser, MagicMock(), SimpleNamespace(task_timeout_minutes=1, interval_seconds=0, page_limit=1),
                               events.append, adapter_factory=lambda _: adapter, accept_job=lambda _: True).run(
                {'id': 'boss', 'name': 'BOSS', 'domains': ['zhipin.com'], 'requires_details': True},
                Criteria(limit=1, current_page=True), TaskControl())
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['count'], 0)
        self.assertEqual(events[-1]['itemError']['title'], '软件开发工程师')
        self.assertFalse(any(event.get('item') for event in events))


if __name__ == '__main__':
    unittest.main()
