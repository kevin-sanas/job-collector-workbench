import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from pathlib import Path

from app.adapters import DomAdapter, detect_state, source_url_allowed
from app.browser import launch_arguments, BrowserSession
from app.collector import TaskControl, Cancelled, Collector, Criteria
from app.storage import Repository


class AdapterTests(unittest.TestCase):
    def test_jsonld_jobs_on_same_listing_keep_distinct_identities(self):
        adapter = DomAdapter({'id': 'boss', 'domains': ['zhipin.com'], 'selectors': {}})
        markup = '<script type="application/ld+json">[{"@type":"JobPosting","title":"开发一","hiringOrganization":{"name":"企业一"},"url":"/job_detail/one.html"},{"@type":"JobPosting","title":"开发二","hiringOrganization":{"name":"企业二"},"url":"/job_detail/two.html"}]</script>'
        jobs = adapter.parse_html(markup, 'https://www.zhipin.com/web/geek/jobs')
        self.assertEqual(len({j.record_key for j in jobs}), 2)
        self.assertEqual(jobs[0].source_url, 'https://www.zhipin.com/job_detail/one.html')

    def test_response_monitor_tracks_detail_page_and_keeps_rate_limit(self):
        base = Path(__file__).resolve().parent.parent / 'data' / 'test-tmp'
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as directory:
            session = BrowserSession(SimpleNamespace(data=Path(directory)), SimpleNamespace())
            callbacks = {}
            detail = SimpleNamespace(tab_id='detail', run_cdp=lambda *args: None, _driver=SimpleNamespace(set_callback=lambda name, callback: callbacks.update({name: callback})))
            session.page = detail
            session.monitor_page(detail, ['zhipin.com'])
            callback = callbacks['Network.responseReceived']
            callback(type='XHR', response={'url': 'https://www.zhipin.com/api/jobs', 'status': 429})
            callback(type='Document', response={'url': 'https://www.zhipin.com/jobs', 'status': 200})
            self.assertEqual(session.status, 429)
            session.page = SimpleNamespace(tab_id='list')
            self.assertEqual(session.status, 0)

    def test_source_boundary_rejects_lookalikes_and_credential_urls(self):
        self.assertTrue(source_url_allowed('https://www.zhipin.com/job_detail/1.html', ['zhipin.com']))
        self.assertFalse(source_url_allowed('https://zhipin.com.evil.cn/job', ['zhipin.com']))
        self.assertFalse(source_url_allowed('https://user:pw@www.zhipin.com/', ['zhipin.com']))
        self.assertFalse(source_url_allowed('file:///private', ['zhipin.com']))

    def test_read_card_keeps_only_job_fields(self):
        source = {'id': 'boss', 'domains': ['zhipin.com'], 'selectors': {'card': '.card', 'title': '.title', 'company': '.company', 'salary': '.salary', 'city': '.city', 'link': 'a', 'description': '.desc', 'tags': '.tags'}}
        adapter = DomAdapter(source)
        html = '<div class="card"><a class="title" href="/job_detail/abc.html">Python开发</a><span class="company">示例科技</span><span class="salary">15-25K</span><span class="city">北京</span><p class="desc">联系13812345678 数据开发</p></div>'
        jobs = adapter.parse_html(html, 'https://www.zhipin.com/web/geek/jobs')
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].company_name, '示例科技')
        self.assertEqual(jobs[0].source_job_id, 'abc')
        self.assertNotIn('13812345678', jobs[0].description)

    def test_page_state_prioritizes_explicit_rate_limit(self):
        self.assertEqual(detect_state({'text': '访问过于频繁，请稍后再试', 'login': True}), 'blocked')
        self.assertEqual(detect_state({'text': '请完成安全验证', 'verification': True}), 'waiting_verification')
        self.assertEqual(detect_state({'text': '页面', 'login': True}), 'waiting_login')
        self.assertEqual(detect_state({'text': '职位介绍：开发短信验证码系统'}), 'ready')

    def test_launch_arguments_keep_direct_and_dedicated_profile(self):
        args = launch_arguments('chrome.exe', Path('profiles/boss'), 54321)
        self.assertIn('--no-proxy-server', args)
        self.assertIn('--remote-debugging-address=127.0.0.1', args)
        self.assertIn('--remote-debugging-port=54321', args)
        self.assertNotIn('--no-sandbox', args)


