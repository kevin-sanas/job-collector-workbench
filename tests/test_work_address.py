import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from reportlab.platypus import Paragraph

from app.adapter_51job import Jobs51Adapter
from app.adapters import DomAdapter
from app.backend_sync import BackendSyncClient
from app.config import ROOT, Settings
from app.models import Job
from app.storage import Repository
from app.transfers import export_excel, export_pdf, import_jobs, preview_file, suggested_mapping


ADDRESS = '长沙雨花区复地·星光天地2栋29025'


class WorkAddressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = next(item for item in json.loads((ROOT / 'config' / 'sources.json').read_text(encoding='utf-8')) if item['id'] == 'boss')
        self.job = Job(source='boss', source_job_id='address-test', title='AI全栈工程师', company_name='示例公司', city='长沙', source_url='https://www.zhipin.com/job_detail/address-test.html')

    def test_legacy_job_defaults_to_empty_and_address_is_cleaned_at_mapping(self):
        self.assertEqual('', Job.from_mapping(self.job.to_dict()).work_address)
        job = Job.from_mapping({**self.job.to_dict(), 'work_address': ADDRESS + ' 联系13812345678'})
        self.assertIn(ADDRESS, job.work_address)
        self.assertNotIn('13812345678', job.work_address)

    def test_address_updates_content_hash_without_changing_identity_or_legacy_hash(self):
        old = self.job.to_dict()
        old.pop('work_address', None)
        old = {key: value for key, value in old.items() if key not in ('first_seen_at', 'last_seen_at', 'task_id', 'import_batch_id')}
        expected = hashlib.sha256(json.dumps(old, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.assertEqual(expected, self.job.content_hash)
        addressed = replace(self.job, work_address=ADDRESS)
        self.assertEqual(self.job.record_key, addressed.record_key)
        self.assertNotEqual(self.job.content_hash, addressed.content_hash)

    def test_boss_detail_reads_address_separately_from_description(self):
        page = SimpleNamespace(url=self.job.source_url, run_js=MagicMock(side_effect=['岗位职责', ADDRESS]))
        job = DomAdapter(self.source).read_detail(page, self.job)
        self.assertEqual('岗位职责', job.description)
        self.assertEqual(ADDRESS, job.work_address)
        self.assertEqual('.location-address', page.run_js.call_args.args[1])

    def test_boss_detail_rejects_another_job_before_reading_address(self):
        page = SimpleNamespace(url='https://www.zhipin.com/job_detail/another.html', run_js=MagicMock())
        with self.assertRaisesRegex(ValueError, '岗位 ID'):
            DomAdapter(self.source).read_detail(page, self.job)
        page.run_js.assert_not_called()

    def test_liepin_detail_reads_only_the_explicit_position_address(self):
        source = next(item for item in json.loads((ROOT / 'config' / 'sources.json').read_text(encoding='utf-8')) if item['id'] == 'liepin')
        job = Job(source='liepin', source_job_id='1950307653', title='韩语翻译专员', company_name='示例公司', city='深圳', source_url='https://www.liepin.com/job/1950307653.shtml')
        address = '深圳-宝安区松岗街道宇恒公寓宇恒工业园3栋4号梯3楼'
        for visible_text in (
            '公司信息\n职位地址： ' + address + '\n注册资本：2397.70万人民币\n经营范围：仪器研发',
            '公司信息\n职位地址：\n' + address + '\n注册资本：2397.70万人民币',
        ):
            with self.subTest(visible_text=visible_text):
                page = SimpleNamespace(url=job.source_url, run_js=MagicMock(side_effect=[job.title, '岗位职责', visible_text]))
                detailed = DomAdapter(source).read_detail(page, job)
                self.assertEqual(address, detailed.work_address)
                self.assertEqual('岗位职责', detailed.description)

    def test_liepin_missing_position_address_does_not_use_registered_address(self):
        source = next(item for item in json.loads((ROOT / 'config' / 'sources.json').read_text(encoding='utf-8')) if item['id'] == 'liepin')
        job = Job(source='liepin', source_job_id='1950307653', title='韩语翻译专员', company_name='示例公司', source_url='https://www.liepin.com/job/1950307653.shtml', work_address=ADDRESS)
        for visible_text in ('公司信息\n注册地址：深圳某注册地址', '职位地址：\n注册资本：2397.70万人民币\n经营范围：仪器研发'):
            with self.subTest(visible_text=visible_text):
                page = SimpleNamespace(url=job.source_url, run_js=MagicMock(side_effect=[job.title, '岗位职责', visible_text]))
                self.assertEqual(ADDRESS, DomAdapter(source).read_detail(page, job).work_address)

    def test_51job_detail_carries_the_visible_address_and_preserves_a_saved_one(self):
        source = next(item for item in json.loads((ROOT / 'config' / 'sources.json').read_text(encoding='utf-8')) if item['id'] == '51job')
        for visible_address, expected_address in (('湖南省长沙天经国际智能产业园2栋901', '湖南省长沙天经国际智能产业园2栋901'), ('', ADDRESS)):
            with self.subTest(visible_address=visible_address):
                adapter = Jobs51Adapter(source)
                job = Job(source='51job', title='Python开发', company_name='示例公司', city='长沙', identity_hint='51job-card:list:0', work_address=ADDRESS)
                item = {'index': 0, 'title': job.title, 'company_name': job.company_name, 'location': '长沙', 'salary_raw': '9千-1.1万'}
                adapter.locators[job.record_key] = item
                detail = SimpleNamespace(url='https://jobs.51job.com/changsha/159407694.html', run_js=MagicMock(side_effect=[200, {
                    'title': job.title, 'company_name': job.company_name, 'salary_raw': item['salary_raw'],
                    'requirements': '长沙\n3-4年\n本科', 'description': '岗位职责', 'work_address': visible_address,
                }]), close=MagicMock())
                browser = SimpleNamespace(tab_ids=['list'], get_tab=MagicMock(return_value=detail))
                original = SimpleNamespace(tab_id='list', browser=browser, run_js=MagicMock(return_value=SimpleNamespace(ele=MagicMock())), run_cdp=MagicMock(return_value={'targetInfos': [
                    {'targetId': 'detail', 'type': 'page', 'openerId': 'list', 'url': detail.url},
                ]}))
                session = SimpleNamespace(page=original, status=0, monitor_page=MagicMock())
                detailed = adapter.read_job_detail(session, job, lambda: None, lambda: None, lambda _: None, 0, SimpleNamespace(wait_revision=0, resume_revision=0))
                self.assertEqual(expected_address, detailed.work_address)
                self.assertEqual('159407694', detailed.source_job_id)
                self.assertEqual('岗位职责', detailed.description)
                self.assertIs(original, session.page)
                detail.close.assert_called_once()

    def test_missing_detail_and_listing_refresh_preserve_saved_address(self):
        saved = replace(self.job, work_address=ADDRESS, description='已有职责')
        page = SimpleNamespace(url=self.job.source_url, run_js=MagicMock(side_effect=['', '']))
        detail = DomAdapter(self.source).read_detail(page, saved)
        self.assertEqual(ADDRESS, detail.work_address)
        self.assertEqual('已有职责', detail.description)
        repository = Repository(self.root / 'jobs.sqlite3')
        repository.upsert(saved)
        repository.upsert(self.job)
        self.assertEqual(ADDRESS, repository.query()[0].work_address)

    def test_old_stored_payload_without_address_still_loads(self):
        repository = Repository(self.root / 'legacy.sqlite3')
        repository.upsert(self.job)
        old = self.job.to_dict()
        old.pop('work_address', None)
        with repository.connect() as connection:
            connection.execute('UPDATE jobs SET payload=?', (json.dumps(old),))
        self.assertEqual('', repository.query()[0].work_address)

    def test_excel_address_roundtrip_and_sync_payload_keep_address(self):
        job = replace(self.job, work_address=ADDRESS)
        path = self.root / 'address.xlsx'
        export_excel([job], path, {})
        preview = preview_file(path, sheet='岗位数据')
        self.assertEqual(['岗位名称', '公司名称', '城市', '原始薪资', '最低薪资', '最高薪资', '计薪周期', '年薪月数', '币种', '学历', '经验', '工作类型', '区县', '工作地址', '岗位描述', '来源', '站点岗位ID', '来源链接', '发布时间原文', '发布时间', '首次采集', '最近采集', '导出关联键'], preview.headers)
        jobs, errors = import_jobs(preview, suggested_mapping(preview.headers))
        self.assertFalse(errors)
        self.assertEqual(ADDRESS, jobs[0].work_address)
        repository = Repository(self.root / 'sync.sqlite3')
        settings = Settings(backend_url='http://127.0.0.1:8080', backend_token='unit-test-only')
        with patch.object(BackendSyncClient, 'submit', return_value={'status': 'SENT'}) as submit:
            BackendSyncClient(repository, settings).push_jobs([job], 'address-test-run')
        payload = submit.call_args.args[2]
        self.assertEqual('collector-jobs.v0.1', payload['schema_version'])
        self.assertEqual(ADDRESS, payload['jobs'][0]['work_address'])
        self.assertEqual(self.job.record_key, payload['jobs'][0]['record_key'])

    def test_retry_of_old_outbox_uses_the_original_payload(self):
        old = self.job.to_dict()
        old.pop('work_address', None)
        payload = {'schema_version': 'collector-jobs.v0.1', 'run_id': 'legacy-run', 'generated_at': '2026-10-01T00:00:00+08:00', 'jobs': [old]}
        repository = Repository(self.root / 'legacy-outbox.sqlite3')
        settings = Settings(backend_url='http://127.0.0.1:8080', backend_token='unit-test-only')
        dictionary_path = self.root / 'legacy-dictionary.json'
        dictionary_path.write_text(json.dumps({'dictionary_version': 'address-test.1', 'terms': [
            {'tech_key': 'ai', 'display_name': 'AI', 'aliases': ['AI'],
             'category': '方向', 'enabled': True, 'match_mode': 'token'},
        ]}), encoding='utf-8')
        client = BackendSyncClient(repository, settings, dictionary_path)
        with patch.object(client, '_request', side_effect=[
                ValueError('offline'), {'batchId': 'legacy'}, {'batchId': 'legacy-evidence'}]) as request:
            self.assertEqual('FAILED', client.submit('JOBS', 'legacy-run', payload)['status'])
            result = client.push_jobs([replace(self.job, work_address=ADDRESS)], 'legacy-run')
        self.assertEqual('SENT', result['status'])
        self.assertEqual('SENT', result['technology_sync']['status'])
        self.assertEqual([call.args[0] for call in request.call_args_list],
                         ['/collector/ingest/jobs', '/collector/ingest/jobs', '/collector/ingest/discovery'])
        for call in request.call_args_list[:2]:
            self.assertEqual(payload, call.args[1])
            self.assertNotIn('work_address', call.args[1]['jobs'][0])
        evidence = request.call_args_list[2].args[1]
        self.assertEqual('address-test.1', evidence['dictionary']['dictionary_version'])
        batch = evidence['batch']
        self.assertEqual('jobs-tech-' + hashlib.sha256(b'legacy-run').hexdigest(), batch['run_id'])
        self.assertEqual(payload['generated_at'], batch['generated_at'])
        self.assertEqual('address-test.1', batch['dictionary_version'])
        self.assertEqual([mention['tech_key'] for mention in batch['job_mentions']], ['ai'])
        self.assertEqual(self.job.record_key, batch['job_mentions'][0]['record_key'])
        self.assertEqual(hashlib.sha256(json.dumps({'title': old['title'], 'description': old['description']},
            ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest(),
            batch['job_mentions'][0]['job_content_hash'])

    def test_pdf_report_includes_the_work_address(self):
        path = self.root / 'address.pdf'
        with patch('app.transfers.Paragraph', wraps=Paragraph) as paragraph:
            export_pdf([replace(self.job, work_address=ADDRESS)], path, {})
        self.assertIn('工作地址：' + ADDRESS, [call.args[0] for call in paragraph.call_args_list])
        self.assertTrue(path.read_bytes().startswith(b'%PDF-'))


if __name__ == '__main__':
    unittest.main()