class ControlTests(unittest.TestCase):
    def test_human_wait_does_not_run_action_until_resume(self):
        control = TaskControl()
        completed = threading.Event()
        worker = threading.Thread(target=lambda: (control.wait_for_user(), completed.set()))
        worker.start()
        time.sleep(0.05)
        self.assertFalse(completed.is_set())
        control.resume()
        worker.join(1)
        self.assertTrue(completed.is_set())

    def test_cancel_releases_waiter(self):
        control = TaskControl()
        outcome = []
        def work():
            try:
                control.wait_for_user()
            except Cancelled:
                outcome.append('cancelled')
        worker = threading.Thread(target=work)
        worker.start()
        control.cancel()
        worker.join(1)
        self.assertEqual(outcome, ['cancelled'])

    def test_human_wait_expires_without_resume(self):
        with self.assertRaises(ValueError):
            TaskControl().wait_for_user(timeout=0.01)


class CollectionIntegrationTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parent.parent / 'data' / 'test-tmp'
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.repo = Repository(Path(self.temp.name) / 'jobs.sqlite3')
        self.source = {'id': 'boss', 'name': '本地流程测试', 'domains': ['zhipin.com'],
            'selectors': {'card': '.card', 'title': '.title', 'company': '.company', 'link': 'a'}}
        markup = ''.join(f'<div class="card"><a class="title" href="/job_detail/{i}.html">开发{i}</a><span class="company">虚构企业</span></div>' for i in range(3))
        class Page:
            url = 'https://www.zhipin.com/web/geek/jobs'
            login = False
            def run_js(self, script, *args):
                return {'text': '', 'login': self.login} if 'verification:' in script else markup
        self.page = Page()
        self.session = SimpleNamespace(page=self.page, status=0, open=lambda source: self.page)
        self.settings = SimpleNamespace(task_timeout_minutes=1, page_limit=2, interval_seconds=0)

    def test_current_page_honors_result_limit_and_stores_rows(self):
        result = Collector(self.session, self.repo, self.settings, lambda task: None).run(self.source, Criteria(limit=2, current_page=True, details=False), TaskControl())
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['count'], 2)
        self.assertEqual(self.repo.count(), 2)

    def test_cancel_from_login_wait_does_not_write_any_job(self):
        self.page.login = True
        control = TaskControl()
        def notify(task):
            if task['status'] == 'waiting_login':
                control.cancel()
        result = Collector(self.session, self.repo, self.settings, notify).run(self.source, Criteria(current_page=True, details=False), control)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(self.repo.count(), 0)

    def test_user_login_resumes_same_task(self):
        self.page.login = True
        control = TaskControl()
        def notify(task):
            if task['status'] == 'waiting_login':
                self.page.login = False
                control.resume()
        result = Collector(self.session, self.repo, self.settings, notify).run(self.source, Criteria(limit=1, current_page=True, details=False), control)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.repo.count(), 1)

    def test_rate_limit_stops_before_parsing(self):
        self.session.status = 429
        result = Collector(self.session, self.repo, self.settings, lambda task: None).run(self.source, Criteria(current_page=True), TaskControl())
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.repo.count(), 0)

    def test_async_card_render_waits_before_requesting_human_sample(self):
        original = self.page.run_js
        attempts = []
        def delayed(script, *args):
            if 'outerHTML' in script and len(attempts) < 2:
                attempts.append(1)
                return ''
            return original(script, *args)
        self.page.run_js = delayed
        control = TaskControl()
        def notify(task):
            if task['status'] == 'waiting_sample':
                control.cancel()
        result = Collector(self.session, self.repo, self.settings, notify).run(self.source, Criteria(limit=1, current_page=True, details=False), control)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.repo.count(), 1)


if __name__ == '__main__':
    unittest.main()
